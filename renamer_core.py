# -*- coding: utf-8 -*-
"""
renamer_core.py - ImageAutoRenamer 核心库

设计目标：
1. 唯一实现来源：GUI(rename_tool.py) / CLI / HTTP API(rename_api.py) / MCP(rename_mcp_server.py) 全部复用本模块，不再各写一套。
2. 零第三方依赖：仅需 Pillow；HTTP 走标准库 http.client，支持连接复用（keep-alive），不再每张图新建 TCP 连接。
3. 速度优化：
   - 预缩放 + JPEG draft 解码（跳过全尺寸解码）
   - 默认长边 768 / JPEG q80（token 数更少，推理更快）
   - 内容 sha1 缓存：同一张图第二次识别 0 推理
   - 并发识别（默认 4 路，可配）
   - 批量接口一次调用完成 N 张，避免逐张 HTTP 往返
4. 上游 API 完全可配：本地 llama-server / 云端 OpenAI 兼容接口，只要填 base_url + api_key + model。
"""

import base64
import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.client import HTTPConnection, HTTPSConnection

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

DEFAULT_PROMPT = (
    "用一句简短中文描述这张图片的主要内容，直接输出描述本身，"
    "不要任何前缀、标点结尾或解释。如果与常见图片相似，"
    "请描述颜色、布局、主体数量、位置等可区分特征，避免与其他图片同名。"
)


# ------------------------------------------------------------------ 配置

def load_config(path=None):
    """读取 config.json（每次调用都重新读，支持热更新）

    优先级：显式 path > 环境变量 IA_RENAMER_CONFIG > 同目录 config.json
    """
    p = path or os.environ.get("IA_RENAMER_CONFIG") or CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def get_api_cfg(config=None):
    """取 api 段配置，缺省安全回退"""
    cfg = config if config is not None else load_config()
    api = dict(cfg.get("api") or {})
    api.setdefault("base_url", "")
    api.setdefault("api_key", "")
    api.setdefault("model", "")
    api.setdefault("timeout", 90)
    api.setdefault("retry", 1)
    api.setdefault("max_concurrency", 4)
    api.setdefault("max_side", cfg.get("max_side", 768))
    api.setdefault("jpeg_quality", 80)
    api.setdefault("name_template", "{desc}")
    api.setdefault("prompt", "")
    api.setdefault("cache_file", "cache/desc_cache.json")
    api.setdefault("serve_host", "127.0.0.1")
    api.setdefault("serve_port", 8787)
    api.setdefault("serve_token", "")
    return api


# ------------------------------------------------------------------ 工具函数

