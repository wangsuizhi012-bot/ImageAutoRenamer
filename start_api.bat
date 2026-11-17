@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================
echo  ImageAutoRenamer API  -  http://127.0.0.1:8787
echo  上游视觉接口请在 config.json 的 api 段填写
echo  关闭本窗口即停止服务
echo ================================================
python rename_api.py %*
if errorlevel 1 (
  echo.
  echo [启动失败] 请确认 python 已加入 PATH，或改用完整路径调用
)
pause
