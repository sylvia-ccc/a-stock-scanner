@echo off
rem A股全景扫描器 一键启动（Windows：双击本文件即可）
cd /d "%~dp0"
set PORT=8080
start "" http://localhost:%PORT%
echo A股全景扫描器: http://localhost:%PORT%  (关闭本窗口即停止)
python webapp\app.py %PORT%
pause
