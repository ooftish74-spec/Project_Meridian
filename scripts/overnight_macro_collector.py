#!/usr/bin/env python3
"""
Overnight Macro Δ Collector — 아침 투자 의사결정의 핵심 데이터 수집
==================================================================

매일 아침 06:30~07:00 KST에 실행하여 밤사이 글로벌 시장 변화를 수집합니다.

수집 항목:
  1. 미국 증시: S&P500, NASDAQ, 다우 선물 (야간 변동)
  2. SGX KOSPI200 프록시: EWY (iShares MSCI South Korea ETF, NYSE)
  3. 거시지표: VIX, DXY(달러인덱스), 미국 10년물 국채, WTI, Gold
  4. 외국인 심리 프록시: EWY 전일 대비 변화 + DXY 방향

출력:
  data/raw/overnight_macro/YYYY-MM-DD.json
  → OIS (Overnight Intelligence Score)에 직접 입력
  → L1 매크로 점수 보정에 사용

Usage:
    python scripts/overnight_macro_collector.py
    python scripts/overnight_macro_collector.py --dry-run

Author: Project-A
Date: 2026-03-27
"""

import json
import logging
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.utils.file_ops import atomic_write_json

logger = logging.getLogger('overnight_macro')
logging.basicConfig(level=logging.INFO, format='%(message)s')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / 'data' / 'raw' / 'overnight_macro'
RESULTS_DIR = PROJECT_ROOT / 'results'


# ═══════════════════════════════════════════════════════════
# 데이터 수집
# ═══════════════════════════════════════════════════════════

