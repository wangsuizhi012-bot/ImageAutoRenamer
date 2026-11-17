---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: b8aef31cb78d91d0ecd6b8d34638f57d_3262c734d9c911f1a0ca525400de85a5
    ReservedCode1: xrU9Q0O4YbskqOdphrdCMXbG/SW8RIyCBFbg1BlNeV1nfZL+SuJs2JBXHpcxVYxbvAywBxwgLjQns2V6+Je0zjFw6IvdkoaJyqTyORRzsb3K6a255YBkzhrVipziKGefGq8q3zV9B8hrgWuOUWAs839PeFKV5hCnZt6aEO/N5wn3XD/fTIi+OqwNkl4=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: b8aef31cb78d91d0ecd6b8d34638f57d_3262c734d9c911f1a0ca525400de85a5
    ReservedCode2: xrU9Q0O4YbskqOdphrdCMXbG/SW8RIyCBFbg1BlNeV1nfZL+SuJs2JBXHpcxVYxbvAywBxwgLjQns2V6+Je0zjFw6IvdkoaJyqTyORRzsb3K6a255YBkzhrVipziKGefGq8q3zV9B8hrgWuOUWAs839PeFKV5hCnZt6aEO/N5wn3XD/fTIi+OqwNkl4=
---

# ImageAutoRenamer 2.0 — 接口化改造说明

原有 GUI 工具改造为「一套核心 + 四种调用方式」：GUI 照旧用，命令行可批处理，HTTP API 给任意程序调，MCP 给 Agent 直接调。

## 1. 文件构成

| 文件 | 作用 |
|---|---|
| `renamer_core.py` | **唯一核心库**：配置、图像编码、内容缓存、视觉客户端、批量改名/回滚（零第三方依赖，仅需 Pillow） |
| `rename_tool.py` | 原 GUI 程序，已改为调用核心库（界面不变，识别改并发 + 走缓存） |
| `rename_api.py` | HTTP API 服务，标准库 `http.server` 实现，无 fastapi/uvicorn 依赖 |
| `rename_mcp_server.py` | MCP stdio 服务，供 Claude/Cursor/任意 Agent 直接当工具调用 |
| `config.json` | 配置，新增 `api` 段（**上游 API 在这里填**） |
| `cache/desc_cache.json` | 识别结果缓存（按图片内容 sha1，同一张图永不重复识别） |
| `rename_log.txt` | 改名日志（原有格式，未改） |

## 2. 你要填的配置（config.json → api 段）

```json
"api": {
  "base_url": "",          // 留空 = 自动用本地 llama-server http://127.0.0.1:11436/v1
  "api_key": "",           // 填则自动加 Authorization: Bearer <key>
  "model": "",             // 留空 = qwen3-vl；填云端模型名如 gpt-4o-mini / qwen-vl-max
  "timeout": 90,           // 单次请求超时秒
  "retry": 1,              // 失败重试次数
  "max_concurrency": 4,    // 并发识别数（本地 8G 显存建议 2~4，云端可开 8~16）
  "max_side": 768,         // 送模型的图长边（越小越快，768 够描述用途）
  "jpeg_quality": 80,
  "max_tokens": 64,
  "temperature": 0.1,
  "name_template": "{desc}",   // 支持 {desc} {date} {time} {seq} {original}
  "prompt": "",            // 留空用内置中文描述提示词
  "cache_file": "cache/desc_cache.json",
  "serve_host": "127.0.0.1",
  "serve_port": 8787,
  "serve_token": ""        // 非空则 HTTP API 需带 Bearer token
}
```

要点：`base_url` 填任意 **OpenAI 兼容** 的 `/v1` 地址即可（llama-server、vLLM、LM Studio、Ollama 的 /v1、阿里/火山/硅基流动等）。填了 base_url 就不再走高硬编码本地地址。

## 3. 四种用法

### 3.1 GUI（原样）
```
python rename_tool.py
```
拖拽图片/文件夹即可；批量识别已是并发，第二批开始的重复图片命中缓存直接出结果。

### 3.2 命令行
```bat
python rename_tool.py --cli "D:\图片目录" --dry-run          :: 只看结果不改名
python rename_tool.py --cli "D:\图片目录" --workers 4         :: 并发改名
python rename_tool.py --undo                                :: 按日志回滚
```

### 3.3 HTTP API
```bat
python rename_api.py                        :: 127.0.0.1:8787
python rename_api.py --port 9000 --token 你的口令
start_api.bat                               :: 双击启动（等同上面默认参数）
```
接口（全 JSON，已开 CORS）：

