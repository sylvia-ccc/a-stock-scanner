#!/bin/bash
# A股全景扫描器 一键启动（macOS：双击本文件即可）
cd "$(dirname "$0")" || exit 1
PORT=8080
URL="http://localhost:$PORT"

# 已在运行 → 直接打开浏览器
if lsof -i :$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  open "$URL"
  exit 0
fi

# 启动服务（后台）并自动打开浏览器；关闭本终端窗口即停止服务
(sleep 2 && open "$URL") &
echo "A股全景扫描器: $URL  (关闭本窗口即停止)"
exec python3 webapp/app.py $PORT