def collect_us_futures() -> dict:
    """미국 주요 선물 야간 변동 수집 (Streaming Night Watch Proxy)."""
    import json
    
    stream_file = PROJECT_ROOT / 'data' / 'macro' / 'us_night_stream.json'
    results = {}
    
    # Futures mapping for logging/display
    mapping = {
        'ES=F': {'key': 'sp500_futures', 'name': 'S&P500 선물'},
        'NQ=F': {'key': 'nasdaq_futures', 'name': 'NASDAQ 선물'},
        'YM=F': {'key': 'dow_futures', 'name': '다우 선물'}
    }
    
    try:
        if stream_file.exists():
            from src.utils.file_ops import atomic_write_json

            with open(stream_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            stream_time = datetime.fromisoformat(data['timestamp'])
            # Check if stream is fresh (within 4 hours)
            if (datetime.now() - stream_time).total_seconds() < 14400:
                for sym, futures_info in mapping.items():
                    if sym in data:
                        futures_data = data[sym]
                        results[futures_info['key']] = {
                            'name': futures_info['name'],
                            'symbol': sym,
                            'prev_close': round(futures_data['prev_close'], 2),
                            'last_close': round(futures_data['price'], 2),
                            'change_pct': round(futures_data['change_pct'], 4),
                            'vs_5d_avg_pct': 0.0,
                            'source': 'night_watch_stream'
                        }
                        logger.info(f"  🌊 [Streamed] {futures_info['name']}: {futures_data['price']:,.2f} ({futures_data['change_pct']:+.2f}%)")
                return results
            else:
                logger.warning("  ⚠️ us_night_stream.json 데이터가 오래되었습니다. Google Finance / VendorMultiplexer 실시간 연동 시도...")
    except Exception as e:
        logger.warning(f"  ⚠️ us_night_stream.json 읽기 실패: {e}")

    # 1st Fallback: GoogleFinanceCollector Direct T-0 Quote Details (0-Lag Live)
    try:
        from src.utils.google_finance_collector import GoogleFinanceCollector
        for sym, futures_info in mapping.items():
            try:
                details = GoogleFinanceCollector.get_quote_details(sym)
                if details and details.get('price', 0) > 0:
                    last_c = float(details['price'])
                    prev_c = float(details.get('prev_close', last_c))
                    chg_pct = float(details.get('change_pct', 0.0))
                    results[futures_info['key']] = {
                        'name': futures_info['name'],
                        'symbol': sym,
                        'prev_close': round(prev_c, 2),
                        'last_close': round(last_c, 2),
                        'change_pct': round(chg_pct, 4),
                        'change_pct_settlement': round(chg_pct, 4),
                        'change_pct_session': round(chg_pct, 4),
                        'vs_5d_avg_pct': 0.0,
                        'source': 'google_finance_t0'
                    }
                    logger.info(f"  ⚡ [Google Finance T-0] {futures_info['name']}: {last_c:,.2f} ({chg_pct:+.2f}%)")
            except Exception as _gfe:
                logger.warning(f"  ⚠️ Google Finance 수집 실패 ({futures_info['name']}): {_gfe}")
        if len(results) == len(mapping):
            return results
    except Exception as e_gf:
        logger.warning(f"  ⚠️ Google Finance 전반 오류: {e_gf}")

    # 2nd Fallback: FRED Official Direct CSV 수집
    try:
        fred_futures_map = {
            'ES=F': ('SP500', 'S&P500 선물'),
            'NQ=F': ('NASDAQ100', 'NASDAQ 선물'),
            'YM=F': ('DJIA', '다우 선물')
        }
        for sym, (fred_id, name) in fred_futures_map.items():
            if sym in results:
                continue
            try:
                url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fred_id}"
                df = pd.read_csv(url)
                if not df.empty and len(df.columns) >= 2:
                    df.columns = ['date', 'value']
                    df['value'] = pd.to_numeric(df['value'], errors='coerce')
                    df = df.dropna()
                    if len(df) >= 1:
                        last_c = float(df['value'].iloc[-1])
                        prev_c = float(df['value'].iloc[-2]) if len(df) > 1 else last_c
                        chg_pct = ((last_c - prev_c) / prev_c) * 100.0 if prev_c > 0 else 0.0
                        futures_info = mapping.get(sym, {'key': sym, 'name': name})
                        results[futures_info['key']] = {
                            'name': name,
                            'symbol': sym,
                            'prev_close': round(prev_c, 2),
                            'last_close': round(last_c, 2),
                            'change_pct': round(chg_pct, 4),
                            'change_pct_settlement': round(chg_pct, 4),
                            'change_pct_session': round(chg_pct, 4),
                            'vs_5d_avg_pct': 0.0,
                            'source': 'fred_official_ssot'
                        }
                        logger.info(f"  🌐 [FRED SSoT] {name}: {last_c:,.2f} ({chg_pct:+.2f}%)")
            except Exception as _fe:
                logger.warning(f"  ⚠️ FRED 지수 수집 실패 ({name}): {_fe}")
    except Exception as e:
        logger.error(f"  ❌ FRED 지수 수집 예외: {e}")
    if results:
        return results

    logger.error("  ❌ US Futures 수집 실패.")
    raise RuntimeError("Critical data missing: US Futures Stream")


def collect_sgx_proxy() -> dict:
    """[DELETED] CME/SGX KOSPI200 야간 선물 프록시 (EWY) 로직 삭제됨.
    한국 증시(KIS API) 야간 선물 수집 모듈(night_futures_monitor.py)로 대체.
    """
    return {}


