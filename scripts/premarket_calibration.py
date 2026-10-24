#!/usr/bin/env python3
"""
Premarket Calibration Script (08:50 AM)
야간 선물 수익률과 KOSPI 동시호가 예상 등락률 간의 괴리를 분석하여,
비정상적인 갭(Fake Gap-Up / Sudden Crash)이 발생할 경우 시그널(Kelly Fraction)을 강제로 삭감합니다.
"""

def pget(name, fallback, ctx=None):  # Wave2 TrackF: lazy (scripts set sys.path after imports)
    try:
        from src.params import pget as _pget
    except Exception:
        return fallback
    return _pget(name, fallback, ctx)
import sys
import json
import logging
from pathlib import Path
from datetime import datetime

base = Path(__file__).parent.parent
sys.path.append(str(base))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('PremarketCalibration')

def _get_expected_kospi_gap():
    """
    KIS API (FHKST01010100)를 호출하여 KODEX 200 (069500)의 동시호가 예상 등락률을 수집.
    """
    try:
        from src.data_collection.kis_data_collector import KISDataCollector
        logger.info("  📡 [KIS API] KODEX 200 예상 체결가 조회 (TR: FHKST01010100) ...")
        collector = KISDataCollector()
        data = collector.get_current_price('069500')
        if data:
            antc_change = float(data.get('antc_change_pct', 0.0))
            if antc_change != 0.0:
                logger.info(f"  ✅ [HOTFIX] 동시호가 예상체결 등락률 수집 성공: {antc_change:+.2f}%")
                return antc_change
            elif 'change_pct' in data:
                return float(data['change_pct'])
        return 0.0 
    except Exception as e:
        logger.warning(f"  KIS API 예상 체결가 조회 실패 (fallback to 0.0): {e}")
        return 0.0

def _get_expected_stock_gap(ticker: str, adapter=None) -> float:
    """개별 종목의 동시호가 예상 등락률 수집 (FHKST01010200 동시호가 호가 수진)."""
    try:
        from src.execution._kis_adapter import KISTraderAdapter
        if adapter is None:
            adapter = KISTraderAdapter(mode="live", fetch_balance_on_init=False)
        headers = adapter._get_headers()
        headers["tr_id"] = "FHKST01010200"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
        import requests
        resp = requests.get(f"{adapter.base_url}/uapi/domestic-stock/v1/quotations/inquire-asking-price", headers=headers, params=params, timeout=5)
        if resp.status_code == 200:
            out1 = resp.json().get("output1", {})
            antc_ctrt = out1.get("antc_cntg_prdy_ctrt")
            if antc_ctrt and antc_ctrt != "":
                val = float(antc_ctrt)
                logger.info(f"  ✅ [{ticker}] 동시호가 체결가 등락률 수집: {val:+.2f}%")
                return val
    except Exception as e:
        logger.warning(f"  [{ticker}] 동시호가 수집 예외: {e}")

    try:
        from src.utils.telemetry_guard import TelemetryValidationGuard
        guard = TelemetryValidationGuard()
        return guard.extract_validated_metric(f"gap_{ticker}", fallback=0.0)
    except Exception:
        return 0.0

