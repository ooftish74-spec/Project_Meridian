from src.utils.api_key_manager import get_av_api_key
from pathlib import Path
_DATA_DIR = Path('data')
'[Phase 57: Alpha Vantage Macro Sentiment] 글로벌 매크로 감성 수집기.\n\nAlpha Vantage NEWS_SENTIMENT API를 통해 글로벌 매크로 시장의 종합 감성 점수를\n수집하여 data/sentiment/global_macro_sentiment.json에 저장한다.\n\n주의: 무료 API 한도 25회/일 → daily_pipeline에서 단 1회만 호출\n'
from __future__ import annotations
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
import requests
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('alpha_vantage_collector')

def collect_global_macro_sentiment() -> dict:
    """✎ Alpha Vantage NEWS_SENTIMENT API 호출 → 글로벌 매크로 감성 점수 수집.

    Returns:
        {
            'average_sentiment_score': float,   # -1~+1 (음수=부정적, 양수=금정적)
            'article_count':           int,     # 처리된 뉴스 건수
            'source':                  str,
            'timestamp':               str,
        }
        API 실패 또는 한도 초과 시 빈 dict {} 반환.
    """
    from src.utils.credential_manager import CredentialManager
    api_key = CredentialManager().read_from_keychain('ALPHA_VANTAGE_API_KEY') or ''
    if not api_key:
        logger.error('  ❌ ALPHA_VANTAGE_API_KEY가 환경 변수에 없습니다.')
        return {}
    url = f'https://www.alphavantage.co/query?function=NEWS_SENTIMENT&topics=economy_macro&apikey={api_key}'
    try:
        logger.info('  🌍 [Phase 57] Alpha Vantage 매크로 감성 데이터 수집 중...')
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()
        if 'Information' in data:
            logger.warning(f'  ⚠️ Alpha Vantage API 한도 초과/제한: {data['Information']}')
            return {}
        if 'Note' in data:
            logger.warning(f'  ⚠️ Alpha Vantage Note: {data['Note']}')
            return {}
        feed = data.get('feed', [])
        if not feed:
            logger.warning('  ⚠️ 가져온 뉴스가 없습니다.')
            return {}
        scores = [float(item['overall_sentiment_score']) for item in feed[:50] if 'overall_sentiment_score' in item]
        avg_score = round(sum(scores) / len(scores), 4) if scores else 0.0
        result = {'average_sentiment_score': avg_score, 'article_count': len(feed), 'scored_count': len(scores), 'source': 'Alpha Vantage NEWS_SENTIMENT', 'timestamp': datetime.now().isoformat()}
        out_dir = ROOT / 'data' / 'sentiment'
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / 'global_macro_sentiment.json'
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        logger.info(f'  ✅ [Phase 57] 글로벌 매크로 감성 점수: {avg_score:+.4f} (기사 {len(feed)}건 / 점수 {len(scores)}건) → {out_path}')
        return result
    except requests.exceptions.Timeout:
        logger.error('  ❌ Alpha Vantage 타임아웃 (20s 초과)', exc_info=True)
        return {}
    except requests.exceptions.HTTPError as _he:
        logger.error(f'  ❌ Alpha Vantage HTTP 오류: {_he}', exc_info=True)
        return {}
    except Exception as e:
        logger.error(f'  ❌ [Phase 57] Alpha Vantage NEWS_SENTIMENT 에러: {e}', exc_info=True)
        return {}

