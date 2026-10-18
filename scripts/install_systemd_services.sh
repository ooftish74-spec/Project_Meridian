#!/bin/bash
set -euo pipefail

# AWS Migration Phase 2: systemd Installer
#
# ── Track B (Wave 1) 운영 원칙 ─────────────────────────────────────────────────
# 1) 멱등성: 각 unit 파일은 내용이 바뀐 경우에만 기록한다 (install_unit). 변경이 없으면
#    daemon-reload 도 하지 않는다 → 재실행해도 타이머 상태/스케줄에 영향 없음.
#    이미 active 인 타이머는 restart 하지 않는다 (start 는 inactive 인 경우에만).
# 2) Persistent 정책:
#    - 매매 세션 타이머(morning, premarket_calibration, closing, aftermarket, us_premarket, us_regular)
#      = Persistent=false. 서버 다운/재부팅 후 '놓친 실행'을 엉뚱한 시각에 따라잡는(catch-up) 것이
#      더 위험하다 (예: 08:45 장전 파이프라인이 14:00 에 실행). 놓친 세션은 fail-closed(미실행)가 원칙이며,
#      장중 감시는 24/7 live_trader_daemon 이 담당한다.
#    - 데이터/정비 타이머(night_futures, token_refresh, collect, evening, weekly_*, automated_audit,
#      frontier_lab) = Persistent=true 유지 (catch-up 이 유익하거나 무해).
# 3) ⚠️ 실행 중인 타이머의 OnCalendar 를 '추가/변경'하면 systemd 는 마지막 트리거(또는 활성화) 시각을
#    기준으로 다음 elapse 를 계산하므로, 그 사이에 지나간 새 시각이 있으면 즉시 1회 발화할 수 있다.
#    (운영 관측: 2026-10-19 03:27 KST, us_regular 드롭인 추가 + restart → Persistent=false 였는데도 즉시 실행.
#     당시엔 코드의 휴장/뉴욕시각 창 가드로 skip.) → 스케줄 변경은 가드가 no-op 이 되는 시간대에만 적용하고
#    적용 직후 `systemctl list-timers` + journal 로 확인할 것. 본 스크립트는 운영과 동일한 OnCalendar 를
#    유지하므로 재실행 시 스케줄 변경이 발생하지 않는다.
# 4) us_regular: 22:30:10 KST(EDT 09:30 ET) + 23:25 KST(EST 09:25 ET). 세션 판정은 코드가 뉴욕 현지 시각으로
#    수행(09:20~16:00 ET, 반일장 13:00) → 계절에 맞지 않는 트리거는 skip. 23:25 는 기존 드롭인
#    (meridian_us_regular.timer.d/10-dst-winter.conf)에서 본 unit 으로 이관 → 중복 방지를 위해 드롭인 제거.
#
# 렌더 전용 모드 (테스트/리뷰용 — sudo/systemctl 미사용):
#   MERIDIAN_SYSTEMD_RENDER_ONLY=1 MERIDIAN_SYSTEMD_DIR=/tmp/units bash scripts/install_systemd_services.sh

RENDER_ONLY="${MERIDIAN_SYSTEMD_RENDER_ONLY:-0}"
SERVICE_DIR="${MERIDIAN_SYSTEMD_DIR:-/etc/systemd/system}"
MERIDIAN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# sudo 로 실행돼도 서비스가 root 로 돌지 않도록 원 사용자 유지
USER="${SUDO_USER:-$(whoami)}"

if [ "$RENDER_ONLY" = "1" ]; then
    SUDO=""
    mkdir -p "$SERVICE_DIR"
else
    SUDO="sudo"
fi

CHANGED=0
install_unit() {
    # stdin 의 내용을 $1 에 기록. 동일하면 건드리지 않음 (멱등).
    local dst="$1" tmp
    tmp="$(mktemp)"
    cat > "$tmp"
    if $SUDO test -f "$dst" && $SUDO cmp -s "$tmp" "$dst"; then
        rm -f "$tmp"
        return 0
    fi
    $SUDO install -m 0644 "$tmp" "$dst"
    rm -f "$tmp"
    CHANGED=1
    echo "  updated: $dst"
}

