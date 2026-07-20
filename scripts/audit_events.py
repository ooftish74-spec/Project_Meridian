#!/usr/bin/env python3
"""
Comprehensive Event Audit Script
=================================
현재 시스템이 인식하고 있는 모든 동적 이벤트(매크로, 뉴스, 장중 리스크)를
종합하여 리포팅하고 검증하는 유틸리티입니다.

Usage:
    python3 scripts/audit_events.py
    python3 scripts/audit_events.py --date 2026-06-19   # 특정 날짜 감사
"""
import sys
from pathlib import Path
from datetime import date, datetime
import json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from src.intelligence.event_calendar import EventCalendar


def run_event_audit(target_date: str = None):
    print("=" * 65)
    print(" 🔍 COMPREHENSIVE EVENT AUDIT REPORT")
    print("=" * 65)

    calendar = EventCalendar()
    today_str = target_date or date.today().isoformat()

    print(f"\n📅 기준일: {today_str}\n")

    # ──────────────────────────────────────────────────────────────────
    # 1. 오늘 활성화된 모든 이벤트 (거시/미시 포함)
    # ──────────────────────────────────────────────────────────────────
    all_events  = calendar.get_events(today_str, macro_only=False)
    macro_events = calendar.get_events(today_str, macro_only=True)

    macro_ids = {e['id'] for e in macro_events}

    print("=" * 65)
    print(" 📋 ACTIVE EVENTS  (Macro O = ML Confidence 반영 / X = 종목 필터만)")
    print("=" * 65)

    if not all_events:
        print("  ✅ 현재 인식된 활성 이벤트가 없습니다. (Risk-On 상태 유지)")
    else:
        print(f"  총 {len(all_events)}개 이벤트 감지 "
              f"(Macro={len(macro_events)}건 / Micro={len(all_events)-len(macro_events)}건)\n")
        print(f"  {'Type':<14} | {'Tier':<4} | {'Macro':<5} | {'Conf↓':>6} | Description")
        print("  " + "-" * 72)

        for ev in sorted(all_events, key=lambda x: (x.get('tier', 3), x.get('type', ''))):
            ev_type  = ev.get('type', 'unknown')[:12]
            tier     = ev.get('tier', '?')
            is_macro = "✅ O" if ev['id'] in macro_ids else " ✗ X"
            conf_red = f"-{ev.get('confidence_reduction', 0) * 100:.0f}%"
            source   = ev.get('source', '')
            desc     = ev.get('name', '')[:42]
            src_tag  = f"[{source}]" if source else ""

            print(f"  {ev_type:<14} | T{tier:<3} | {is_macro:<5} | {conf_red:>6} | {desc} {src_tag}")

    # ──────────────────────────────────────────────────────────────────
    # 2. ML 모델 파라미터 — get_features() macro_only=True 결과
    # ──────────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(" ⚙️  SYSTEM ML FEATURES  (macro_only=True 적용 후)")
    print("=" * 65)
    features = calendar.get_features(today_str)

    ev_count   = features.get('event_count', 0)
    conf_drop  = features.get('event_confidence_reduction', 0) * 100
    t1_hours   = features.get('event_hours_until_t1', 999)
    ev_type_f  = features.get('event_type', 'none')
    is_today   = features.get('event_is_today', False)

    print(f"  - 거시 이벤트 개수           : {ev_count}건")
    print(f"  - ML Confidence 최대 하락폭  : -{conf_drop:.0f}%")
    print(f"  - 당일 이벤트 존재 여부       : {'⚠️  예' if is_today else '없음'}")
    print(f"  - Tier 1 이벤트까지 거리      : {t1_hours}시간 후")
    print(f"  - 우선 이벤트 유형            : {ev_type_f}")

    # ── 검증: Macro 이벤트만 confidence_reduction에 반영됐는지 교차 확인
    macro_max_cr = (max((e.get('confidence_reduction', 0) for e in macro_events), default=0)
                    if macro_events else 0)
    all_max_cr   = (max((e.get('confidence_reduction', 0) for e in all_events), default=0)
                    if all_events else 0)

    print()
    if abs(macro_max_cr - features.get('event_confidence_reduction', 0)) < 0.001:
        print("  ✅ 검증 통과: get_features()가 Macro 이벤트 기준으로만 Confidence 산출")
    else:
        print(f"  ⚠️  불일치: features={features.get('event_confidence_reduction', 0):.2f} "
              f"vs macro_max={macro_max_cr:.2f}")

    if all_max_cr > macro_max_cr:
        micro_only = [e for e in all_events if e['id'] not in macro_ids]
        print(f"  ✅ 검증 통과: Micro 이벤트 {len(micro_only)}건이 ML Confidence에서 차단됨")
    elif not all_events:
        print("  ℹ️  이벤트 없음 — 필터 검증 불필요")
    else:
        print("  ℹ️  Micro 이벤트 없음 또는 동일 감소율 — 차단 동작 문제 없음")

    # ──────────────────────────────────────────────────────────────────
    # 3. 장중 / 뉴스 동적 리스크 (dynamic_events.json)
    # ──────────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(" 🚨 DYNAMIC INTRADAY / NEWS RISKS  (최근 24h)")
    print("=" * 65)

    dyn_file = PROJECT_ROOT / 'results' / 'dynamic_events.json'
    if dyn_file.exists():
        try:
            raw = json.loads(dyn_file.read_text())
            if not isinstance(raw, list):
                raw = [raw]
            now = datetime.now()
            valid_events = 0
            for ev in raw:
                try:
                    detected   = datetime.fromisoformat(ev.get('detected_at', '2000-01-01'))
                    hours_ago  = (now - detected).total_seconds() / 3600
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    continue
                if hours_ago > 24:
                    continue
                valid_events += 1
                ev_type_n  = ev.get('type', 'NEWS')
                tier_n     = ev.get('tier', 2)
                headline   = ev.get('headline', '(제목 없음)')[:50]
                # macro_only 필터 통과 여부 판단
                MACRO_TYPES = {'geopolitical', 'monetary', 'policy', 'economic'}
                ev_event_type = ev.get('event_type', 'news')
                is_macro_n = ev_event_type in MACRO_TYPES
                macro_tag  = "Macro=O" if is_macro_n else "Macro=X"
                print(f"  [{ev_type_n:<12}] T{tier_n} | {macro_tag} | {headline} ({hours_ago:.1f}h ago)")

            if valid_events == 0:
                print("  - 최근 24시간 내 유효한 뉴스 돌발 리스크 없음")
        except Exception as e:
            print(f"  ⚠️  dynamic_events.json 파싱 에러: {e}")
    else:
        print("  - dynamic_events.json 없음 (뉴스 수집 전 또는 오늘 미실행)")

    # ──────────────────────────────────────────────────────────────────
    # 4. 향후 7일 Macro 이벤트 프리뷰
    # ──────────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(" 📆 UPCOMING MACRO EVENTS (향후 7일 프리뷰)")
    print("=" * 65)
    from datetime import timedelta
    base = date.fromisoformat(today_str)
    upcoming = []
    for delta in range(1, 8):
        future_str = (base + timedelta(days=delta)).isoformat()
        future_macro = calendar.get_events(future_str, macro_only=True)
        for ev in future_macro:
            upcoming.append((future_str, delta, ev))

    if not upcoming:
        print("  - 향후 7일간 등록된 거시 이벤트 없음")
    else:
        print(f"  {'날짜':<12} | {'D+':<4} | {'Tier':<4} | {'Conf↓':>6} | Description")
        print("  " + "-" * 60)
        for ev_date, d, ev in sorted(upcoming, key=lambda x: (x[0], x[2].get('tier', 3))):
            tier    = ev.get('tier', '?')
            conf    = f"-{ev.get('confidence_reduction', 0) * 100:.0f}%"
            desc    = ev.get('name', '')[:35]
            print(f"  {ev_date:<12} | D+{d:<3} | T{tier:<3} | {conf:>6} | {desc}")

    print("\n" + "=" * 65)
    print(" ✅ Audit 완료.")
    print("=" * 65 + "\n")


if __name__ == '__main__':
    # CLI: --date YYYY-MM-DD 옵션 지원
    target = None
    if '--date' in sys.argv:
        idx = sys.argv.index('--date')
        if idx + 1 < len(sys.argv):
            target = sys.argv[idx + 1]
    run_event_audit(target)