def file_sha1(path, chunk=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def encode_image(path, max_side=768, quality=80, known_sha=None):
    """图片 -> (base64 jpeg, 文件sha1)。JPEG 走 draft 降采样解码，比全解码快数倍。"""
    if Image is None:
        raise RuntimeError("Pillow 未安装：pip install Pillow")
    sha = known_sha or file_sha1(path)
    with Image.open(path) as img:
        if (img.format or "").upper() in ("JPEG", "MPO", "JPG"):
            try:
                img.draft("RGB", (max_side, max_side))
            except Exception:
                pass
        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > max_side:
            # thumbnail 内部走 draft/reduce 降采样，比全解码后 LANCZOS 快数倍
            img.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii"), sha


def clean_name(desc, max_len=60, space_to_underscore=False):
    """清洗 Windows 非法字符 / 首尾点空格 / 截断"""
    name = ILLEGAL_CHARS.sub("", desc or "")
    name = name.strip().strip(".")
    # 视觉模型偶尔多带的前缀不该进文件名
    for p in ("这张图片", "这是一张", "这是一个", "这是一幅", "展示了", "展示一个",
              "图片内容：", "描述：", "内容："):
        if name.startswith(p):
            name = name[len(p):].lstrip("：: ")
            break
    if space_to_underscore:
        name = re.sub(r"\s+", "_", name)
    name = re.sub(r"\s{2,}", " ", name)
    if not name:
        name = "IMG_%s" % datetime.now().strftime("%Y%m%d_%H%M%S")
    return name[:max_len].strip().strip(".")


def collect_images(paths, recursive=True):
    """收集图片：支持单文件 / 文件夹 / 通配目录，去重保序"""
    if isinstance(paths, str):
        paths = [paths]
    seen, out = set(), []
    for p in paths or []:
        p = str(p).strip().strip("{}").strip('"')
        if not p:
            continue
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            if recursive:
                for root, _d, files in os.walk(p):
                    for fn in sorted(files):
                        if os.path.splitext(fn)[1].lower() in SUPPORTED_EXTS:
                            ap = os.path.join(root, fn)
                            if ap not in seen:
                                seen.add(ap)
                                out.append(ap)
            else:
                for fn in sorted(os.listdir(p)):
                    ap = os.path.join(p, fn)
                    if os.path.isfile(ap) and os.path.splitext(fn)[1].lower() in SUPPORTED_EXTS:
                        if ap not in seen:
                            seen.add(ap)
                            out.append(ap)
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED_EXTS:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


# ------------------------------------------------------------------ 缓存

class DescCache:
    """描述结果缓存：键 = 内容sha1 + 模型，值 = 描述文本。降低重复识别开销"""

    def __init__(self, path, limit=50000, autosave_s=30):
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._data = {}
        self._dirty = 0
        self._load()
        if autosave_s and autosave_s > 0:
            t = threading.Thread(target=self._autosave_loop, args=(autosave_s,), daemon=True)
            t.start()

    def _autosave_loop(self, interval):
        while True:
            time.sleep(interval)
            if self._dirty > 0:
                self.save()

    def _load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self._data = d
        except Exception:
            self._data = {}

    def get(self, key):
        with self._lock:
            return self._data.get(key)

    def put(self, key, value):
        with self._lock:
            self._data[key] = value
            self._dirty += 1
            if len(self._data) > self.limit:
                for k in list(self._data.keys())[: len(self._data) - self.limit]:
                    self._data.pop(k, None)
            need_save = self._dirty >= 5
            if need_save:
                self._dirty = 0
        if need_save:
            self.save()

    def save(self):
        try:
            with self._lock:
                snapshot = dict(self._data)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def __len__(self):
        return len(self._data)


# ------------------------------------------------------------------ 视觉 API 客户端

class VisionClient:
    """OpenAI 兼容 /v1/chat/completions 客户端，标准库实现，线程内连接复用"""

    def __init__(self, api_cfg=None, base_cfg=None, cache=None):
        cfg = base_cfg if base_cfg is not None else load_config()
        api = api_cfg if api_cfg is not None else get_api_cfg(cfg)
        self.cache = cache

        base_url = str(api.get("base_url") or "").strip()
        if base_url:
            self.base_url = base_url.rstrip("/")
            if not self.base_url.endswith("/v1"):
                self.base_url += "/v1"
            self.source = "remote"
        else:
            self.base_url = "http://127.0.0.1:%d/v1" % int(cfg.get("port", 11436))
            self.source = "local"

        self.api_key = str(api.get("api_key") or "").strip()
        self.model = str(api.get("model") or "").strip() or "qwen3-vl"
        self.timeout = float(api.get("timeout") or 90)
        self.retry = max(0, int(api.get("retry") or 0))
        self.prompt = str(api.get("prompt") or DEFAULT_PROMPT)
        self.max_tokens = int(api.get("max_tokens") or cfg.get("max_tokens") or 64)
        self.temperature = float(api.get("temperature", cfg.get("temperature", 0.1)))

        u = urllib.parse.urlsplit(self.base_url)
        self._scheme = (u.scheme or "http").lower()
        self._host = u.hostname or "127.0.0.1"
        self._port = u.port or (443 if self._scheme == "https" else 80)
        self._prefix = (u.path or "").rstrip("/")
        self._local = threading.local()

    # -- 连接管理（每线程一条长连接，失败即重建）
    def _conn(self, fresh=False):
        conn = getattr(self._local, "conn", None)
        if conn is None or fresh:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            cls = HTTPSConnection if self._scheme == "https" else HTTPConnection
            conn = cls(self._host, self._port, timeout=self.timeout)
            self._local.conn = conn
        return conn

    def _post_json(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        resp = self._conn().request("POST", path, body=body, headers=headers)
        r = self._conn().getresponse()
        raw = r.read()
        if r.status != 200:
            raise RuntimeError("HTTP %s: %s" % (r.status, raw[:300].decode("utf-8", "ignore")))
        try:
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception as e:
            raise RuntimeError("响应不是合法 JSON: %s" % e)

    def chat(self, messages, max_tokens=None, temperature=None):
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": int(max_tokens if max_tokens is not None else self.max_tokens),
            "temperature": float(temperature if temperature is not None else self.temperature),
        }
        path = self._prefix + "/chat/completions"
        last_err = None
        for attempt in range(self.retry + 1):
            try:
                data = self._post_json(path, payload)
                return (data["choices"][0]["message"]["content"] or "").strip()
            except Exception as e:
                last_err = e
                # 连接可能已废（keep-alive 被上游关闭），强制重建后重试
                self._conn(fresh=True)
                if attempt < self.retry:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError("视觉接口调用失败: %s" % last_err)

    def describe(self, b64_img, prompt=None):
        key = None
        if self.cache is not None:
            key = hashlib.sha1(("%s|%s|%s" % (
                hashlib.sha1(b64_img.encode()).hexdigest(), self.model, prompt or "")).encode()).hexdigest()
            hit = self.cache.get(key)
            if hit:
                return hit
        msgs = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64_img}},
                {"type": "text", "text": prompt or self.prompt},
            ],
        }]
        text = self.chat(msgs)
        if key:
            self.cache.put(key, text)
        return text

    def health(self):
        """探活：优先 /health，退回 /v1/models"""
        for p in ("/health", self._prefix + "/models"):
            try:
                self._conn().request("GET", p)
                r = self._conn().getresponse()
                body = r.read().decode("utf-8", "ignore")
                if r.status == 200:
                    return {"ok": True, "probe": p, "detail": body[:200]}
            except Exception:
                self._conn(fresh=True)
        return {"ok": False, "probe": None, "detail": "unreachable"}


