# -*- coding: utf-8 -*-
"""
rename_mcp_server.py - ImageAutoRenamer 的 MCP(Model Context Protocol) 封装

用途：让任何支持 MCP 的 Agent（Marvis / Claude Desktop / Cursor / n8n MCP 节点等）
      直接把「按图片内容自动改名」当成一个原生工具调用，无需自己拼 HTTP 请求。

传输：stdio，newline-delimited JSON-RPC 2.0（MCP 标准），零第三方依赖。

Agent 侧配置示例（Claude Desktop claude_desktop_config.json）：
{
  "mcpServers": {
    "image-auto-renamer": {
      "command": "python",
      "args": ["E:/AI/_tools/小工具脚本/ImageAutoRenamer/rename_mcp_server.py"]
    }
  }
}

暴露的工具：
  rename_images   批量识图改名
  preview_names   只预览建议名，不动文件
  describe_image  单图内容描述
  undo_rename     按日志回滚
  service_health  探活当前视觉后端
"""

import json
import os
import sys
import time
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from renamer_core import Renamer, load_config  # noqa: E402

SERVER_INFO = {"name": "image-auto-renamer", "version": "2.0"}
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "rename_images",
        "description": ("按图片内容（本地视觉模型识别）批量自动重命名图片。支持传单个文件、多个文件、"
                        "或整个文件夹（递归）。返回逐条 JSON 结果，并在 rename_log.txt 留可回滚记录。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"},
                          "description": "图片文件或文件夹的绝对路径列表"},
                "template": {"type": "string",
                             "description": "命名模板，占位符 {desc} {date} {orig}，默认 {desc}"},
                "dry_run": {"type": "boolean", "description": "true 则只预览不改名，默认 false"},
                "max_workers": {"type": "integer", "description": "并发识别线程数，默认取配置"},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "preview_names",
        "description": "只识别并返回建议的新文件名，绝不改动磁盘文件。适合让用户先确认。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {"type": "array", "items": {"type": "string"}},
                "template": {"type": "string"},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "describe_image",
        "description": "识别单张图片内容并返回一句中文描述与建议文件名。可传本地路径或 base64。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "本地图片绝对路径"},
                "image_base64": {"type": "string", "description": "图片 base64（与 path 二选一）"},
                "prompt": {"type": "string", "description": "自定义识别提示词"},
            },
        },
    },
    {
        "name": "undo_rename",
        "description": "按 rename_log.txt 逆序回滚改名操作（新名 -> 原名）。默认 dry_run 预览。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "true 只演练，默认 true"},
                "last_n": {"type": "integer", "description": "只回滚最近 N 条，0 为全部"},
            },
        },
    },
    {
        "name": "service_health",
        "description": "检查视觉推理后端是否就绪，返回当前 base_url / 模型 / 并发 / 缓存条数。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class McpServer:
    def __init__(self):
        self.renamer = None

    def get_renamer(self):
        if self.renamer is None:
            self.renamer = Renamer(load_config())
        return self.renamer

    # ---------------- 工具实现 ----------------
    def call_tool(self, name, args):
        args = args or {}
        r = self.get_renamer()
        if name in ("rename_images", "preview_names"):
            dry = True if name == "preview_names" else bool(args.get("dry_run", False))
            return r.process(args.get("paths") or [], dry_run=dry,
                             template=args.get("template"),
                             max_workers=args.get("max_workers"))
        if name == "describe_image":
            t0 = time.time()
            if args.get("image_base64"):
                b64 = args["image_base64"]
                if "," in b64[:64]:
                    b64 = b64.split(",", 1)[1]
                desc = r.client.describe(b64, prompt=args.get("prompt"))
                return {"ok": True, "desc": desc, "elapsed_ms": int((time.time() - t0) * 1000)}
            p = args.get("path")
            if not p or not os.path.isfile(p):
                return {"ok": False, "error": "path 不存在: %s" % p}
            d = r.describe_file(p, prompt=args.get("prompt"))
            d.update(ok=True, path=p, suggest=r.render_name(d["desc"], p, args.get("template")))
            return d
        if name == "undo_rename":
            return r.undo(dry_run=bool(args.get("dry_run", True)),
                          last_n=int(args.get("last_n") or 0))
        if name == "service_health":
            return {"ok": True, "upstream": r.client.health(), "info": r.info()}
        raise ValueError("未知工具: " + name)

    # ---------------- JSON-RPC ----------------
    def handle(self, msg):
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            }}
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = params.get("name")
            try:
                data = self.call_tool(name, params.get("arguments"))
                text = json.dumps(data, ensure_ascii=False, indent=2)
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                }}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "%s: %s\n%s"
                                 % (type(e).__name__, e, traceback.format_exc()[-800:])}],
                    "isError": True,
                }}
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": "Method not found: %s" % method}}


def main():
    srv = McpServer()
    stdin = sys.stdin
    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        # 兼容 LSP 风格 header（部分客户端）
        if line.lower().startswith("content-length:"):
            n = int(line.split(":", 1)[1])
            stdin.readline()  # 空行
            payload = stdin.read(n)
        else:
            payload = line
        try:
            msg = json.loads(payload)
        except Exception:
            continue
        try:
            out = srv.handle(msg)
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                   "error": {"code": -32603, "message": str(e)}}
        if out is None:
            continue
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    try:
        if srv.renamer is not None:
            srv.renamer.cache.save()
    except Exception:
        pass


if __name__ == "__main__":
    main()
