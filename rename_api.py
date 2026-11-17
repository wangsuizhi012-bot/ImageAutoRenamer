# -*- coding: utf-8 -*-
"""
rename_api.py - ImageAutoRenamer HTTP API 服务（标准库实现，零额外依赖）

启动：
    python rename_api.py                 # 默认 127.0.0.1:8787
    python rename_api.py --port 8787 --host 0.0.0.0
    python rename_api.py --token mysecret  # 开启 Bearer 鉴权

接口（全部 JSON）：
    GET  /                    服务说明 + 接口清单（Agent 自发现）
    GET  /health              上游视觉后端探活
    GET  /v1/info             当前配置（api_key 脱敏）
    POST /v1/describe         单图/多图识别，返回描述
    POST /v1/preview          批量预览新文件名（不改名）
    POST /v1/rename           批量改名（dry_run 可开关）
    POST /v1/undo             按日志回滚
    POST /v1/upload           base64 图片上传 + 可选直接改名（远程 Agent 用）

OpenAI 兼容的上游配置写在 config.json 的 "api" 段（base_url / api_key / model 等）。
"""

import argparse
import base64
import json
import os
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from renamer_core import (
    BASE_DIR, Renamer, load_config, get_api_cfg, collect_images,
)

UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
START_TS = time.time()

