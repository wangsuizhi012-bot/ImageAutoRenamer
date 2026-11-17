# -*- coding: utf-8 -*-
"""
ImageAutoRenamer - 拖图识图自动改名工具
把图片拖进窗口，本地视觉模型识别内容后自动改名。
依赖: Python 3.11+ + tkinter + tkinterdnd2 + Pillow（识别/缓存/改名统一走 renamer_core）
"""

import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import tkinter as tk
except ImportError:
    tk = None

# 统一走核心库：并发 / 内容缓存 / 连接复用 / 上游 API 可配置都在 renamer_core 里
from renamer_core import Renamer, VisionClient, collect_images, load_config
from renamer_core import get_api_cfg as core_api_cfg

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    TkinterDnD = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG = load_config()
PORT = int(CONFIG["port"])
SERVER_URL = f"http://127.0.0.1:{PORT}"

# 全局状态
server_proc = None          # 本工具启动的 llama-server 进程
server_starting = False
server_stopping = False
pending_items = []          # [(orig_path, new_name, ext)]


def log_msg(msg):
    print(msg, flush=True)


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
        # 状态灯必须探活配置的上游（api.base_url），11436 只是无配置时的回退
        try:
            api = dict(core_api_cfg(CONFIG))
            api["timeout"] = timeout
            if VisionClient(api, CONFIG).health().get("ok"):
                return True
        except Exception:
            pass
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
                upstream = core_api_cfg(CONFIG).get("base_url") or "端口 %d" % PORT
                self.status_var.set("模型服务已就绪（%s）" % upstream)
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
        """并发识别并生成改名预览（默认取 config.api.max_concurrency 路并发）"""
        global pending_items
        pending_items = []
        total = len(images)
        workers = max(1, int(core_api_cfg(CONFIG).get("max_concurrency", 4)))
        results = {}
        counter = {"done": 0}
        lock = threading.Lock()
        renamer = Renamer(CONFIG)

        def work(idx, img_path):
            try:
                d = renamer.describe_file(img_path)
                return idx, img_path, renamer.render_name(d["desc"], img_path), None
            except Exception as e:
                return idx, img_path, None, str(e)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(work, i, p) for i, p in enumerate(images)]
            for fut in as_completed(futs):
                idx, img_path, base_name, err = fut.result()
                with lock:
                    counter["done"] += 1
                    n = counter["done"]
                if err:
                    log_msg(f"[识别失败] {img_path}: {err}")
                    self.root.after(0, lambda p=img_path, e=err: self.listbox.insert(
                        tk.END, f"[识别失败] {os.path.basename(p)}  {e}"))
                else:
                    ext = os.path.splitext(img_path)[1].lower()
                    results[idx] = (img_path, base_name + ext, ext)
                    self.root.after(0, lambda p=img_path, b=base_name, x=ext: self.listbox.insert(
                        tk.END, f"{os.path.basename(p)}  ->  {b}{x}"))
                self.root.after(0, lambda k=n: self.status_var.set(f"[{k}/{total}] 已识别"))

        items = [results[i] for i in range(total) if i in results]
        pending_items = items
        self.root.after(0, lambda: self.status_var.set(
            f"识别完成：{len(items)} 张可改名（失败 {total - len(items)} 张）。确认后点『全部改名』或『跳过』"))
        self.root.after(0, lambda: self.btn_all.config(state=tk.NORMAL))
        self.root.after(0, lambda: self.btn_skip.config(state=tk.NORMAL))

    # ---------- 改名执行 ----------
    def do_rename(self, item, renamer):
        orig, new_name, ext = item
        new_path = renamer.alloc_target(orig, os.path.splitext(new_name)[0])
        try:
            os.rename(orig, new_path)
            renamer.log_rename(orig, new_path)
            return True, new_path
        except Exception as e:
            return False, str(e)

    def rename_all(self):
        global pending_items
        if not pending_items:
            return
        done, failed = 0, 0
        renamer = Renamer(CONFIG)
        for it in pending_items:
            ok, info = self.do_rename(it, renamer)
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


# ---------------------------------------------------------- headless mode


def run_cli(path, dry_run=False, workers=None):
    """Headless batch mode: 复用 core（并发识别 + 内容缓存 + 结构化结果）。

    Usable by workflow runners / emp_runner as the worker node, no GUI.
    """
    r = Renamer(load_config())

    def progress(it, done, total):
        if it.get("error") and it.get("status") in ("error", "rename_failed"):
            log_msg("[FAIL %d/%d] %s %s" % (done, total, it["src"], it["error"]))
            return
        tag = "DRY" if dry_run else "OK"
        log_msg("[%s %d/%d] %s -> %s  (desc: %s)" % (
            tag, done, total, os.path.basename(it["src"]),
            os.path.basename(it.get("dst") or ""), (it.get("desc") or "")[:40]))

    res = r.process([path], dry_run=dry_run,
                    max_workers=max(1, int(workers)) if workers else None,
                    on_progress=progress)
    if not res.get("total"):
        log_msg("[CLI] no images found: " + str(path))
        r.cache.save()
        return
    log_msg("[CLI] done=%d failed=%d cached=%d elapsed=%dms dry_run=%s" % (
        res["renamed"], res["failed"], res["cached"], res["elapsed_ms"], dry_run))
    r.cache.save()


def run_undo(dry_run=False):
    """Reverse rename_log.txt entries (new -> original), for overseer/rollback."""
    log_file = os.path.join(BASE_DIR, CONFIG.get("log_file", "rename_log.txt"))
    if not os.path.exists(log_file):
        log_msg("[UNDO] no log file: " + log_file)
        return
    pairs = []
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 4 and parts[2] == "->":
                pairs.append((parts[1], parts[3]))
    ok = skip = fail = 0
    for orig, new in reversed(pairs):
        if os.path.exists(orig):
            skip += 1
            continue
        if not os.path.exists(new):
            fail += 1
            log_msg("[UNDO-SKIP] new missing: " + new)
            continue
        tag = "DRY" if dry_run else "OK"
        log_msg("[%s] %s -> %s" % (tag, os.path.basename(new), os.path.basename(orig)))
        if not dry_run:
            try:
                os.rename(new, orig)
            except Exception as e:
                fail += 1
                log_msg("[UNDO-FAIL] %s: %s" % (new, e))
                continue
        ok += 1
    log_msg("[UNDO] restored=%d skip=%d fail=%d dry_run=%s" % (ok, skip, fail, dry_run))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ImageAutoRenamer: local vision model auto rename")
    parser.add_argument("--cli", metavar="PATH", help="headless mode: process a file or folder")
    parser.add_argument("--dry-run", action="store_true", help="preview only, never rename")
    parser.add_argument("--workers", type=int, default=None, help="parallel workers in CLI mode (default: config api.max_concurrency)")
    parser.add_argument("--undo", action="store_true", help="reverse rename_log.txt")
    args = parser.parse_args()

    if args.undo:
        run_undo(dry_run=args.dry_run)
        return
    if args.cli:
        run_cli(args.cli, dry_run=args.dry_run, workers=args.workers)
        return

    if tk is None:
        log_msg("[错误] tkinter 不可用，无法启动 GUI")
        sys.exit(1)
    if TkinterDnD is None:
        log_msg("[错误] 未安装 tkinterdnd2，请先执行: pip install tkinterdnd2")
        sys.exit(1)
    if Image is None:
        log_msg("[错误] 未安装 Pillow，请先执行: pip install Pillow")
        sys.exit(1)

    root = TkinterDnD.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
