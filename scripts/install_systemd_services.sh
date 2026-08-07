#!/bin/bash
set -euo pipefail

# AWS Migration Phase 2: systemd Installer

SERVICE_DIR="/etc/systemd/system"
MERIDIAN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER=$(whoami)

echo "Installing Project Meridian systemd services to AWS EC2 (Ubuntu)..."

# 1. Night Futures Fetcher Timer (Runs at 05:30 AM)
cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_night_futures.service
[Unit]
Description=Meridian Night Futures Fetcher
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/usr/bin/python3 scripts/fetch_night_futures.py
SERVICE_EOF

cat << TIMER_EOF | sudo tee $SERVICE_DIR/meridian_night_futures.timer
[Unit]
Description=Timer for Meridian Night Futures Fetcher

[Timer]
OnCalendar=*-*-* 06:05:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 1.5 Collect Pipeline Timer (Runs at 06:00 AM)
cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_collect.service
[Unit]
Description=Meridian Collect Pipeline
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh collect
SERVICE_EOF

cat << TIMER_EOF | sudo tee $SERVICE_DIR/meridian_collect.timer
[Unit]
Description=Timer for Meridian Collect Pipeline

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 2. Morning Pipeline Timer (Runs at 07:50 AM)
cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_morning.service
[Unit]
Description=Meridian Morning Pipeline
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh morning
SERVICE_EOF

cat << TIMER_EOF | sudo tee $SERVICE_DIR/meridian_morning.timer
[Unit]
Description=Timer for Meridian Morning Pipeline

[Timer]
OnCalendar=*-*-* 07:50:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 2.5 Premarket Calibration Timer (Runs at 08:50 AM)
cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_premarket_calibration.service
[Unit]
Description=Meridian Premarket Calibration
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/usr/bin/python3 scripts/premarket_calibration.py
SERVICE_EOF

cat << TIMER_EOF | sudo tee $SERVICE_DIR/meridian_premarket_calibration.timer
[Unit]
Description=Timer for Meridian Premarket Calibration

[Timer]
OnCalendar=*-*-* 08:50:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 3. Intraday Pipeline Daemon (Runs continuously 09:00-15:20)
cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_intraday.service
[Unit]
Description=Meridian Intraday Pipeline Daemon
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/usr/bin/python3 scripts/intraday_daemon.py
Restart=always
RestartSec=10
SERVICE_EOF


# 3.5 Closing Pipeline Timer (Runs at 15:10 PM)
cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_closing.service
[Unit]
Description=Meridian Closing Pipeline
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh closing
SERVICE_EOF

cat << TIMER_EOF | sudo tee $SERVICE_DIR/meridian_closing.timer
[Unit]
Description=Timer for Meridian Closing Pipeline

[Timer]
OnCalendar=*-*-* 15:10:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 3.8 Aftermarket Pipeline Timer (Runs at 15:35 PM)
cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_aftermarket.service
[Unit]
Description=Meridian Aftermarket Pipeline
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh aftermarket
SERVICE_EOF

cat << TIMER_EOF | sudo tee $SERVICE_DIR/meridian_aftermarket.timer
[Unit]
Description=Timer for Meridian Aftermarket Pipeline

[Timer]
OnCalendar=*-*-* 15:35:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 4. Evening Pipeline Timer (Runs at 20:00 PM)
cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_evening.service
[Unit]
Description=Meridian Evening Pipeline
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh evening
SERVICE_EOF

cat << TIMER_EOF | sudo tee $SERVICE_DIR/meridian_evening.timer
[Unit]
Description=Timer for Meridian Evening Pipeline

[Timer]
OnCalendar=*-*-* 20:30:00
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_night_monitor.service
[Unit]
Description=Meridian KIS Night Futures Websocket Monitor
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/home/ubuntu/Project_Meridian/venv/bin/python3 src/data/night_futures_monitor.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE_EOF

cat << SERVICE_EOF | sudo tee $SERVICE_DIR/meridian_telegram.service
[Unit]
Description=Meridian Telegram 24/7 Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/usr/bin/python3 scripts/run_telegram_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# Enable all services (including the bot and monitor)
for service in meridian_night_futures meridian_collect meridian_morning meridian_premarket_calibration meridian_intraday meridian_closing meridian_aftermarket meridian_evening meridian_telegram meridian_night_monitor; do
    if [ -f "$SERVICE_DIR/$service.service" ]; then
        sudo systemctl enable $service.service
    fi
    if [ -f "$SERVICE_DIR/$service.timer" ]; then
        sudo systemctl enable $service.timer
        sudo systemctl start $service.timer
    fi
done

sudo systemctl daemon-reload
sudo systemctl start meridian_telegram.service
sudo systemctl start meridian_night_monitor.service
sudo systemctl start meridian_intraday.service

echo "✅ systemd setup complete. All daily pipelines are scheduled and Telegram Bot is running."
