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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger('overnight_macro')
logging.basicConfig(level=logging.INFO, format='%(message)s')

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / 'data' / 'raw' / 'overnight_macro'
RESULTS_DIR = PROJECT_ROOT / 'results'


# ═══════════════════════════════════════════════════════════
# 데이터 수집
# ═══════════════════════════════════════════════════════════

def collect_us_futures() -> dict:
    """미국 주요 선물 야간 변동 수집 (Medallion Philosophy: 유동성 ETF Proxy)."""
    from src.utils.vendor_multiplexer import VendorMultiplexer
    
    tickers = {
        'sp500_futures':  {'symbol': 'ES=F',     'name': 'S&P500 선물'},
        'nasdaq_futures': {'symbol': 'NQ=F',     'name': 'NASDAQ 선물'},
        'dow_futures':    {'symbol': 'YM=F',     'name': '다우 선물'},
    }
    
    vmx = VendorMultiplexer()
    end = datetime.now()
    start = end - timedelta(days=5)
    
    results = {}
    for key, info in tickers.items():
        try:
            h = vmx.fetch(info['symbol'], start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
            if h is not None and len(h) >= 2:
                prev_close = float(h.iloc[-2])
                last_close = float(h.iloc[-1])
                change_pct = (last_close / prev_close - 1) * 100
                
                # 5일 평균 대비 위치
                avg_5d = float(h.mean())
                vs_avg = (last_close / avg_5d - 1) * 100
                
                results[key] = {
                    'name': info['name'],
                    'symbol': info['symbol'],
                    'prev_close': round(prev_close, 2),
                    'last_close': round(last_close, 2),
                    'change_pct': round(change_pct, 4),
                    'vs_5d_avg_pct': round(vs_avg, 4),
                }
                logger.info(f"  ✅ {info['name']}: {last_close:,.2f} ({change_pct:+.2f}%)")
        except Exception as e:
            logger.warning(f"  ❌ {info['name']}: {e}")
    
    return results


def collect_sgx_proxy() -> dict:
    """SGX KOSPI200 선물 프록시: EWY (iShares MSCI South Korea ETF).
    
    SGX KOSPI200 선물은 무료 API가 없으므로, NYSE 상장 한국 ETF인 EWY를 프록시로 사용합니다.
    
    EWY의 장점:
      - NYSE에서 16:00~20:00 EST (06:00~10:00 KST+1) 거래
      - 한국 시장이 닫힌 후 미국 투자자들의 한국 심리를 직접 반영
      - 삼성전자, SK하이닉스 등 KOSPI 주요주로 구성
      - KOSPI와 상관계수 0.95+
    
    추가 프록시:
      - FLKR (Franklin FTSE South Korea ETF)
      - 원/달러 환율 움직임 (DXY 역방향)
    """
    from src.utils.vendor_multiplexer import VendorMultiplexer
    vmx = VendorMultiplexer()
    end = datetime.now()
    start_5d = end - timedelta(days=5)
    start_1m = end - timedelta(days=30)
    
    result = {}
    
    # 1차: EWY (핵심 프록시)
    try:
        h = vmx.fetch('EWY', start_5d.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        h20 = vmx.fetch('EWY', start_1m.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        
        if h is not None and len(h) >= 2:
            prev = float(h.iloc[-2])
            last = float(h.iloc[-1])
            change = (last / prev - 1) * 100
            
            ma20 = float(h20.mean()) if h20 is not None and len(h20) > 0 else last
            vs_ma20 = (last / ma20 - 1) * 100
            
            result['ewy'] = {
                'name': 'EWY (iShares MSCI South Korea)',
                'prev_close': round(prev, 2),
                'last_close': round(last, 2),
                'change_pct': round(change, 4),
                'vs_ma20_pct': round(vs_ma20, 4),
                'proxy_for': 'SGX KOSPI200 Futures',
                'correlation_note': 'KOSPI와 상관계수 0.95+',
            }
            logger.info(f"  ✅ EWY(SGX 프록시): ${last:.2f} ({change:+.2f}%)")
    except Exception as e:
        logger.warning(f"  ❌ EWY: {e}")
    
    # 2차: FLKR (보조 프록시)
    try:
        h_flkr = vmx.fetch('FLKR', start_5d.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
        if h_flkr is not None and len(h_flkr) >= 2:
            prev = float(h_flkr.iloc[-2])
            last = float(h_flkr.iloc[-1])
            change = (last / prev - 1) * 100
            result['flkr'] = {
                'name': 'FLKR (Franklin FTSE South Korea)',
                'prev_close': round(prev, 2),
                'last_close': round(last, 2),
                'change_pct': round(change, 4),
            }
    except Exception as _e:
        logger.warning(f"  suppressed: {_e}")
    
    # ★ SGX 프록시 다중 앙상블 점수 (2026-05-29)
    # 실증 가중치: EWY(0.40) + NQ선물(0.25) + SOX(0.20) + DXY역방향(0.15)
    ewy_chg = result.get('ewy', {}).get('change_pct', 0)
    result['sgx_proxy_components'] = {'ewy': ewy_chg}
    result['sgx_proxy_source'] = 'ensemble'
    result['sgx_proxy_score'] = max(5, min(95, 50 + ewy_chg * 10))  # fallback
    
    return result


def collect_macro_indicators() -> dict:
    """거시경제 지표: VIX, DXY, 국채, 유가, 금."""
    from src.utils.vendor_multiplexer import VendorMultiplexer
    vmx = VendorMultiplexer()
    end = datetime.now()
    start = end - timedelta(days=5)
    
    indicators = {
        'vix':       {'symbol': '^VIX',      'name': 'VIX (공포지수)',        'invert': True},
        'dxy':       {'symbol': 'DX-Y.NYB',  'name': 'DXY (달러인덱스)',      'invert': True},
        'us10y':     {'symbol': '^TNX',       'name': '미국 10년물 국채',      'invert': True},
        'wti_oil':   {'symbol': 'CL=F',      'name': 'WTI 원유',             'invert': False},
        'gold':      {'symbol': 'GC=F',      'name': '금 선물',              'invert': False},
        'usdkrw':    {'symbol': 'USDKRW=X',  'name': '원/달러 환율',          'invert': True},
    }
    
    results = {}
    for key, info in indicators.items():
        try:
            h = vmx.fetch(info['symbol'], start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
            if h is not None and len(h) >= 2:
                prev = float(h.iloc[-2])
                last = float(h.iloc[-1])
                change = (last / prev - 1) * 100
                
                results[key] = {
                    'name': info['name'],
                    'symbol': info['symbol'],
                    'prev_close': round(prev, 4),
                    'last_close': round(last, 4),
                    'change_pct': round(change, 4),
                    'impact_direction': 'negative' if info['invert'] else 'positive',
                }
                logger.info(f"  ✅ {info['name']}: {last:.2f} ({change:+.2f}%)")
            elif h is not None and len(h) == 1:
                last = float(h.iloc[-1])
                results[key] = {
                    'name': info['name'],
                    'symbol': info['symbol'],
                    'last_close': round(last, 4),
                    'change_pct': 0,
                    'note': 'only_1_day_available',
                }
        except Exception as e:
            logger.warning(f"  ❌ {info['name']}: {e}")
    
    return results


# ═══════════════════════════════════════════════════════════
# 종합 점수 계산
# ═══════════════════════════════════════════════════════════

def compute_overnight_score(us_futures: dict, sgx_proxy: dict, macro: dict) -> dict:
    """Overnight Macro Δ 종합 점수 계산.
    
    가중치:
      미국 선물 변동: 30% (S&P/NQ/YM 평균)
      SGX 프록시:    25% (EWY 변화)
      VIX 레벨:     15% (공포 수준)
      DXY 방향:     10% (달러 강세 = 외국인 매도)
      국채 금리:     10% (금리 상승 = 성장주 약세)
      유가:         10% (에너지/인플레)
    """
    scores = {}
    weights = {}
    
    # 1. 미국 선물 평균 변화 → 점수
    us_changes = [v['change_pct'] for v in us_futures.values() if 'change_pct' in v]
    if us_changes:
        avg_us = sum(us_changes) / len(us_changes)
        # +1% → 60, +2% → 70, -1% → 40, -2% → 30
        scores['us_futures'] = max(5, min(95, 50 + avg_us * 10))
        weights['us_futures'] = 0.30
    
    # 2. SGX 프록시 (★ 앙상블: EWY 40% + NQ 25% + SOX 20% + DXY inv 15%)
    ewy_chg = sgx_proxy.get('ewy', {}).get('change_pct', 0)
    nq_chg = us_futures.get('nasdaq_futures', {}).get('change_pct', 0)
    # SOX: macro 또는 signal_cache에서
    sox_chg = macro.get('sox', {}).get('change_pct', 0)
    dxy_chg = macro.get('dxy', {}).get('change_pct', 0)

    ensemble_chg = (
        ewy_chg * 0.40 +
        nq_chg * 0.25 +
        sox_chg * 0.20 +
        (-dxy_chg) * 0.15  # DXY 역방향
    )
    sgx_ensemble_score = max(5, min(95, 50 + ensemble_chg * 10))
    scores['sgx_proxy'] = sgx_ensemble_score
    weights['sgx_proxy'] = 0.25
    # 앙상블 결과를 sgx_proxy에 저장
    sgx_proxy['sgx_proxy_score'] = round(sgx_ensemble_score, 1)
    sgx_proxy['sgx_proxy_components'] = {
        'ewy': round(ewy_chg, 3), 'nq': round(nq_chg, 3),
        'sox': round(sox_chg, 3), 'dxy_inv': round(-dxy_chg, 3),
    }
    logger.info(f"  📊 SGX 앙상블: {sgx_ensemble_score:.0f}/100 "
                f"(EWY={ewy_chg:+.1f}% NQ={nq_chg:+.1f}% "
                f"SOX={sox_chg:+.1f}% DXY={dxy_chg:+.1f}%)")
    
    # 3. VIX 레벨
    vix = macro.get('vix', {})
    if 'last_close' in vix:
        vix_val = vix['last_close']
        # VIX 15=80, 20=65, 25=50, 30=35, 40=15
        vix_score = max(5, min(95, 100 - vix_val * 2))
        scores['vix'] = vix_score
        weights['vix'] = 0.15
    
    # 4. DXY (달러 역방향)
    dxy = macro.get('dxy', {})
    if 'change_pct' in dxy:
        # DXY 상승 = 원화 약세 = 외국인 매도 → 부정적
        dxy_score = max(5, min(95, 50 - dxy['change_pct'] * 20))
        scores['dxy'] = dxy_score
        weights['dxy'] = 0.10
    
    # 5. 미국 10년물 (금리 역방향)
    us10y = macro.get('us10y', {})
    if 'change_pct' in us10y:
        # 금리 상승 → 성장주 약세
        bond_score = max(5, min(95, 50 - us10y['change_pct'] * 15))
        scores['us10y'] = bond_score
        weights['us10y'] = 0.10
    
    # 6. 유가
    oil = macro.get('wti_oil', {})
    if 'change_pct' in oil:
        # 유가 급등 → 인플레 우려 → 약간 부정적
        oil_score = max(5, min(95, 50 - oil['change_pct'] * 5))
        scores['oil'] = oil_score
        weights['oil'] = 0.10
    
    # 가중 평균
    if weights:
        total_weight = sum(weights.values())
        weighted_score = sum(scores[k] * weights[k] for k in scores) / total_weight
    else:
        weighted_score = 50
    
    # 시장 방향 판정
    if weighted_score >= 65:
        direction = 'bullish'
        label = '🟢 강세'
    elif weighted_score >= 55:
        direction = 'mildly_bullish'
        label = '🟢 약세강세'
    elif weighted_score >= 45:
        direction = 'neutral'
        label = '⚪ 중립'
    elif weighted_score >= 35:
        direction = 'mildly_bearish'
        label = '🟡 약세약세'
    else:
        direction = 'bearish'
        label = '🔴 약세'
    
    return {
        'overnight_score': round(weighted_score, 1),
        'direction': direction,
        'label': label,
        'component_scores': {k: round(v, 1) for k, v in scores.items()},
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
    ewy_chg = sgx_proxy.get('ewy', {}).get('change_pct', 0)
    
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
        logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
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
    
    # 2. SGX KOSPI200 프록시
    logger.info("\n─── SGX 프록시 (EWY) ───")
    sgx_proxy = collect_sgx_proxy()
    
    # 3. 거시지표
    logger.info("\n─── 거시경제 지표 ───")
    macro = collect_macro_indicators()
    
    # 4. 종합 점수
    logger.info("\n─── 종합 Overnight Score ───")
    overnight = compute_overnight_score(us_futures, sgx_proxy, macro)
    logger.info(f"  📊 Overnight Score: {overnight['overnight_score']:.0f}/100 → {overnight['label']}")
    for k, v in overnight['component_scores'].items():
        w = overnight['weights'].get(k, 0)
        logger.info(f"    {k:15s}: {v:5.1f}/100 (가중 {w:.0%})")
    
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
    
    # SGX 프록시
    if 'ewy' in sgx_proxy:
        briefing_lines.append(f"  EWY(SGX프록시): {sgx_proxy['ewy']['change_pct']:+.2f}%")
    
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
        with open(output_file, 'w') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        logger.info(f"\n  💾 저장: {output_file}")
        
        # 텔레그램 발송
        try:
            from src.notifications.telegram_notifier import TelegramNotifier
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