def run_calibration():
    logger.info("==================================================")
    logger.info(f" 🌅 08:50 Premarket Calibration Started")
    logger.info("==================================================")
    try:
        from src.utils.market_calendar import MarketCalendar
        _today_str = datetime.now().strftime('%Y-%m-%d')
        if not MarketCalendar().is_trading_day(_today_str):
            logger.info(f"  ⏭ KRX 휴장일({_today_str}) — Premarket Calibration 스킵 (dual_exit_plan 보존)")
            return
    except Exception as _cal_e:
        logger.warning(f"  ⚠️ MarketCalendar 확인 중 예외 (계속 진행): {_cal_e}")

    signals_file = base / 'results' / 'latest_signals.json'
    if not signals_file.exists():
        logger.error("  🚨 latest_signals.json 파일이 없습니다. 모닝 파이프라인(07:50)이 실패했거나 실행되지 않았습니다.")
        return

    from src.utils.file_ops import atomic_write_json
    from config.dynamic_config import DynamicConfig
    cfg = DynamicConfig()

    with open(signals_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # ----------------------------------------------------
    # 0. Account Reconciliation (08:50 KST Account Alignment)
    # ----------------------------------------------------
    logger.info("  ⚖️ [Account Reconciliation] KIS 실계좌 Bottom-Up NAV 대조 및 MTS 동기화 진행...")
    adapter = None
    try:
        from src.execution._kis_adapter import KISTraderAdapter
        from src.execution.account_reconciler import AccountReconciler
        adapter = KISTraderAdapter(mode='live')
        adapter.fetch_live_balance()
        reconciled = AccountReconciler().reconcile(adapter)
        logger.info(f"  ✅ [Account Alignment] MTS 동기화 NAV: ₩{reconciled.total_equity_krw:,.0f} (예수금: ₩{reconciled.cash_krw:,.0f}, 종목평가액: ₩{reconciled.positions_value_krw:,.0f})")
    except Exception as e_rec:
        logger.warning(f"  ⚠️ [Account Alignment] 대조 도중 경고 (fallback 유지): {e_rec}")

    if not data or 'signals' not in data:
        logger.warning("  ⚠️ 시그널이 비어있습니다. (Exit-Only 모드 등). Calibration 생략.")
        return

    signals = data['signals']

    # ----------------------------------------------------
    # 1. Macro Calibration (KOSPI 200 vs Night Futures)
    # ----------------------------------------------------
    cache_file = base / 'data' / 'macro' / 'night_futures.json'
    night_futures_ret = None
    _nf_reason = 'missing'
    if cache_file.exists():
        try:
            with open(cache_file, 'r') as f:
                cache = json.load(f)
            _nf_raw = cache.get('change_pct', cache.get('final_pct'))
            _nf_ts = cache.get('timestamp') or cache.get('updated_at')
            _max_age_h = float(pget('premarket.night_futures_max_age_hours', 16) or 16)
            _age_h = None
            if _nf_ts:
                _age_h = (datetime.now() - datetime.fromisoformat(str(_nf_ts)).replace(tzinfo=None)).total_seconds() / 3600.0
            if _nf_raw is None:
                _nf_reason = 'no_change_field'
            elif str(cache.get('status', 'OK')).upper() not in ('OK', 'VERIFIED'):
                _nf_reason = f"status={cache.get('status')}"
            elif _age_h is None or _age_h < -0.5 or _age_h > _max_age_h:
                _nf_reason = f"stale(age={_age_h if _age_h is None else round(_age_h, 1)}h > {_max_age_h}h)"
            else:
                night_futures_ret = float(_nf_raw)
                _nf_reason = f"fresh(age={_age_h:.1f}h)"
        except Exception as _nf_e:
            _nf_reason = f"error:{_nf_e}"

    expected_gap = _get_expected_kospi_gap()

    # ★ Wave 0 A2: 야간선물 데이터가 없거나 오래되면 0.0으로 간주하지 않고 매크로 보정을 통째로 생략(pass-through)
    if night_futures_ret is None:
        logger.warning(f"  ⏭️ [Macro Calibration] 야간선물 데이터 사용 불가({_nf_reason}) → 매크로 보정 생략 (신호 무변경)")
    else:
        logger.info(f"  📊 야간 선물 등락률: {night_futures_ret:+.2f}% [{_nf_reason}]")
        logger.info(f"  📊 KOSPI 예상 체결 등락률 : {expected_gap:+.2f}%")
        divergence = expected_gap - night_futures_ret
        # ★ Wave 0 A2: (1) 신호 종목코드를 114800 인버스로 바꾸던 방향 반전 로직 제거
        #               (2) 검증되지 않은 1.25x 공격 승수 비활성 — 보정은 위험을 줄이는 방향만 허용 (0-B에서 z-gap 모델로 대체)
        if night_futures_ret <= 0 and expected_gap <= -1.0:
            logger.warning(f"  ⚠️ [MACRO] 하락 정렬(야간선물 {night_futures_ret:+.2f}%, 동시호가 {expected_gap:+.2f}%) — "
                           f"신호 종목 변경 없음 (인버스 강제 전환 폐지)")
        elif night_futures_ret > 0 and expected_gap <= -1.0:
            logger.info(f"  📊 [MACRO] 야간선물↑/동시호가↓ 괴리({divergence:+.2f}%) — 승수 적용 없음 (공격 승수 비활성)")
        elif abs(divergence) >= 1.5:
            logger.info(f"  📊 [MACRO DIVERGENCE] 괴리율({divergence:+.2f}%) 감지 ➔ 적정 포지션 유지")
        else:
            logger.info("  ✅ 매크로 이상징후 없음.")

    # ----------------------------------------------------
    # 2. Micro Calibration (S2 개별종목 갭 역이용)
    # ----------------------------------------------------
    logger.info("  🔍 [Micro Calibration] S2 개별 종목 동시호가 분석 시작...")
    s2_signals = signals.get('S2', [])
    modified_s2 = False
    for sig in s2_signals:
        if sig.get('direction', '') != 'long':
            continue
        ticker = sig.get('ticker')
        if not ticker:
            continue
        
        stock_gap = _get_expected_stock_gap(ticker, adapter=adapter)
        name = sig.get('name', ticker)
        original_size = sig.get('size_pct', 0.0)
        
        if stock_gap >= 3.0:
            # Unjustified Gap Up -> 추격 매수 금지 (Penalty)
            new_size = round(original_size * 0.5, 4)
            sig['size_pct'] = new_size
            logger.warning(f"  ⚠️ [S2 Gap-Up Penalty] {name}({ticker}) 갭상승 {stock_gap:+.2f}% 과열! 추격매수 방어 (size: {original_size:.3f} -> {new_size:.3f})")
            modified_s2 = True
        elif stock_gap <= -2.0:
            # Overreaction Gap Down -> 저점 매수 기회 (Boost)
            new_size = round(min(1.0, original_size * 1.3), 4)
            sig['size_pct'] = new_size
            logger.info(f"  🚀 [S2 Gap-Down Boost] {name}({ticker}) 갭하락 {stock_gap:+.2f}% 투매! 저점매수 증폭 (size: {original_size:.3f} -> {new_size:.3f})")
            modified_s2 = True
        else:
            logger.info(f"     └ {name}({ticker}) 동시호가 {stock_gap:+.2f}% (정상 범위)")

    # ── Phase 1 & Phase 2: 08:54:30 KST 이원화 청산 엔진 스케줄링 ──
    try:
        HIGH_LIQUIDITY_INDEX_ETFS = {'069500', '122630', '252670', '114800', '252710'}
        dual_exit_plan = []
        seen_krx_tickers = set()
        raw_signals = data.get('signals', {})
        sigs_list = []
        if isinstance(raw_signals, dict):
            for s_id, s_list in raw_signals.items():
                if isinstance(s_list, list):
                    sigs_list.extend(s_list)
        elif isinstance(raw_signals, list):
            sigs_list = raw_signals

        for sig in sigs_list:
            t = str(sig.get('ticker', '')).strip()
            if not t or t.startswith('_') or not t[0].isdigit():
                continue
            if t in seen_krx_tickers:
                continue
            seen_krx_tickers.add(t)
            is_high_liq = t in HIGH_LIQUIDITY_INDEX_ETFS
            if is_high_liq:
                action = '08:54:30_CANCEL_IF_POSITIVE_GAP' if expected_gap > -0.3 else '08:30_KEEP_AUCTION_SELL'
            else:
                action = '09:00:15_REGULAR_MARKET_LP_EXIT'
            
            dual_exit_plan.append({
                'ticker': t,
                'name': sig.get('name', t),
                'direction': sig.get('direction', 'long'),
                'is_high_liquidity': is_high_liq,
                'action_plan': action,
                'kospi_gap_forecast': expected_gap
            })
        
        exit_plan_file = base / 'results' / 'dual_exit_plan.json'
        atomic_write_json(exit_plan_file, dual_exit_plan, indent=2, ensure_ascii=False)
        logger.info(f"  ⚡ [Phase 1/2 Dual Exit] 08:54:30 이원화 청산 계획 생성 완료 ({len(dual_exit_plan)}개 종목)")
    except Exception as _de_err:
        logger.error(f"  [Dual Exit Engine] 계획 수립 예외: {_de_err}")

    # Self-Healing: 주말/야간 API 타임아웃 셧다운 플래그 자동 정리
    try:
        halt_flag = base / 'results' / 'SYSTEM_HALT.flag'
        if halt_flag.exists():
            halt_data = json.loads(halt_flag.read_text())
            reason = halt_data.get('reason', '')
            if 'API Timeout' in reason or 'Maintenance' in reason or 'Timeout' in reason:
                halt_flag.unlink()
                logger.warning(f"  🟢 [Self-Healing] 주말/야간 API 타임아웃 셧다운 플래그 자동 해제 완료! (사유: {reason})")
    except Exception as _sh_err:
        logger.error(f"  [Self-Healing] 셧다운 플래그 자동 정리 실패: {_sh_err}")

    if modified_s2:
        atomic_write_json(signals_file, data, indent=2)
        logger.info("  ✅ 교정된 시그널(latest_signals.json) 저장 완료.")
    else:
        logger.info("  ✅ 마이크로 이상징후 없음. 시그널 변동 없이 100% 유지.")
        
    logger.info("==================================================")
    logger.info(f" 🏁 08:50 Premarket Calibration Finished")
    logger.info("==================================================")

if __name__ == "__main__":
    run_calibration()
