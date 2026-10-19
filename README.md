---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: b8aef31cb78d91d0ecd6b8d34638f57d_df1df739c86a11f1acd7525400cd780f
    ReservedCode1: 5fCiaIj32tAhhQGRdWIHLNqohl+coZR5apAZYYm7Jn9FspUoQk9ES1FC6LfFsGYaRtv8ya22Zmp7uB25Z0PfYclbu+1RodcVpaoIg/LdJIKzyf5jGg+ln88xNnKtfGnh7KCALzZdQPbg98rPzgXZkfO9WYZF0ircHKd6Ogs+T/MZ4HnLB5FLXjCcfmQ=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: b8aef31cb78d91d0ecd6b8d34638f57d_df1df739c86a11f1acd7525400cd780f
    ReservedCode2: 5fCiaIj32tAhhQGRdWIHLNqohl+coZR5apAZYYm7Jn9FspUoQk9ES1FC6LfFsGYaRtv8ya22Zmp7uB25Z0PfYclbu+1RodcVpaoIg/LdJIKzyf5jGg+ln88xNnKtfGnh7KCALzZdQPbg98rPzgXZkfO9WYZF0ircHKd6Ogs+T/MZ4HnLB5FLXjCcfmQ=
---

# ImageAutoRenamer —— 拖图识图自动改名（本地免费轻量）

把图片或文件夹拖进窗口，程序调用**本机**视觉大模型（llama.cpp + Qwen3-VL-8B）识别图片内容，自动把文件改名为内容描述。全程本地推理，**无需联网、无需付费、无需打开 ComfyUI**。

## 一、环境要求

- Windows 10/11
- Python 3.11（已验证 3.11.8）
- 依赖库（已安装则跳过）：
  ```
  pip install tkinterdnd2 requests Pillow
  ```
- llama.cpp CUDA 构建：`E:\AI\llama.cpp-cuda\llama-server.exe`（build 10360）
- 视觉模型：`E:\AI\LLM\GGUF\Qwen3\Qwen3-VL-8B-Instruct-abliterated-v2.0.Q6_K.gguf` + 同名 `.mmproj-f16.gguf`

## 二、使用步骤

1. **（可选）手动启动模型服务**：双击 `start_server.bat`，等待输出显示模型加载完成（加载约 1~3 分钟）。
   也可以不手动启动，直接运行工具后点击窗口内 **「启动模型服务」** 按钮，程序会自动拉起服务并等待就绪。
2. **运行工具**：
   ```
   python E:\AI\工具\小工具脚本\ImageAutoRenamer\rename_tool.py
   ```
3. **拖入图片**：支持拖入单张图片（jpg/jpeg/png/bmp/webp）或整个文件夹（文件夹会递归处理其中所有支持的图片）。
4. 程序逐张识别并显示 **原名 → 新名** 预览，确认无误后点击 **「全部改名」**（或点「跳过」放弃本次改名）。
5. 改名记录（原名绝对路径 → 新名绝对路径）会追加写入 `E:\AI\工具\小工具脚本\ImageAutoRenamer\rename_log.txt`，便于找回。

## 三、配置说明（config.json）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| model | Qwen3-VL-8B Q6_K.gguf | 视觉主模型 |
| mmproj | Qwen3-VL-8B mmproj-f16.gguf | 视觉投影文件 |
| port | 11436 | llama-server 端口 |
| ngl / ctx | 99 / 4096 | GPU 层数 / 上下文长度 |
| max_tokens / temperature | 64 / 0.1 | 识别描述生成参数 |
| max_side | 1024 | 图片最长边压缩阈值（先转 JPEG 再送入模型） |
| max_name_len | 60 | 改名最大长度（超出截断） |
| log_file | rename_log.txt | 改名记录文件 |

## 四、备选后端切换（8B 加载失败/超时时）

若 Qwen3-VL-8B 因显存不足或加载超时失败，可切换为更轻量的 **MiniCPM5-2B**。两种方式任选：

**方式 A（命令行一次性测试）**：
```
E:\AI\llama.cpp-cuda\llama-minicpmv-cli.exe -m E:\AI\LLM\GGUF\MiniCPM5-2B\MiniCPM5-2B-Q4_K_M.gguf --image <图片路径> -p "用一句简短中文描述这张图片的主要内容" --temp 0.1 -n 64
```

**方式 B（接入本工具的 API 模式）**：llama-minicpmv-cli 也支持 `--api-server 11436` 启动 HTTP 服务（端口与主配置一致即可），启动命令示例：
```
E:\AI\llama.cpp-cuda\llama-minicpmv-cli.exe -m E:\AI\LLM\GGUF\MiniCPM5-2B\MiniCPM5-2B-Q4_K_M.gguf --api-server 11436 --temp 0.1 -n 64
```
启动后在 `rename_tool.py` 界面点「检查服务状态」确认就绪即可直接使用。

> 说明：备选后端只需替换"服务进程"，工具的 API 调用方式（`/v1/chat/completions` + data URL 图片）保持不变。

## 五、常见问题

- **提示 tkinterdnd2 未安装 / 拖拽不可用**：执行 `pip install tkinterdnd2` 后重启工具。
- **模型加载失败（进程提前退出）**：多为显存不足，改用备选后端 MiniCPM5-2B（见上）。
- **识别内容不准确**：可调整 config.json 的 `max_tokens`（描述更长）或 `temperature`（更低更稳定）。
- **改名后想找回原文件**：打开 `rename_log.txt`，按新名搜索对应行即可得到原路径。

## 六、文件清单

| 文件 | 作用 |
|---|---|
| rename_tool.py | 主程序（拖拽、识图、改名、服务管理） |
| config.json | 配置（模型路径、端口、识别参数） |
| start_server.bat | 手动启动 llama-server 的快捷脚本 |
| rename_log.txt | 改名记录（运行时自动生成/追加） |
*（内容由AI生成，仅供参考）*