echo "Installing Project Meridian systemd services to AWS EC2 (Ubuntu)..."

# 1. Night Futures Fetcher Timer (Runs at 05:10 AM KST)
install_unit "$SERVICE_DIR/meridian_night_futures.service" << SERVICE_EOF
[Unit]
Description=Meridian Night Futures Fetcher
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=$MERIDIAN_ROOT/venv/bin/python3 scripts/fetch_night_futures.py
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_night_futures.timer" << TIMER_EOF
[Unit]
Description=Meridian Night Futures Collector Timer (05:10 KST)

[Timer]
OnCalendar=*-*-* 05:10:00 Asia/Seoul
Persistent=true
Unit=meridian_night_futures.service

[Install]
WantedBy=timers.target
TIMER_EOF

# 1.4 KIS API Token Refresh Timer (Runs at 08:15 AM KST - Single Point Refresh)
#     (운영 unit 과 동일: network-online, PYTHONPATH, systemd_pipeline.log 로 출력)
install_unit "$SERVICE_DIR/meridian_token_refresh.service" << SERVICE_EOF
[Unit]
Description=Meridian KIS Token Refresh (08:15 AM KST Single-Point Refresh)
After=network-online.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
Environment="PYTHONPATH=$MERIDIAN_ROOT"
ExecStart=$MERIDIAN_ROOT/venv/bin/python3 -c "from src.execution._kis_adapter import KISTraderAdapter; KISTraderAdapter(mode=\"live\", fetch_balance_on_init=False)._request_new_token()"
StandardOutput=append:$MERIDIAN_ROOT/results/logs/systemd_pipeline.log
StandardError=append:$MERIDIAN_ROOT/results/logs/systemd_pipeline.log
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_token_refresh.timer" << TIMER_EOF
[Unit]
Description=Meridian KIS Token Pre-Warming Timer (08:15 KST)

[Timer]
OnCalendar=*-*-* 08:15:00 Asia/Seoul
Persistent=true
Unit=meridian_token_refresh.service

[Install]
WantedBy=timers.target
TIMER_EOF

# 1.5 Collect Pipeline Timer (Runs at 06:00 AM)
install_unit "$SERVICE_DIR/meridian_collect.service" << SERVICE_EOF
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

install_unit "$SERVICE_DIR/meridian_collect.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Collect Pipeline

[Timer]
OnCalendar=*-*-* 06:00:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 2. Morning Pipeline Timer (Runs at 08:45 AM) — 매매 세션: Persistent=false
install_unit "$SERVICE_DIR/meridian_morning.service" << SERVICE_EOF
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

install_unit "$SERVICE_DIR/meridian_morning.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Morning Pipeline

[Timer]
OnCalendar=*-*-* 08:45:00 Asia/Seoul
Persistent=false

[Install]
WantedBy=timers.target
TIMER_EOF

# 2.5 Premarket Calibration Timer (Runs at 08:50 AM) — 매매 세션: Persistent=false
install_unit "$SERVICE_DIR/meridian_premarket_calibration.service" << SERVICE_EOF
[Unit]
Description=Meridian Premarket Calibration
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=$MERIDIAN_ROOT/venv/bin/python3 scripts/premarket_calibration.py
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_premarket_calibration.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Premarket Calibration

[Timer]
OnCalendar=*-*-* 08:50:00 Asia/Seoul
Persistent=false

[Install]
WantedBy=timers.target
TIMER_EOF

# 3. Intraday Pipeline Daemon (Runs continuously 09:00-15:20)
#    NOTE: [Install] 섹션/타이머 없음 → enable/start 하지 않음 (운영에서도 static·미기동, 장중 감시는 live_trader_daemon)
install_unit "$SERVICE_DIR/meridian_intraday.service" << SERVICE_EOF
[Unit]
Description=Meridian Intraday Pipeline Daemon
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=$MERIDIAN_ROOT/venv/bin/python3 scripts/intraday_daemon.py
Restart=always
RestartSec=10
SERVICE_EOF


# 3.5 Closing Pipeline Timer (Runs at 15:10 PM) — 매매 세션: Persistent=false
install_unit "$SERVICE_DIR/meridian_closing.service" << SERVICE_EOF
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

