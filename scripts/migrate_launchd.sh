#!/bin/bash
# ═══════════════════════════════════════════════════════════
# Project Meridian — launchd Migration Script
# ═══════════════════════════════════════════════════════════
# 1. Legacy Project-A + First plist 일괄 unload + 백업
# 2. Meridian plist 심링크 생성 + load
# ═══════════════════════════════════════════════════════════

set -euo pipefail

LAUNCH_DIR="$HOME/Library/LaunchAgents"
MERIDIAN_PLIST_DIR="$(cd "$(dirname "$0")/../config/launchd" && pwd)"
BACKUP_DIR="$LAUNCH_DIR/_legacy_project_a"

echo "════════════════════════════════════════════"
echo "  Project Meridian — launchd Migration"
echo "════════════════════════════════════════════"
echo ""

# ── Step 1: Legacy unload + backup ──
echo "=== Step 1: Legacy plist 비활성화 ==="
mkdir -p "$BACKUP_DIR"

LEGACY_COUNT=0
for plist in "$LAUNCH_DIR"/com.project-a.*.plist \
             "$LAUNCH_DIR"/com.projecta.*.plist \
             "$LAUNCH_DIR"/com.projectfirst.*.plist; do
    [ -f "$plist" ] || continue
    name=$(basename "$plist")

    # Skip already disabled
    if [[ "$name" == _disabled_* ]]; then
        echo "  ⏭️  $name (already disabled)"
        continue
    fi

    # Unload
    label=$(echo "$name" | sed 's/\.plist$//')
    launchctl unload "$plist" 2>/dev/null || true
    echo "  ❌ Unloaded: $label"

    # Move to backup
    mv "$plist" "$BACKUP_DIR/$name"
    LEGACY_COUNT=$((LEGACY_COUNT + 1))
done

echo "  ✅ $LEGACY_COUNT legacy plist 비활성화 완료"
echo ""

# ── Step 2: Meridian plist 활성화 ──
echo "=== Step 2: Meridian plist 활성화 ==="

MERIDIAN_COUNT=0
for plist in "$MERIDIAN_PLIST_DIR"/*.plist; do
    [ -f "$plist" ] || continue
    name=$(basename "$plist")
    target="$LAUNCH_DIR/$name"

    # Remove old symlink if exists
    [ -L "$target" ] && rm "$target"
    [ -f "$target" ] && rm "$target"

    # Create symlink
    ln -s "$plist" "$target"

    # Load
    label=$(echo "$name" | sed 's/\.plist$//')
    launchctl load "$target" 2>/dev/null || true
    echo "  ✅ Loaded: $label"
    MERIDIAN_COUNT=$((MERIDIAN_COUNT + 1))
done

echo "  ✅ $MERIDIAN_COUNT Meridian plist 활성화 완료"
echo ""

# ── Step 3: 검증 ──
echo "=== Step 3: 검증 ==="
echo "  로드된 Meridian 작업:"
launchctl list 2>/dev/null | grep "com.meridian" | while read pid status label; do
    echo "    ✅ $label"
done

echo ""
echo "  남은 Legacy 작업:"
REMAINING=$(launchctl list 2>/dev/null | grep -c "com.project[af]" || true)
if [ "$REMAINING" -eq 0 ]; then
    echo "    ✅ 모든 legacy 작업 제거됨"
else
    echo "    ⚠️  $REMAINING개 legacy 작업 잔존"
    launchctl list 2>/dev/null | grep "com.project[af]" | while read pid status label; do
        echo "      - $label"
    done
fi

echo ""
echo "════════════════════════════════════════════"
echo "  Migration 완료"
echo "  Legacy: $LEGACY_COUNT 비활성화"
echo "  Meridian: $MERIDIAN_COUNT 활성화"
echo "  백업 위치: $BACKUP_DIR/"
echo "════════════════════════════════════════════"