SERVICE_CARD = {
    "service": "ImageAutoRenamer API",
    "version": "2.0",
    "endpoints": {
        "GET /": "本说明",
        "GET /health": "上游视觉后端探活",
        "GET /v1/info": "当前配置（api_key 脱敏）",
        "POST /v1/describe": {"body": {"paths": ["D:/a.jpg"], "image_base64": "…", "prompt": "可选"},
                              "desc": "返回图片内容描述"},
        "POST /v1/preview": {"body": {"paths": ["D:/dir"], "template": "{date}_{desc}"},
                             "desc": "只返回建议的新文件名，不落盘"},
        "POST /v1/rename": {"body": {"paths": ["D:/dir"], "dry_run": False,
                                     "template": "{desc}", "max_workers": 4},
                            "desc": "实际改名，返回逐条结果"},
        "POST /v1/undo": {"body": {"dry_run": True, "last_n": 0}, "desc": "按日志逆序回滚"},
        "POST /v1/upload": {"body": {"filename": "a.jpg", "image_base64": "…", "apply": False},
                            "desc": "远程图片上传，可顺手改名"},
    },
    "auth": "若 config.json 的 api.serve_token 非空，需带 Authorization: Bearer <token>",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ImageAutoRenamer/2.0"
    renamer = None       # 由 main() 注入
    token = ""

    # ---------------- 基础输出 ----------------
    def _send(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def _auth_ok(self):
        if not self.token:
            return True
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer ") and h[7:].strip() == self.token:
            return True
        return (self.headers.get("X-API-Key") or "").strip() == self.token

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    # ---------------- 路由 ----------------
    def do_OPTIONS(self):
        self._send({"ok": True})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path == "/":
            card = dict(SERVICE_CARD)
            card["uptime_s"] = int(time.time() - START_TS)
            return self._send({"ok": True, **card})
        if path == "/health":
            try:
                h = self.renamer.client.health()
            except Exception as e:
                h = {"ok": False, "detail": str(e)}
            return self._send({"ok": h.get("ok", False), "upstream": h,
                               "backend": self.renamer.client.base_url,
                               "model": self.renamer.client.model})
        if path == "/v1/info":
            if not self._auth_ok():
                return self._send({"ok": False, "error": "unauthorized"}, 401)
            return self._send({"ok": True, "info": self.renamer.info()})
        return self._send({"ok": False, "error": "not found: " + path}, 404)

    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        if not self._auth_ok():
            return self._send({"ok": False, "error": "unauthorized"}, 401)
        try:
            body = self._read_json()
        except ValueError as e:
            return self._send({"ok": False, "error": str(e)}, 400)

        try:
            if path == "/v1/describe":
                return self._send(self._describe(body))
            if path == "/v1/preview":
                body["dry_run"] = True
                return self._send(self.renamer.process(
                    body.get("paths") or [], dry_run=True,
                    template=body.get("template"), prompt=body.get("prompt"),
                    max_workers=body.get("max_workers")))
            if path == "/v1/rename":
                return self._send(self.renamer.process(
                    body.get("paths") or [], dry_run=bool(body.get("dry_run", False)),
                    template=body.get("template"), prompt=body.get("prompt"),
                    max_workers=body.get("max_workers")))
            if path == "/v1/undo":
                return self._send(self.renamer.undo(
                    dry_run=bool(body.get("dry_run", True)),
                    last_n=int(body.get("last_n") or 0)))
            if path == "/v1/upload":
                return self._send(self._upload(body))
            return self._send({"ok": False, "error": "not found: " + path}, 404)
        except Exception as e:
            return self._send({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    # ---------------- 业务 ----------------
    def _describe(self, body):
        t0 = time.time()
        b64 = body.get("image_base64")
        if b64:
            if "," in b64[:64]:
                b64 = b64.split(",", 1)[1]
            desc = self.renamer.client.describe(b64, prompt=body.get("prompt"))
            return {"ok": True, "desc": strip_desc(desc), "raw": desc,
                    "elapsed_ms": int((time.time() - t0) * 1000)}
        paths = collect_images(body.get("paths") or [])
        if not paths:
            return {"ok": False, "error": "paths 中未找到图片"}
        out = []
        for p in paths:
            try:
                d = self.renamer.describe_file(p, prompt=body.get("prompt"))
                d["src"] = p
                d["suggest"] = self.renamer.render_name(d["desc"], p, body.get("template"))
                out.append(d)
            except Exception as e:
                out.append({"src": p, "error": str(e)})
        return {"ok": True, "total": len(out), "elapsed_ms": int((time.time() - t0) * 1000),
                "results": out}

    def _upload(self, body):
        b64 = body.get("image_base64") or ""
        if not b64:
            return {"ok": False, "error": "缺少 image_base64"}
        if "," in b64[:64]:
            b64 = b64.split(",", 1)[1]
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        fn = os.path.basename(body.get("filename") or
                              "upload_%s.jpg" % datetime.now().strftime("%Y%m%d_%H%M%S"))
        dst = os.path.join(UPLOAD_DIR, fn)
        with open(dst, "wb") as f:
            f.write(base64.b64decode(b64))
        if body.get("apply"):
            res = self.renamer.process([dst], dry_run=False,
                                       template=body.get("template"),
                                       prompt=body.get("prompt"))
            return res
        d = self.renamer.describe_file(dst, prompt=body.get("prompt"))
        return {"ok": True, "path": dst, "desc": strip_desc(d["desc"]),
                "suggest": self.renamer.render_name(d["desc"], dst, body.get("template"))}


def strip_desc(text):
    """去掉模型偶尔带出的前缀/引号，便于直接当文件名"""
    if not text:
        return ""
    t = text.strip().strip('"').strip("'").strip()
    for p in ("这张图片", "图片内容：", "描述：", "内容："):
        if t.startswith(p):
            t = t[len(p):].lstrip("：: ")
    return t.strip()


def main():
    ap = argparse.ArgumentParser(description="ImageAutoRenamer HTTP API server")
    ap.add_argument("--host", default=None, help="监听地址，默认取 config api.serve_host")
    ap.add_argument("--port", type=int, default=None, help="监听端口，默认取 config api.serve_port")
    ap.add_argument("--token", default=None, help="Bearer 鉴权 token，默认取 config api.serve_token")
    ap.add_argument("--config", default=None, help="指定配置文件路径，默认用同目录 config.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    api = get_api_cfg(cfg)
    host = args.host or api["serve_host"]
    port = int(args.port or api["serve_port"])
    token = args.token if args.token is not None else str(api.get("serve_token") or "")

    Handler.renamer = Renamer(cfg)
    Handler.token = token

    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        print("[ERROR] 端口 %s:%d 无法监听: %s" % (host, port, e), flush=True)
        sys.exit(1)
    srv.daemon_threads = True
    info = Handler.renamer.info()
    print("=" * 62, flush=True)
    print(" ImageAutoRenamer API 已启动", flush=True)
    print("  地址     : http://%s:%d" % (host, port), flush=True)
    print("  上游后端 : %s (%s)" % (info["base_url"], info["backend"]), flush=True)
    print("  模型     : %s" % info["model"], flush=True)
    print("  并发     : %d  缓存: %d 条" % (info["concurrency"], info["cache_entries"]), flush=True)
    print("  鉴权     : %s" % ("Bearer " + token[:4] + "***" if token else "未开启"), flush=True)
    print("=" * 62, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[API] 收到中断，正在关闭...", flush=True)
    finally:
        Handler.renamer.cache.save()
        srv.server_close()


if __name__ == "__main__":
    main()