install_unit "$SERVICE_DIR/meridian_closing.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Closing Pipeline

[Timer]
OnCalendar=*-*-* 15:10:00 Asia/Seoul
Persistent=false

[Install]
WantedBy=timers.target
TIMER_EOF

# 3.8 Aftermarket Pipeline Timer (Runs at 15:35 PM) — 매매 세션(시간외): Persistent=false
install_unit "$SERVICE_DIR/meridian_aftermarket.service" << SERVICE_EOF
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

install_unit "$SERVICE_DIR/meridian_aftermarket.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Aftermarket Pipeline

[Timer]
OnCalendar=*-*-* 15:35:00 Asia/Seoul
Persistent=false

[Install]
WantedBy=timers.target
TIMER_EOF

# 4. Evening Pipeline Timer (Runs at 20:30 PM)
install_unit "$SERVICE_DIR/meridian_evening.service" << SERVICE_EOF
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

install_unit "$SERVICE_DIR/meridian_evening.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Evening Pipeline

[Timer]
OnCalendar=*-*-* 20:30:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 4.1 US Premarket Pipeline Timer (Runs at 17:30 PM KST) — 매매 세션: Persistent=false
install_unit "$SERVICE_DIR/meridian_us_premarket.service" << SERVICE_EOF
[Unit]
Description=Meridian US Premarket Pipeline
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh us_premarket
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_us_premarket.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian US Premarket Pipeline

[Timer]
OnCalendar=*-*-* 17:30:00 Asia/Seoul
Persistent=false

[Install]
WantedBy=timers.target
TIMER_EOF

# 4.2 US Regular Market Pipeline Timer — 매매 세션: Persistent=false
#     22:30:10 KST = 09:30:10 EDT (서머타임, 운영 값과 동일) / 23:25 KST = 09:25 EST (표준시, 구 드롭인 이관)
install_unit "$SERVICE_DIR/meridian_us_regular.service" << SERVICE_EOF
[Unit]
Description=Meridian US Regular Market Pipeline
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh us_regular
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_us_regular.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian US Regular Market Pipeline

[Timer]
OnCalendar=*-*-* 22:30:10 Asia/Seoul
OnCalendar=*-*-* 23:25:00 Asia/Seoul
Persistent=false

[Install]
WantedBy=timers.target
TIMER_EOF

# 23:25 트리거가 본 unit 으로 이관됐으므로 구 DST 드롭인 제거 (OnCalendar 중복 방지).
LEGACY_DROPIN="$SERVICE_DIR/meridian_us_regular.timer.d/10-dst-winter.conf"
if $SUDO test -f "$LEGACY_DROPIN"; then
    $SUDO rm -f "$LEGACY_DROPIN"
    $SUDO rmdir "$SERVICE_DIR/meridian_us_regular.timer.d" 2>/dev/null || true
    CHANGED=1
    echo "  removed legacy drop-in: $LEGACY_DROPIN (23:25 now in base unit)"
fi

# 4.5 Weekly Retrain (Runs at 02:00 AM on Saturdays)
install_unit "$SERVICE_DIR/meridian_weekly_retrain.service" << SERVICE_EOF
[Unit]
Description=Meridian Weekly ML Retrain
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh weekly_retrain
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_weekly_retrain.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Weekly Retrain

[Timer]
OnCalendar=Sat *-*-* 02:00:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 4.5.5 Automated Audit Pipeline (Runs daily at 15:45 KST)
#       NOTE: 기존 동작 유지 — 아래 enable 목록에 없음 (운영에서도 disabled). 활성화는 별도 결정 사항.
install_unit "$SERVICE_DIR/meridian_automated_audit.service" << SERVICE_EOF
[Unit]
Description=Meridian Automated Operational Audit Pipeline
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=$MERIDIAN_ROOT/venv/bin/python3 scripts/run_automated_audit_pipeline.py
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_automated_audit.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Automated Audit Pipeline

