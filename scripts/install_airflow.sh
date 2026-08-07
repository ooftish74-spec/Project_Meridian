#!/bin/bash
set -euo pipefail

# AWS Migration Phase 3: Apache Airflow Installer

MERIDIAN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER=$(whoami)
AIRFLOW_HOME="/home/$USER/airflow"
SERVICE_DIR="/etc/systemd/system"

echo "=============================================="
echo "🌬️  Installing Apache Airflow on AWS EC2..."
echo "=============================================="

# 1. Install Airflow
source $MERIDIAN_ROOT/venv/bin/activate
pip install "apache-airflow==2.9.2" || true

# Force upgrade typing_extensions and pydantic to fix Sentinel import error
pip install --upgrade typing_extensions pydantic pydantic-core

export AIRFLOW_HOME=$AIRFLOW_HOME

# 2. Init DB and Create Admin User
airflow db migrate
airflow users create \
    --username admin \
    --firstname Admin \
    --lastname Admin \
    --role Admin \
    --email admin@meridian.com \
    --password admin || true

# 3. Create Systemd Services for Airflow Webserver and Scheduler
echo "Setting up Airflow Systemd Services..."

cat << SERVICE_EOF | sudo tee $SERVICE_DIR/airflow-webserver.service
[Unit]
Description=Airflow webserver daemon
After=network.target

[Service]
Environment="AIRFLOW_HOME=$AIRFLOW_HOME"
EnvironmentFile=/home/$USER/Project_Meridian/.env
User=$USER
Group=$USER
Type=simple
ExecStart=$MERIDIAN_ROOT/venv/bin/airflow webserver --port 8080
Restart=on-failure
RestartSec=5s
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICE_EOF

cat << SERVICE_EOF | sudo tee $SERVICE_DIR/airflow-scheduler.service
[Unit]
Description=Airflow scheduler daemon
After=network.target

[Service]
Environment="AIRFLOW_HOME=$AIRFLOW_HOME"
EnvironmentFile=/home/$USER/Project_Meridian/.env
User=$USER
Group=$USER
Type=simple
ExecStart=$MERIDIAN_ROOT/venv/bin/airflow scheduler
Restart=always
RestartSec=5s
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# 4. Create symlink for DAGs so Airflow can see meridian_daily_dag.py
mkdir -p $AIRFLOW_HOME/dags
mkdir -p $MERIDIAN_ROOT/dags
ln -sf $MERIDIAN_ROOT/dags/meridian_daily_dag.py $AIRFLOW_HOME/dags/meridian_daily_dag.py

# 5. Enable and start
sudo systemctl daemon-reload
sudo systemctl enable airflow-webserver.service
sudo systemctl enable airflow-scheduler.service

sudo systemctl restart airflow-webserver.service
sudo systemctl restart airflow-scheduler.service

echo "✅ Apache Airflow setup complete."
echo "Web UI available at http://<EC2_IP>:8080 (Login: admin / admin)"