def collect_global_macro(symbols: list) -> dict:
    """[Phase 1] Alpha Vantage를 통한 글로벌 매크로 지표 수집 (yfinance 대체)."""
    from src.utils.credential_manager import CredentialManager
    import pandas as pd
    api_key = CredentialManager().read_from_keychain('ALPHA_VANTAGE_API_KEY') or ''
    if not api_key:
        logger.error('  ❌ ALPHA_VANTAGE_API_KEY 누락.')
        return {}
    results = {}
    for symbol in symbols:
        try:
            url = f'https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={api_key}'
            resp = requests.get(url, timeout=10)
            data = resp.json()
            if 'Global Quote' in data and '05. price' in data['Global Quote']:
                price = float(data['Global Quote']['05. price'])
                change_pct_str = data['Global Quote'].get('10. change percent', '0%').replace('%', '')
                change_pct = float(change_pct_str)
                results[symbol] = {'price': price, 'change_1d': change_pct}
                logger.info(f'  🌍 [Alpha Vantage] {symbol} 수집 성공: {price} ({change_pct:+.2f}%)')
            else:
                logger.warning(f'  ⚠️ [Alpha Vantage] {symbol} 데이터 없음 또는 Premium 한도 도달.')
        except Exception as e:
            logger.error(f'  ❌ [Alpha Vantage] {symbol} 수집 실패: {e}', exc_info=True)
    return results

def collect_options_pcr(symbol: str='SPY') -> float:
    """[Phase 2] Alpha Vantage HISTORICAL_OPTIONS API를 활용해 Put-Call Ratio(PCR) 수집.

    현재 Alpha Vantage 무료/일반 Premium 플랜에서 HISTORICAL_OPTIONS는 추가 결제가 필요할 수 있습니다.
    호출 실패 시 Fallback으로 1.0(중립)을 반환합니다.
    """
    api_key = get_av_api_key()
    if not api_key:
        return 1.0
    url = f'https://www.alphavantage.co/query?function=HISTORICAL_OPTIONS&symbol={symbol}&apikey={api_key}'
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if 'data' in data and len(data['data']) > 0:
                latest = data['data'][0]
                put_vol = float(latest.get('put_volume', 0))
                call_vol = float(latest.get('call_volume', 0))
                if call_vol > 0:
                    pcr = put_vol / call_vol
                    logger.info(f'  📊 [Alpha Vantage] {symbol} Options PCR 수집 성공: {pcr:.2f}')
                    return round(pcr, 4)
            logger.warning(f'  ⚠️ [Alpha Vantage] Options PCR 데이터 없음 (Premium 권한 필요할 수 있음)')
    except Exception as e:
        logger.error(f'  ❌ [Alpha Vantage] Options PCR 수집 실패: {e}', exc_info=True)
    return 1.0

def collect_news_sentiment() -> float:
    """[Phase 3] Alpha Vantage NEWS_SENTIMENT API를 활용해 매크로/시장 센티먼트 수집.
    
    Topics: economy_macro, financial_markets
    
    Returns:
        float: 평균 센티먼트 점수 (보통 -0.35 ~ 0.35 사이, 낮을수록 부정적)
    """
    api_key = get_av_api_key()
    if not api_key:
        return 0.0
    url = f'https://www.alphavantage.co/query?function=NEWS_SENTIMENT&topics=economy_macro,financial_markets&limit=50&apikey={api_key}'
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if 'feed' in data and len(data['feed']) > 0:
                scores = []
                for item in data['feed']:
                    score = float(item.get('overall_sentiment_score', 0.0))
                    scores.append(score)
                avg_score = sum(scores) / len(scores) if scores else 0.0
                import json
                out_dir = _DATA_DIR / 'sentiment'
                out_dir.mkdir(parents=True, exist_ok=True)
                out_file = out_dir / 'global_macro_sentiment.json'
                result = {'average_sentiment_score': round(avg_score, 4), 'article_count': len(scores), 'last_updated': datetime.now().isoformat()}
                out_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
                logger.info(f'  📰 [Alpha Vantage] News Sentiment 수집 성공: {avg_score:+.3f} ({len(scores)} articles)')
                return round(avg_score, 4)
            else:
                logger.warning('  ⚠️ [Alpha Vantage] News Sentiment 데이터 없음.')
    except Exception as e:
        logger.error(f'  ❌ [Alpha Vantage] News Sentiment 수집 실패: {e}', exc_info=True)
    return 0.0
if __name__ == '__main__':
    res = collect_global_macro_sentiment()
    if res:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print('[⚠️] 수집 실패 또는 빈 결과 — API 키 확인 후 재시도')
        sys.exit(1)