[Timer]
OnCalendar=*-*-* 15:45:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 4.6 Weekly Validate (Runs at 23:00 KST on Sundays)
install_unit "$SERVICE_DIR/meridian_weekly_validate.service" << SERVICE_EOF
[Unit]
Description=Meridian Weekly Validation
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/bin/bash scripts/run_pipeline.sh weekly_validate
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_weekly_validate.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Weekly Validation & Corporate Ledger CSV

[Timer]
OnCalendar=Sun *-*-* 23:00:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF

# 4.7 Frontier Laboratory Timer (Track 2 Asynchronous Scientific Research & Genetic Evolution)
# Runs twice daily off-market: 06:15 KST (US post-market) and 16:30 KST (KRX post-market)
install_unit "$SERVICE_DIR/meridian_frontier_lab.service" << SERVICE_EOF
[Unit]
Description=Meridian Track 2 Frontier Laboratory Runner
After=network.target

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=$MERIDIAN_ROOT/venv/bin/python3 scripts/run_frontier_laboratory.py --mode daily
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_frontier_lab.timer" << TIMER_EOF
[Unit]
Description=Timer for Meridian Track 2 Frontier Laboratory

[Timer]
OnCalendar=*-*-* 06:15:00 Asia/Seoul
OnCalendar=*-*-* 16:30:00 Asia/Seoul
Persistent=true

[Install]
WantedBy=timers.target
TIMER_EOF


install_unit "$SERVICE_DIR/meridian_telegram.service" << SERVICE_EOF
[Unit]
Description=Meridian Telegram 24/7 Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=$MERIDIAN_ROOT/venv/bin/python3 scripts/run_telegram_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE_EOF

install_unit "$SERVICE_DIR/meridian_live_trader_daemon.service" << SERVICE_EOF
[Unit]
Description=Meridian 24/7 Live Trader Daemon (1-Sec Continuous Monitoring)
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$MERIDIAN_ROOT
EnvironmentFile=/home/ubuntu/Project_Meridian/.env
ExecStart=/home/ubuntu/Project_Meridian/venv/bin/python3 scripts/run_live_trader_daemon.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE_EOF

if [ "$RENDER_ONLY" = "1" ]; then
    echo "✅ render-only: units written to $SERVICE_DIR (changed=$CHANGED). No sudo/systemctl executed."
    exit 0
fi

# 5. Logrotate & Disk Protection (Caps log size at 1GB over 5 years)
if [ -d "/etc/logrotate.d" ]; then
cat << LOGROTATE_EOF | sudo tee /etc/logrotate.d/meridian > /dev/null
/home/ubuntu/Project_Meridian/logs/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    copytruncate
    maxsize 50M
}
LOGROTATE_EOF
fi

# daemon-reload 는 enable/start 보다 먼저, 그리고 unit 이 실제로 바뀐 경우에만.
if [ "$CHANGED" = "1" ]; then
    echo "Unit files changed → systemctl daemon-reload"
    sudo systemctl daemon-reload
else
    echo "No unit changes → daemon-reload skipped (idempotent re-run)"
fi

# Timers: enable (idempotent) + start only if inactive. Active timers are never restarted (see header §3).
TIMERS="meridian_token_refresh meridian_night_futures meridian_collect meridian_morning meridian_premarket_calibration meridian_closing meridian_aftermarket meridian_evening meridian_us_premarket meridian_us_regular meridian_weekly_retrain meridian_weekly_validate meridian_frontier_lab"
for t in $TIMERS; do
    if [ -f "$SERVICE_DIR/$t.timer" ]; then
        sudo systemctl enable "$t.timer" > /dev/null
        if ! systemctl is-active --quiet "$t.timer"; then
            sudo systemctl start "$t.timer"
        fi
    fi
done

# Long-running services (have [Install]); oneshot services are timer-activated and need no enable.
for s in meridian_telegram meridian_live_trader_daemon; do
    sudo systemctl enable "$s.service" > /dev/null
done
sudo systemctl start meridian_telegram.service
sudo systemctl start meridian_live_trader_daemon.service

systemctl list-timers --all --no-pager | grep meridian || true
echo "✅ systemd setup complete. 24/7 Live Trader Daemon, Logrotate, Weekly ML Retrain, and all daily pipelines are active."
