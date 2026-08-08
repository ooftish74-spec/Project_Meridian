#!/bin/bash
# =================================================================
# Project Meridian — Monday 2-Stage Staged Launch Protocol
# =================================================================
# Stage 1 (08:30 KST): KIS_MODE=paper Rehearsal & IAM/Secrets Audit
# Stage 2 (08:50 KST): KIS_MODE=live Hot-Swap (Gated by Stage 1 Pass)
# =================================================================
set -e

REGION="ap-northeast-2"
GATE_FILE="/tmp/meridian_rehearsal_pass.flag"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

echo "=========================================================="
echo " 🚀 Project Meridian Monday Staged Launch Protocol"
echo "    Time: ${TIMESTAMP}"
echo "=========================================================="

ACTION=${1:-"stage1"}

if [ "${ACTION}" == "stage1" ]; then
    echo "📋 [Stage 1: 08:30 KST] Running Paper Rehearsal & IAM Audit..."
    rm -f ${GATE_FILE}

    # 1. Audit AWS Secrets Manager Read Access
    echo "  1️⃣ AWS Secrets Manager IAM Read Check..."
    if aws secretsmanager get-secret-value --secret-id Meridian --region ${REGION} > /dev/null 2>&1; then
        echo "     ✅ AWS Secrets Manager IAM Read: SUCCESS"
    else
        echo "     ❌ AWS Secrets Manager IAM Read: FAILED (Permission Denied or Secret Missing)"
        exit 1
    fi

    # 2. Run Paper Trading Rehearsal Container
    echo "  2️⃣ Running KIS_MODE=paper 1-Cycle Orchestrator Rehearsal..."
    export ENVIRONMENT=production
    export KIS_MODE=paper
    export AWS_REGION=${REGION}

    if python3 scripts/stream_orchestrator.py --once > /tmp/rehearsal.log 2>&1; then
        echo "     ✅ KIS_MODE=paper Orchestrator Rehearsal: SUCCESS"
        echo "REHEARSAL_PASS_TIMESTAMP=${TIMESTAMP}" > ${GATE_FILE}
        echo "=========================================================="
        echo " 🎉 Stage 1 PASS! System is ready for Live Switch at 08:50 KST."
        echo "=========================================================="
    else
        echo "     ❌ KIS_MODE=paper Orchestrator Rehearsal: FAILED"
        echo "     Check logs at /tmp/rehearsal.log"
        exit 1
    fi

elif [ "${ACTION}" == "stage2" ]; then
    echo "📋 [Stage 2: 08:50 KST] LIVE Hot-Swap Gate Assessment..."

    # Check Stage 1 Gate Status
    if [ ! -f "${GATE_FILE}" ]; then
        echo "🚨 [BLOCK] Stage 1 (Paper Rehearsal) was NOT passed or not executed!"
        echo "   Live trading switch is HALTED for safety."
        exit 1
    fi

    echo "  ✅ Gate Check Passed. Switching Systemd Container to KIS_MODE=live..."
    
    # Update systemd service environment or restart container in Live Mode
    systemctl stop meridian-container.service || true
    
    # Run Docker Container in LIVE Mode
    docker run -d --name meridian-live \
        --rm \
        -e ENVIRONMENT=production \
        -e KIS_MODE=live \
        -e TZ=Asia/Seoul \
        -e AWS_REGION=${REGION} \
        -v /home/ubuntu/meridian_data:/app/data \
        -v /home/ubuntu/meridian_results:/app/results \
        -v /home/ubuntu/meridian_logs:/app/logs \
        -p 8501:8501 \
        $(aws sts get-caller-identity --query Account --output text --region ${REGION}).dkr.ecr.${REGION}.amazonaws.com/project-meridian:latest \
        python scripts/stream_orchestrator.py

    echo "=========================================================="
    echo " 🔥 LIVE HOT-SWAP COMPLETE! Meridian is Live Trading for 09:00 KST Market Open."
    echo "=========================================================="

else
    echo "Usage: $0 [stage1|stage2]"
    exit 1
fi
