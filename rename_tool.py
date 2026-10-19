# -*- coding: utf-8 -*-
"""
ImageAutoRenamer - 拖图识图自动改名工具
把图片拖进窗口，本地视觉模型识别内容后自动改名。
依赖: Python 3.11 + tkinter + tkinterdnd2 + requests + Pillow
"""

import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|]')


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()
PORT = int(CONFIG["port"])
SERVER_URL = f"http://127.0.0.1:{PORT}"
SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# 全局状态
server_proc = None          # 本工具启动的 llama-server 进程
server_starting = False
server_stopping = False
pending_items = []          # [(orig_path, new_name, ext)]
log_lock = threading.Lock()


def log_msg(msg):
    print(msg, flush=True)


def log_rename(orig_path, new_path):
    """把改名记录追加写入 rename_log.txt（UTF-8）"""
    try:
        with log_lock:
            log_file = os.path.join(BASE_DIR, CONFIG.get("log_file", "rename_log.txt"))
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{orig_path}\t->\t{new_path}\n")
    except Exception as e:
        log_msg(f"[日志写入失败] {e}")


def image_to_jpeg_base64(path, max_side):
    """把图片转为 JPEG 并压缩到最长边 max_side 内，返回 base64 字符串"""
    img = Image.open(path)
    img = img.convert("RGB")
    w, h = img.size
    max_dim = max(w, h)
    if max_dim > max_side:
        ratio = max_side / max_dim
        img = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def call_vision_api(b64_img):
    """调用本地 llama-server 视觉 API，返回描述文本"""
    payload = {
        "model": "qwen3-vl",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                    {"type": "text", "text": "用一句简短中文描述这张图片的主要内容，直接输出描述本身，不要任何前缀或解释"},
                ],
            }
        ],
        "max_tokens": int(CONFIG.get("max_tokens", 64)),
        "temperature": float(CONFIG.get("temperature", 0.1)),
    }
    resp = requests.post(f"{SERVER_URL}/v1/chat/completions", json=payload, timeout=180)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def clean_name(desc, max_len):
    """清洗 Windows 非法字符，去首尾空白，截断"""
    name = ILLEGAL_CHARS.sub("", desc)
    name = name.strip().strip(".")
    if not name:
        name = f"IMG_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return name[:max_len]


