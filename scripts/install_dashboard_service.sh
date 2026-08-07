#!/usr/bin/env bash
# Install Streamlit Dashboard as a systemd service (Bound to localhost:8501 for SSH tunneling)

set -euo pipefail

# Define variables
PROJECT_ROOT="/home/ubuntu/Project_Meridian"
VENV_PYTHON="${PROJECT_ROOT}/venv/bin/python3"
STREAMLIT_BIN="${PROJECT_ROOT}/venv/bin/streamlit"
SERVICE_NAME="meridian_dashboard"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo "=========================================================="
echo "🚀 Installing ${SERVICE_NAME} Service (SSH Tunneling Only)"
echo "=========================================================="

cat << EOF | sudo tee "${SERVICE_FILE}" > /dev/null
[Unit]
Description=Project Meridian Streamlit Dashboard (V3)
After=network.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=${PROJECT_ROOT}
Environment="PATH=${PROJECT_ROOT}/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="KIS_MODE=live"
# Run streamlit bound only to localhost (127.0.0.1) for zero hacking risk.
ExecStart=${STREAMLIT_BIN} run dashboard/app.py --server.port 8501 --server.address 127.0.0.1 --server.headless true
Restart=always
RestartSec=5
StandardOutput=append:${PROJECT_ROOT}/logs/dashboard_stdout.log
StandardError=append:${PROJECT_ROOT}/logs/dashboard_stderr.log

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd..."
sudo systemctl daemon-reload

echo "Enabling and starting ${SERVICE_NAME}..."
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sudo systemctl status "${SERVICE_NAME}" --no-pager

echo "✅ Dashboard service installed successfully."
echo "🔐 SSH Tunneling command: ssh -i ~/.ssh/meridian-key.pem -L 8501:localhost:8501 ubuntu@<EC2-IP>"