| 方法 | 路径 | 请求体 | 说明 |
|---|---|---|---|
| GET | `/` | - | 服务自描述 + 接口清单，Agent 可自动发现 |
| GET | `/health` | - | 上游视觉后端探活 |
| GET | `/v1/info` | - | 当前配置（api_key 脱敏） |
| POST | `/v1/describe` | `{"paths":["D:/a.jpg"]}` 或 `{"image_base64":"…"}` | 返回图片内容描述 |
| POST | `/v1/preview` | `{"paths":["D:/dir"],"template":"{desc}"}` | 只算新名字，不落盘 |
| POST | `/v1/rename` | `{"paths":["D:/dir"],"dry_run":false,"max_workers":4}` | 实际改名，逐条返回结果 |
| POST | `/v1/undo` | `{"dry_run":true,"last_n":0}` | 按日志逆序回滚 |
| POST | `/v1/upload` | `{"filename":"a.jpg","image_base64":"…","apply":false}` | 远程图片落地识别（Agent 传图用） |

调用示例（PowerShell / cmd 均可）：

```bat
curl -s http://127.0.0.1:8787/health
curl -s -X POST http://127.0.0.1:8787/v1/preview -H "Content-Type: application/json" -d "{\"paths\":[\"D:/图片\"]}"
```

Python：

```python
import requests, json
r = requests.post("http://127.0.0.1:8787/v1/rename",
                  json={"paths": ["D:/图片"], "dry_run": False},
                  headers={"Authorization": "Bearer 你的口令"}, timeout=600)
print(json.dumps(r.json(), ensure_ascii=False, indent=2))
```

### 3.4 MCP（给 Agent 用，推荐）
服务名 `image-auto-renamer`，stdio 传输。客户端配置（Claude Desktop / Cursor / 自研 Agent 通用）：

```json
{
  "mcpServers": {
    "image-auto-renamer": {
      "command": "python",
      "args": ["E:\\AI\\_tools\\小工具脚本\\ImageAutoRenamer\\rename_mcp_server.py"],
      "env": { "PYTHONIOENCODING": "utf-8", "IA_RENAMER_CONFIG": "E:\\AI\\_tools\\小工具脚本\\ImageAutoRenamer\\config.json" }
    }
  }
}
```

暴露的工具：

| 工具 | 参数 | 说明 |
|---|---|---|
| `rename_images` | `paths`(list, 必填), `dry_run`(bool, 默认 true), `template` | 批量改名，默认只预览 |
| `preview_names` | `paths`, `template` | 只算新名字 |
| `describe_image` | `paths` | 返回内容描述 |
| `undo_rename` | `dry_run`(默认 true), `last_n` | 回滚 |
| `service_health` | - | 探活 |

> `IA_RENAMER_CONFIG` 可指向另一份 config（多套上游切换），不设则用同目录 config.json。

## 4. 提速点（相对原版）

| 环节 | 原版 | 现在 |
|---|---|---|
| 批量识别 | 逐张串行 | `ThreadPoolExecutor` 并发（默认 4，可调） |
| 重复图片 | 每次重新推理 | 内容 sha1 缓存命中，实测 350ms → 5ms/张，自动落盘（30s 自动保存 + 每批结束保存） |
| 送模型体积 | 长边 1024 | 长边 768（`max_side` 可调），JPEG draft 降采样解码，编码耗时下降数倍 |
| HTTP 连接 | 每次新建 | socket 复用连接，上游 keep-alive |
| 缓存命中时 | - | 完全跳过解码与推理，只读文件头算 sha1 |
| 进程启动 | - | API/MCP 零第三方依赖，`requests`/`fastapi` 都不需要装 |

## 5. 安全与回滚

- 默认只绑定 `127.0.0.1`；要局域网/别的机器调用才改 `--host 0.0.0.0`，并配上 `serve_token`。
- `/v1/rename` 支持 `dry_run`，Agent 建议先 preview 再 rename。
- 所有改名写入 `rename_log.txt`（原格式），`/v1/undo`、CLI `--undo`、GUI 撤销三者共用同一日志，互不冲突。
- 同名冲突自动加 `_1`/`_2`；同批次内先预览再改名的占位会被正确释放，不会出现莫名递增后缀。

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `/health` 返回 ok=false | 上游没起或地址不对：本地 llama-server 需在 11436 就绪，或把 `base_url` 填成你的云端地址 |
| 401 | `serve_token` 非空但请求没带 `Authorization: Bearer <token>` 或 `X-API-Key` |
| 识别很慢 | `max_concurrency` 调大（云端）/ 调小（本地显存不足），或降低 `max_side` |
| 名字重复带 `_1` | 同描述撞名，属正常唯一化；可把 `name_template` 改成 `{date}_{time}_{desc}` 之类 |
| 想换模型 | 只改 `api.model`；换模型后缓存键含模型名，不会串结果 |
*（内容由AI生成，仅供参考）*