def collect_macro_indicators() -> dict:
    """거시경제 지표: VIX, DXY, 국채, 유가, 금."""
    from src.utils.vendor_multiplexer import VendorMultiplexer
    vmx = VendorMultiplexer()
    end = datetime.now()
    start = end - timedelta(days=5)
    
    indicators = {
        'vix':       {'symbol': 'VIX',       'name': 'VIX (공포지수)',        'invert': True},
        'dxy':       {'symbol': 'DXY',       'name': 'DXY (달러인덱스)',      'invert': True},
        'us10y':     {'symbol': 'US10Y',     'name': '미국 10년물 국채',      'invert': True},
        'wti_oil':   {'symbol': 'WTI',       'name': 'WTI 원유',             'invert': False},
        'gold':      {'symbol': 'GOLD',      'name': '금 선물',              'invert': False},
        'usdkrw':    {'symbol': 'USDKRW',    'name': '원/달러 환율',          'invert': True},
    }
    
    results = {}
    for key, info in indicators.items():
        success = False
        for attempt in range(4):
            try:
                h = vmx.fetch(info['symbol'], start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
                if h is not None and len(h) >= 1:
                    last = float(h.iloc[-1].iloc[0]) if isinstance(h.iloc[-1], pd.Series) else float(h.iloc[-1])
                    if len(h) >= 2:
                        prev = float(h.iloc[-2].iloc[0]) if isinstance(h.iloc[-2], pd.Series) else float(h.iloc[-2])
                    else:
                        prev = last
                    change = (last / prev - 1) * 100 if prev != 0 else 0.0
                    
                    results[key] = {
                        'name': info['name'],
                        'symbol': info['symbol'],
                        'prev_close': round(prev, 4),
                        'last_close': round(last, 4),
                        'change_pct': round(change, 4),
                        'impact_direction': 'negative' if info['invert'] else 'positive',
                    }
                    logger.info(f"  ✅ {info['name']}: {last:.2f} ({change:+.2f}%)")
                    success = True
                    break
                elif h is not None and len(h) == 1:
                    last = float(h.iloc[-1].iloc[0]) if isinstance(h.iloc[-1], pd.Series) else float(h.iloc[-1])
                    results[key] = {
                        'name': info['name'],
                        'symbol': info['symbol'],
                        'last_close': round(last, 4),
                        'change_pct': 0,
                        'note': 'only_1_day_available',
                    }
                    success = True
                    break
            except Exception as e:
                import time
                logger.warning(f"  ⚠️ {info['name']} 수집 실패 (시도 {attempt+1}/4): {e}")
                time.sleep(2 ** attempt)
        if not success:
            if key in ['vix', 'usdkrw', 'dxy', 'us10y']:
                logger.error(f"  ❌ {info['name']}: 최대 재시도 초과. 수집 실패.")
                raise RuntimeError(f"Critical data missing: {info['name']}")
            else:
                logger.warning(f"  ⚠️ {info['name']}: 선택 지표 수집 실패 -> 기본값 0.0 설정")
                results[key] = {'name': info['name'], 'symbol': info['symbol'], 'prev_close': 0.0, 'last_close': 0.0, 'change_pct': 0.0, 'impact_direction': 'neutral'}
    
    return results


# ═══════════════════════════════════════════════════════════
# 종합 점수 계산
# ═══════════════════════════════════════════════════════════

def compute_overnight_score(us_futures: dict, sgx_proxy: dict, macro: dict) -> dict:
    """Overnight Macro Δ 종합 점수 계산 (SSOT OvernightIntelligenceScore 엔진 연동)."""
    try:
        from src.intelligence.overnight_intelligence import OvernightIntelligenceScore
        ois_engine = OvernightIntelligenceScore()
        ois_res = ois_engine.calculate()
        score = ois_res['ois']
        sentiment = ois_res.get('sentiment', 'neutral')
        
        label_map = {
            'strong_bullish': '🟢🟢 강력강세',
            'bullish': '🟢 강세',
            'neutral': '⚪ 중립',
            'bearish': '🔴 약세',
            'strong_bearish': '🔴🔴 강력약세'
        }
        label = label_map.get(sentiment, '⚪ 중립')
        
        comp_scores = {}
        for k, v in ois_res.get('components', {}).items():
            comp_scores[k] = v.get('score', 50.0)
            
        return {
            'overnight_score': score,
            'direction': sentiment,
            'label': label,
            'component_scores': comp_scores,
            'weights': ois_engine.WEIGHTS,
            'ois_raw': ois_res
        }
    except Exception as _e:
        logger.warning(f"  ⚠️ SSOT OIS 연동 실패, 레거시 산출 사용: {_e}")
        # Legacy fallback...
        scores = {}
        weights = {}
        us_changes = [v['change_pct'] for v in us_futures.values() if 'change_pct' in v]
        if us_changes:
            avg_us = sum(us_changes) / len(us_changes)
            scores['us_futures'] = max(5, min(95, 50 + avg_us * 10))
            weights['us_futures'] = 0.30
        weighted_score = sum(scores.values()) / max(len(scores), 1)
        return {
            'overnight_score': round(weighted_score, 1),
            'direction': 'neutral',
            'label': '⚪ 중립',
            'component_scores': scores,
            'weights': weights,
        }


def compute_kospi_gap_estimate(overnight: dict, sgx_proxy: dict,
                                us_futures: dict) -> dict:
    """아침 KOSPI 시가 갭 예측.
    
    ★ 핵심 보정: EWY 변화에서 전일 KOSPI 변화를 차감
    
    이유:
      EWY는 미국장에서 거래되므로, 한국장 당일 변화를 "뒤늦게" 반영합니다.
      예) 3/26 한국장 KOSPI -3.4% → 3/26 미국장 EWY -6%
      → EWY -6%의 대부분은 이미 어제 KOSPI에 반영된 정보
      → 순수 야간 신정보 = EWY변화 - 전일KOSPI변화
    
    보정 모델:
      순야간정보 = EWY변화 - 전일KOSPI변화
      KOSPI 갭 ≈ 순야간정보 × 0.5 + 미국선물 × 0.3 + VIX보정
    """
    ewy_chg = 0
    
    us_changes = [v['change_pct'] for v in us_futures.values() if 'change_pct' in v]
    us_avg = sum(us_changes) / len(us_changes) if us_changes else 0
    
    # ★ 전일 KOSPI 변화 로드 (이미 반영된 정보 차감)
    prev_kospi_chg = 0
    try:
        from pykrx import stock as pykrx_stock
        from datetime import datetime as _dt, timedelta as _td
        _end = _dt.now().strftime('%Y%m%d')
        _start = (_dt.now() - _td(days=10)).strftime('%Y%m%d')
        df = pykrx_stock.get_market_ohlcv_by_date(_start, _end, '069500')  # KODEX200
        if len(df) >= 2:
            prev_kospi_chg = (float(df['종가'].iloc[-1]) / float(df['종가'].iloc[-2]) - 1) * 100
    except Exception as _e:
        logger.warning(f"  suppressed: {_e}")
    
    # 순수 야간 정보 = EWY 변화 - 전일 KOSPI 변화
    net_overnight = ewy_chg - prev_kospi_chg
    
    # VIX 보정
    try:
        overnight_score = overnight.get('component_scores', {}).get('vix', 50)
        if overnight_score < 35:
            vix_adj = -0.3
        elif overnight_score > 65:
            vix_adj = 0.1
        else:
            vix_adj = 0
    except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
        import logging
        logging.getLogger(__name__).debug(f"Targeted fallback: {e}")
        vix_adj = 0
    
    # 보정된 갭 모델: 순야간 × 0.5 + 미국선물 × 0.3 + VIX
    estimated_gap = net_overnight * 0.5 + us_avg * 0.3 + vix_adj
    
    # 신뢰도
    if (net_overnight > 0 and us_avg > 0) or (net_overnight < 0 and us_avg < 0):
        confidence = 'high'
    elif abs(net_overnight) < 0.3 and abs(us_avg) < 0.3:
        confidence = 'low'
    else:
        confidence = 'medium'
    
    return {
        'estimated_gap_pct': round(estimated_gap, 2),
        'ewy_raw_chg': round(ewy_chg, 2),
        'prev_kospi_chg': round(prev_kospi_chg, 2),
        'net_overnight': round(net_overnight, 2),
        'ewy_contribution': round(net_overnight * 0.5, 2),
        'us_contribution': round(us_avg * 0.3, 2),
        'vix_adjustment': round(vix_adj, 2),
        'confidence': confidence,
    }


# ═══════════════════════════════════════════════════════════
# 메인
# ═══════════════════════════════════════════════════════════

def main():
    today = datetime.now().strftime('%Y-%m-%d')
    dry_run = '--dry-run' in sys.argv
    
    logger.info("═══════════════════════════════════════════════════")
    logger.info(f"  🌙 Overnight Macro Δ Collector — {today}")
    logger.info("═══════════════════════════════════════════════════")
    
    # 1. 미국 선물
    logger.info("\n─── 미국 선물 ───")
    us_futures = collect_us_futures()
    
    # 2. SGX KOSPI200 프록시 (삭제됨 - KIS Night Futures로 대체)
    sgx_proxy = collect_sgx_proxy()
    
    # 3. 거시지표
    logger.info("\n─── 거시경제 지표 ───")
    macro = collect_macro_indicators()
    
    # 3.5. 1차 저장 (SSoT OIS 엔진이 당일 지표를 즉시 참조하도록 보장)
    if not dry_run:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_file = OUTPUT_DIR / f'{today}.json'
        atomic_write_json(output_file, {
            'date': today,
            'timestamp': datetime.now().isoformat(),
            'us_futures': us_futures,
            'macro_indicators': macro
        }, indent=2, ensure_ascii=False)

    # 4. 종합 점수
    logger.info("\n─── 종합 Overnight Score ───")
    overnight = compute_overnight_score(us_futures, sgx_proxy, macro)
    logger.info(f"  📊 Overnight Score: {overnight['overnight_score']:.0f}/100 → {overnight['label']}")
    for k, v in overnight['component_scores'].items():
        w = overnight['weights'].get(k, 0)
        val = float(v) if v is not None else 50.0
        logger.info(f"    {k:15s}: {val:5.1f}/100 (가중 {w:.0%})")
    
    # 5. KOSPI 갭 예측
    logger.info("\n─── KOSPI 시가 갭 예측 ───")
    gap = compute_kospi_gap_estimate(overnight, sgx_proxy, us_futures)
    logger.info(f"  📈 예상 KOSPI 갭: {gap['estimated_gap_pct']:+.2f}%")
    logger.info(f"     EWY 기여: {gap['ewy_contribution']:+.2f}%")
    logger.info(f"     미국 기여: {gap['us_contribution']:+.2f}%")
    logger.info(f"     VIX 보정: {gap['vix_adjustment']:+.2f}%")
    logger.info(f"     신뢰도:   {gap['confidence']}")
    
    # 6. 아침 브리핑 메시지 생성
    briefing_lines = [
        f"🌙 <b>Overnight Macro Δ</b> ({today})",
        "",
    ]
    
    # 미국 선물
    for k, v in us_futures.items():
        briefing_lines.append(f"  {v['name']}: {v['change_pct']:+.2f}%")
    
    # SGX 프록시 출력 삭제됨
    
    briefing_lines.append("")
    
    # 거시지표
    for k, v in macro.items():
        if 'change_pct' in v:
            briefing_lines.append(f"  {v['name']}: {v['last_close']:.2f} ({v['change_pct']:+.2f}%)")
    
    briefing_lines.extend([
        "",
        f"📊 종합: {overnight['overnight_score']:.0f}/100 {overnight['label']}",
        f"📈 KOSPI 갭 예상: {gap['estimated_gap_pct']:+.2f}% ({gap['confidence']})",
    ])
    
    briefing = '\n'.join(briefing_lines)
    logger.info(f"\n─── 텔레그램 브리핑 ───")
    logger.info(briefing)
    
    # 7. 저장
    output = {
        'date': today,
        'timestamp': datetime.now().isoformat(),
        'us_futures': us_futures,
        'sgx_proxy': sgx_proxy,
        'macro_indicators': macro,
        'overnight_score': overnight,
        'kospi_gap_estimate': gap,
        'briefing': briefing,
    }
    
    if not dry_run:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_file = OUTPUT_DIR / f'{today}.json'
        atomic_write_json(output_file, output, indent=2, ensure_ascii=False)
        logger.info(f"\n  💾 저장: {output_file}")
        
        # 텔레그램 발송
        try:
            from src.utils.telegram_notifier import TelegramNotifier
            tg = TelegramNotifier()
            if tg.enabled:
                tg.send_message(briefing)
                logger.info("  📨 텔레그램 전송 완료")
        except Exception as e:
            logger.warning(f"  ⚠️ 텔레그램: {e}")
    else:
        logger.info("\n  🏃 Dry run — 저장 생략")
    
    logger.info("\n═══ Done ═══")
    return output


if __name__ == '__main__':
    main()