# ------------------------------------------------------------------ 主流程

class Renamer:
    """识图 + 改名 全流程封装。线程安全，可被服务端多请求复用。"""

    def __init__(self, config=None):
        self.config = config if config is not None else load_config()
        self.api = get_api_cfg(self.config)
        self.client = VisionClient(self.api, self.config)

        self.max_side = int(self.api["max_side"])
        self.quality = int(self.api["jpeg_quality"])
        self.max_name_len = int(self.config.get("max_name_len", 60))
        self.template = self.api["name_template"] or "{desc}"
        self.concurrency = max(1, int(self.api["max_concurrency"]))
        self._sem = threading.BoundedSemaphore(self.concurrency)
        self._reserved_lock = threading.Lock()
        self._reserved_map = {}

        cache_path = self.api["cache_file"]
        if not os.path.isabs(cache_path):
            cache_path = os.path.join(BASE_DIR, cache_path)
        self.cache = DescCache(cache_path)
        self.log_file = os.path.join(BASE_DIR, self.config.get("log_file", "rename_log.txt"))
        self.log_lock = threading.Lock()

    # -- 描述
    def describe_file(self, path, prompt=None):
        t0 = time.time()
        sha = file_sha1(path)
        key = hashlib.sha1(("%s|%s|%s" % (sha, self.client.model, prompt or "")).encode()).hexdigest()
        hit = self.cache.get(key)
        if hit:
            # 命中缓存时完全跳过解码与推理
            return {"desc": hit, "cached": True, "ms": int((time.time() - t0) * 1000), "sha1": sha}
        b64, sha = encode_image(path, self.max_side, self.quality, known_sha=sha)
        with self._sem:
            desc = self.client.describe(b64, prompt=prompt)
        self.cache.put(key, desc)
        return {"desc": desc, "cached": False, "ms": int((time.time() - t0) * 1000), "sha1": sha}

    # -- 命名
    def render_name(self, desc, src_path, template=None):
        tpl = template or self.template
        stem = os.path.splitext(os.path.basename(src_path))[0]
        try:
            date = datetime.fromtimestamp(os.path.getmtime(src_path)).strftime("%Y%m%d")
        except Exception:
            date = datetime.now().strftime("%Y%m%d")
        raw = (tpl.replace("{desc}", desc or "")
                  .replace("{date}", date)
                  .replace("{orig}", stem)
                  .replace("{ext}", ""))
        return clean_name(raw, self.max_name_len)

    def _names(self, folder):
        s = self._reserved_map.get(folder)
        if s is None:
            try:
                s = set(n.lower() for n in os.listdir(folder))
            except Exception:
                s = set()
            self._reserved_map[folder] = s
        return s

    def alloc_target(self, src_path, base_name):
        """重名自动追加 _1、_2；同批并发也安全"""
        folder = os.path.dirname(src_path)
        ext = os.path.splitext(src_path)[1].lower()
        with self._reserved_lock:
            names = self._names(folder)
            cand = base_name + ext
            i = 1
            while cand.lower() in names:
                cand = "%s_%d%s" % (base_name, i, ext)
                i += 1
            names.add(cand.lower())
            return os.path.join(folder, cand)

    def log_rename(self, orig, new):
        try:
            with self.log_lock:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write("%s\t%s\t->\t%s\n" % (
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"), orig, new))
        except Exception:
            pass

    def _release_name(self, src_path):
        """改名后释放旧文件名的占位，避免同批次重复追加 _1/_2"""
        folder = os.path.dirname(src_path)
        with self._reserved_lock:
            s = self._reserved_map.get(folder)
            if s is not None:
                s.discard(os.path.basename(src_path).lower())

    def _one(self, path, template, prompt):
        r = {"src": path, "dst": None, "desc": None, "cached": False,
             "ms": 0, "status": "pending", "error": None}
        try:
            d = self.describe_file(path, prompt=prompt)
            r.update(desc=d["desc"], cached=d["cached"], ms=d["ms"])
            r["base_name"] = self.render_name(d["desc"], path, template)
            r["status"] = "described"
        except Exception as e:
            r["status"] = "error"
            r["error"] = str(e)
        return r

    def process(self, paths, dry_run=True, template=None, prompt=None, max_workers=None,
                on_progress=None):
        """批量识别（并发）+ 顺序分配名称 + 落盘改名。返回结构化结果。

        on_progress(result, done, total) 每张完成时回调，供长任务流式观察。
        """
        t0 = time.time()
        with self._reserved_lock:
            # 每次 process 是独立批次：唯一化只依据磁盘现状 + 本批次结果，
            # 避免上一次 preview 占位残留导致名字被追加 _1/_2
            self._reserved_map.clear()
        images = collect_images(paths)
        if not images:
            return {"ok": False, "error": "未找到支持的图片（jpg/jpeg/png/bmp/webp）",
                    "total": 0, "renamed": 0, "failed": 0, "cached": 0,
                    "elapsed_ms": 0, "results": []}

        workers = int(max_workers or self.concurrency)
        results = [None] * len(images)

        def emit(r, done):
            if on_progress:
                try:
                    on_progress(r, done, len(images))
                except Exception:
                    pass

        if workers <= 1:
            for i, p in enumerate(images):
                results[i] = self._one(p, template, prompt)
                emit(results[i], i + 1)
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self._one, p, template, prompt): i for i, p in enumerate(images)}
                done = 0
                for f in as_completed(futs):
                    results[futs[f]] = f.result()
                    done += 1
                    emit(results[futs[f]], done)

        renamed = failed = cached = 0
        plan = []
        for r in results:
            if r is None:
                failed += 1
                continue
            if r["status"] == "error":
                failed += 1
                plan.append(r)
                continue
            if r["cached"]:
                cached += 1
            target = self.alloc_target(r["src"], r["base_name"])
            r["dst"] = target
            if os.path.normcase(target) == os.path.normcase(r["src"]):
                r["status"] = "unchanged"
                renamed += 1
                plan.append(r)
                continue
            if dry_run:
                r["status"] = "preview"
                renamed += 1
                plan.append(r)
                continue
            try:
                os.rename(r["src"], target)
                self.log_rename(r["src"], target)
                self._release_name(r["src"])
                r["status"] = "renamed"
                renamed += 1
            except Exception as e:
                r["status"] = "rename_failed"
                r["error"] = str(e)
                failed += 1
            plan.append(r)

        try:
            self.cache.save()
        except Exception:
            pass
        return {
            "ok": failed == 0,
            "dry_run": bool(dry_run),
            "total": len(images),
            "renamed": renamed,
            "failed": failed,
            "cached": cached,
            "concurrency": workers,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "results": plan,
        }

    def undo(self, dry_run=True, last_n=0):
        """按 rename_log.txt 逆序回滚"""
        if not os.path.exists(self.log_file):
            return {"ok": False, "error": "日志不存在: " + self.log_file, "restored": 0, "results": []}
        pairs = []
        with open(self.log_file, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 4 and parts[2] == "->":
                    pairs.append((parts[1], parts[3]))
        if last_n and last_n > 0:
            pairs = pairs[-last_n:]
        ok = skip = fail = 0
        out = []
        for orig, new in reversed(pairs):
            item = {"from": new, "to": orig, "status": "ok"}
            if os.path.exists(orig):
                item["status"] = "skip_exists"
                skip += 1
            elif not os.path.exists(new):
                item["status"] = "missing"
                fail += 1
            elif dry_run:
                item["status"] = "preview"
                ok += 1
            else:
                try:
                    os.rename(new, orig)
                    ok += 1
                except Exception as e:
                    item["status"] = "error"
                    item["error"] = str(e)
                    fail += 1
            out.append(item)
        return {"ok": fail == 0, "dry_run": bool(dry_run), "restored": ok,
                "skipped": skip, "failed": fail, "results": out}

    def info(self):
        return {
            "backend": self.client.source,
            "base_url": self.client.base_url,
            "model": self.client.model,
            "api_key_set": bool(self.client.api_key),
            "max_side": self.max_side,
            "jpeg_quality": self.quality,
            "concurrency": self.concurrency,
            "name_template": self.template,
            "cache_entries": len(self.cache),
            "log_file": self.log_file,
        }
