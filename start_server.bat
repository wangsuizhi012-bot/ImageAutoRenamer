@echo off
rem 手动启动 llama-server 模型服务（Qwen3-VL-8B，端口 11436）
rem 启动后可运行 rename_tool.py 直接使用，无需再点"启动模型服务"

set SERVER=E:\AI\llama.cpp-cuda\llama-server.exe
set MODEL=E:\AI\LLM\GGUF\Qwen3\Qwen3-VL-8B-Instruct-abliterated-v2.0.Q6_K.gguf
set MMPROJ=E:\AI\LLM\GGUF\Qwen3\Qwen3-VL-8B-Instruct-abliterated-v2.0.mmproj-f16.gguf

echo 正在启动 llama-server（Qwen3-VL-8B，端口 11436）...
"%SERVER%" -m "%MODEL%" --mmproj "%MMPROJ%" --port 11436 -ngl 99 -c 4096

echo.
echo 服务已退出。若需停止可关闭本窗口。
pause
