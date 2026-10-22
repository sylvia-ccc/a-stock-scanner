#!/usr/bin/env bash
# A股全景扫描器 服务器一键部署 (systemd 守护)
# 用法:
#   sudo bash deploy/deploy.sh            # 反代模式: 仅监听 127.0.0.1, 由 Nginx/Caddy 对外
#   sudo bash deploy/deploy.sh public     # 直接暴露: 监听 0.0.0.0:$PORT (记得放行安全组)
# 环境变量: PORT (默认 8080)
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR=$(pwd)
PORT="${PORT:-8080}"
MODE="${1:-nginx}"

command -v python3 >/dev/null 2>&1 || { echo "✗ 未找到 python3, 请先安装 (apt install python3 / dnf install python3)"; exit 1; }
command -v systemctl >/dev/null 2>&1 || { echo "✗ 需要 systemd (Ubuntu/Debian/CentOS/阿里云默认均有)"; exit 1; }

if [ "$MODE" = "public" ]; then BIND="0.0.0.0"; else BIND="127.0.0.1"; fi

sudo tee /etc/systemd/system/a-stock-scanner.service >/dev/null <<EOF
[Unit]
Description=A-Stock Scanner (webapp)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=PORT=${PORT}
Environment=BIND=${BIND}
ExecStart=$(command -v python3) ${APP_DIR}/webapp/app.py
Restart=always
RestartSec=3
# 安全基线
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now a-stock-scanner
sleep 1
sudo systemctl --no-pager --lines=3 status a-stock-scanner || true

echo
echo "✅ 部署完成 (模式: ${MODE}, 端口: ${PORT})"
echo "   常用命令:"
echo "     sudo systemctl restart a-stock-scanner   # 重启(更新代码后)"
echo "     sudo journalctl -u a-stock-scanner -f    # 看日志"
if [ "$MODE" = "public" ]; then
  echo "   ⚠ 直接暴露模式: 请确认云安全组/防火墙已放行 ${PORT}/tcp"
  echo "     强烈建议在前面加一层口令保护 (见 deploy/nginx.conf.example 的 auth_basic 段)"
else
  echo "   服务仅监听 127.0.0.1:${PORT}, 请用 Nginx/Caddy 反代对外 (样例: deploy/nginx.conf.example)"
fi