def unique_target_path(dir_path, base_name, ext):
    """目标名冲突时自动追加 _1、_2 ..."""
    candidate = os.path.join(dir_path, base_name + ext)
    if not os.path.exists(candidate):
        return candidate
    i = 1
    while True:
        candidate = os.path.join(dir_path, f"{base_name}_{i}{ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def collect_images(paths):
    """收集拖入路径中的图片文件（单文件或整个文件夹递归）"""
    images = []
    for p in paths:
        p = p.strip()
        if not p:
            continue
        # tkinterdnd2 在 Windows 下会带花括号，去掉
        p = p.strip("{}")
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for fn in sorted(files):
                    if os.path.splitext(fn)[1].lower() in SUPPORTED_EXTS:
                        images.append(os.path.join(root, fn))
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in SUPPORTED_EXTS:
            images.append(p)
    return images


class App:
    def __init__(self, root):
        self.root = root
        root.title("ImageAutoRenamer - 拖图识图自动改名")
        root.geometry("640x480")
        root.minsize(480, 360)

        self.status_var = tk.StringVar(value="把图片拖进来")
        self.list_var = tk.StringVar(value="")

        top = tk.Frame(root)
        top.pack(fill=tk.X, padx=10, pady=8)
        tk.Label(top, text="把图片拖进来（支持单张图片或整个文件夹）", font=("Microsoft YaHei UI", 13)).pack(side=tk.LEFT)

        btn_row = tk.Frame(root)
        btn_row.pack(fill=tk.X, padx=10)
        self.btn_start = tk.Button(btn_row, text="启动模型服务", command=self.start_server_click, width=14)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_stop = tk.Button(btn_row, text="停止服务", command=self.stop_server_click, width=10, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT)
        self.btn_health = tk.Button(btn_row, text="检查服务状态", command=self.check_health_click, width=13)
        self.btn_health.pack(side=tk.LEFT, padx=(6, 0))

        self.status = tk.Label(root, textvariable=self.status_var, anchor="w", fg="#333")
        self.status.pack(fill=tk.X, padx=10, pady=(4, 0))

        lst_frame = tk.Frame(root)
        lst_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(6, 4))
        self.listbox = tk.Listbox(lst_frame, font=("Consolas", 10), activestyle="none")
        scroll = tk.Scrollbar(lst_frame, orient=tk.VERTICAL, command=self.listbox.yview)
        self.listbox.config(yscrollcommand=scroll.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        action_row = tk.Frame(root)
        action_row.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.btn_all = tk.Button(action_row, text="全部改名", command=self.rename_all, state=tk.DISABLED, width=14)
        self.btn_all.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_skip = tk.Button(action_row, text="跳过", command=self.rename_skip, state=tk.DISABLED, width=10)
        self.btn_skip.pack(side=tk.LEFT)
        self.btn_clear = tk.Button(action_row, text="清空列表", command=self.clear_list, width=10)
        self.btn_clear.pack(side=tk.RIGHT)

        # 拖拽注册
        if TkinterDnD is not None:
            root.drop_target_register(DND_FILES)
            root.dnd_bind("<<Drop>>", self.on_drop)
            self.dnd_ok = True
        else:
            self.dnd_ok = False
            self.status_var.set("tkinterdnd2 未安装，拖拽不可用（请 pip install tkinterdnd2），可手动输入路径")

        # 启动时自动检测服务状态
        threading.Thread(target=self.auto_health_check, daemon=True).start()

    # ---------- 服务管理 ----------
    def auto_health_check(self):
        ok = self.health_ok()
        self.root.after(0, lambda: self.set_server_ui(ok))

    def health_ok(self, timeout=3):
        try:
            req = urllib.request.Request(f"{SERVER_URL}/health", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 200:
                    body = r.read().decode("utf-8", "ignore")
                    return "ok" in body.lower() or "ready" in body.lower()
        except Exception:
            pass
        return False

    def set_server_ui(self, ok):
        if ok:
            self.btn_start.config(state=tk.DISABLED, text="模型服务已就绪")
            self.btn_stop.config(state=tk.NORMAL)
            if not self.status_var.get().startswith("模型服务"):
                self.status_var.set("模型服务已就绪（端口 %d）" % PORT)
        else:
            self.btn_start.config(state=tk.NORMAL, text="启动模型服务")
            self.btn_stop.config(state=tk.DISABLED)
            if not server_starting:
                self.status_var.set("模型服务未启动，点『启动模型服务』开始（首次加载约需 1-3 分钟）")

    def check_health_click(self):
        self.status_var.set("正在检查服务状态...")
        threading.Thread(target=self._check_health_worker, daemon=True).start()

    def _check_health_worker(self):
        ok = self.health_ok(timeout=5)
        self.root.after(0, lambda: self.set_server_ui(ok))
        self.root.after(0, lambda: self.status_var.set("模型服务正常" if ok else "模型服务未启动/未就绪"))

    def start_server_click(self):
        global server_proc, server_starting
        if server_starting:
            return
        server_starting = True
        self.btn_start.config(state=tk.DISABLED, text="正在启动...")
        self.status_var.set("正在启动模型服务，请稍候（首次加载模型约需 1-3 分钟）...")
        threading.Thread(target=self._start_server_worker, daemon=True).start()

    def _start_server_worker(self):
        global server_proc, server_starting
        try:
            model = CONFIG["model"]
            mmproj = CONFIG["mmproj"]
            ngl = int(CONFIG.get("ngl", 99))
            ctx = int(CONFIG.get("ctx", 4096))
            cmd = [
                CONFIG.get("llama_server", r"E:\AI\llama.cpp-cuda\llama-server.exe"),
                "-m", model,
                "--mmproj", mmproj,
                "--port", str(PORT),
                "-ngl", str(ngl),
                "-c", str(ctx),
            ]
            server_proc = subprocess.Popen(
                cmd, cwd=BASE_DIR,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            deadline = time.time() + 300
            while time.time() < deadline:
                if self.health_ok(timeout=2):
                    self.root.after(0, lambda: self.set_server_ui(True))
                    self.root.after(0, lambda: self.status_var.set("模型服务已就绪（端口 %d）" % PORT))
                    log_msg("[服务] llama-server 已就绪")
                    return
                if server_proc.poll() is not None:
                    self.root.after(0, lambda: self.set_server_ui(False))
                    self.root.after(0, lambda: self.status_var.set(
                        "llama-server 进程提前退出，模型加载可能失败。\n建议改用备选后端（MiniCPM5-2B），见 README.md。"))
                    log_msg("[服务] llama-server 提前退出，加载失败")
                    return
                time.sleep(2)
            self.root.after(0, lambda: self.set_server_ui(False))
            self.root.after(0, lambda: self.status_var.set("模型加载超时（300 秒）。建议改用备选后端，见 README.md。"))
            log_msg("[服务] llama-server 加载超时")
        except Exception as e:
            self.root.after(0, lambda: self.set_server_ui(False))
            self.root.after(0, lambda: self.status_var.set(f"启动失败: {e}"))
            log_msg(f"[服务] 启动异常: {e}")
        finally:
            server_starting = False

    def stop_server_click(self):
        global server_proc, server_stopping
        if server_stopping:
            return
        server_stopping = True
        self.btn_stop.config(state=tk.DISABLED)
        self.status_var.set("正在停止模型服务...")
        threading.Thread(target=self._stop_server_worker, daemon=True).start()

    def _stop_server_worker(self):
        global server_proc, server_stopping
        try:
            if server_proc is not None and server_proc.poll() is None:
                server_proc.terminate()
                try:
                    server_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server_proc.kill()
                server_proc = None
            # 兜底：杀掉占用端口的 llama-server（仅针对本工具启动的那一个端口）
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue | "
                 f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }}"],
                capture_output=True, timeout=15)
            self.root.after(0, lambda: self.set_server_ui(False))
            self.root.after(0, lambda: self.status_var.set("模型服务已停止"))
        except Exception as e:
            self.root.after(0, lambda: self.status_var.set(f"停止失败: {e}"))
        finally:
            server_stopping = False

    # ---------- 拖拽与预览 ----------
    def on_drop(self, event):
        raw = event.data
        # tkinterdnd2 Windows 路径可能形如 {C:/a.jpg} {C:/b.jpg}
        paths = re.findall(r"\{[^}]+\}|[^\s{}]+", raw)
        images = collect_images(paths)
        if not images:
            self.status_var.set("未发现支持的图片（jpg/jpeg/png/bmp/webp）")
            return
        self.status_var.set(f"收到 {len(images)} 张图片，正在识别...")
        self.btn_all.config(state=tk.DISABLED)
        self.btn_skip.config(state=tk.DISABLED)
        self.listbox.delete(0, tk.END)
        threading.Thread(target=self.recognize_worker, args=(images,), daemon=True).start()

    def recognize_worker(self, images):
        """逐张识别并生成改名预览"""
        global pending_items
        pending_items = []
        items = []
        for idx, img_path in enumerate(images, 1):
            self.root.after(0, lambda i=idx, n=len(images), p=img_path:
                            self.status_var.set(f"[{i}/{n}] 正在识别: {os.path.basename(p)}"))
            try:
                b64 = image_to_jpeg_base64(img_path, int(CONFIG.get("max_side", 1024)))
                desc = call_vision_api(b64)
            except Exception as e:
                desc = ""
                self.root.after(0, lambda p=img_path, err=str(e):
                                self.listbox.insert(tk.END, f"[识别失败] {os.path.basename(p)}  {err}"))
                log_msg(f"[识别失败] {img_path}: {e}")
                continue
            base_name = clean_name(desc, int(CONFIG.get("max_name_len", 60)))
            ext = os.path.splitext(img_path)[1].lower()
            new_path = unique_target_path(os.path.dirname(img_path), base_name, ext)
            new_name = os.path.basename(new_path)
            items.append((img_path, new_name, ext, new_path))
            self.root.after(0, lambda it=(img_path, new_name):
                            self.listbox.insert(tk.END, f"{os.path.basename(it[0])}  ->  {it[1]}"))
        pending_items = [(p, n, e) for p, n, e, _ in items]
        self.root.after(0, lambda: self.status_var.set(
            f"识别完成：{len(items)} 张可改名（失败 {len(images) - len(items)} 张）。确认后点『全部改名』或『跳过』"))
        self.root.after(0, lambda: self.btn_all.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.btn_skip.config(state=tk.NORMAL))

    # ---------- 改名执行 ----------
    def do_rename(self, item):
        orig, new_name, ext = item
        orig_dir = os.path.dirname(orig)
        new_path = unique_target_path(orig_dir, os.path.splitext(new_name)[0], ext)
        try:
            os.rename(orig, new_path)
            log_rename(orig, new_path)
            return True, new_path
        except Exception as e:
            return False, str(e)

    def rename_all(self):
        global pending_items
        if not pending_items:
            return
        done, failed = 0, 0
        for it in pending_items:
            ok, info = self.do_rename(it)
            if ok:
                done += 1
                self.root.after(0, lambda p=info: self.listbox.insert(tk.END, f"[已改名] {p}"))
            else:
                failed += 1
                self.root.after(0, lambda err=info: self.listbox.insert(tk.END, f"[改名失败] {err}"))
        pending_items = []
        self.root.after(0, lambda: self.status_var.set(f"改名完成：成功 {done}，失败 {failed}。记录已写入 rename_log.txt"))
        self.root.after(0, lambda: self.btn_all.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.btn_skip.config(state=tk.DISABLED))

    def rename_skip(self):
        global pending_items
        pending_items = []
        self.status_var.set("已跳过本次改名")
        self.btn_all.config(state=tk.DISABLED)
        self.btn_skip.config(state=tk.DISABLED)

    def clear_list(self):
        self.listbox.delete(0, tk.END)
        self.status_var.set("把图片拖进来")
        self.btn_all.config(state=tk.DISABLED)
        self.btn_skip.config(state=tk.DISABLED)


def main():
    global tk
    import tkinter as tk

    if TkinterDnD is None:
        log_msg("[错误] 未安装 tkinterdnd2，请先执行: pip install tkinterdnd2")
        sys.exit(1)
    if requests is None:
        log_msg("[错误] 未安装 requests，请先执行: pip install requests")
        sys.exit(1)
    if Image is None:
        log_msg("[错误] 未安装 Pillow，请先执行: pip install Pillow")
        sys.exit(1)

    root = TkinterDnD.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
