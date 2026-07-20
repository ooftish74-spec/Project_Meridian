"""
한국 관세청 수출입 무역통계 API Client
=========================================
data.go.kr 공공데이터포털 — 관세청 품목별 수출입실적.

핵심 예측 피처:
  - 반도체(HS 8541/8542) 수출 → 삼성전자/SK하이닉스 중기 실적 직결
  - 자동차(HS 8703) 수출 → 현대차/기아 실적 연동
  - 철강(HS 72) 수출 → 포스코 등 철강 섹터
  - 대중국 수출 → 한국 수출의 25% → 전체 시장

설정:
  data.go.kr 에서 API 키 발급 (즉시, 무료)
  .env에 DATA_GO_KR_API_KEY=인증키

Usage:
    from src.data_collection.customs_api import CustomsTradeClient
    client = CustomsTradeClient()
    semi = client.get_semiconductor_exports('202501', '202512')
    features = client.get_trade_features()
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree
import pandas as pd
import requests
logger = logging.getLogger(__name__)

class CustomsTradeClient:
    """한국 관세청 품목별 수출입실적 API 클라이언트."""
    BASE_URL = 'https://apis.data.go.kr/1220000/Itemtrade/getItemtradeList'
    HS_CODES = {'semiconductor': '8541', 'ic_chip': '8542', 'display': '9013', 'auto': '8703', 'auto_parts': '8708', 'steel': '72', 'petrochem': '2710', 'battery': '8507', 'ship': '8901'}
    COUNTRY_CODES = {'china': 'CN', 'usa': 'US', 'japan': 'JP', 'eu': 'DE', 'vietnam': 'VN'}

    def __init__(self, api_key: str=None):
        self.api_key = api_key or self._load_api_key()
        self._rate_limit_delay = 0.3
        if self.api_key:
            logger.info(f'  🏛️ 관세청 API 초기화 (키: {self.api_key[:8]}...)')
        else:
            logger.warning('  ⚠️ 관세청 API 키 미설정 — .env에 DATA_GO_KR_API_KEY 추가 필요')

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _load_api_key(self) -> str:
        """[Keychain] DATA_GO_KR API 키 로드."""
        from src.utils.credential_manager import CredentialManager
        return CredentialManager().read_from_keychain('DATA_GO_KR_API_KEY') or ''

    def _call_api(self, params: dict) -> Optional[pd.DataFrame]:
        """API 호출."""
        if not self.is_available:
            return None
        params['serviceKey'] = self.api_key
        params.setdefault('numOfRows', '100')
        params.setdefault('pageNo', '1')
        try:
            time.sleep(self._rate_limit_delay)
            resp = requests.get(self.BASE_URL, params=params, timeout=10)
            if resp.status_code != 200:
                logger.warning(f'  관세청 API HTTP {resp.status_code}')
                return None
            root = ElementTree.fromstring(resp.content)
            result_code = root.findtext('.//resultCode')
            if result_code and result_code != '00':
                msg = root.findtext('.//resultMsg', 'Unknown')
                logger.warning(f'  관세청 API 에러: {result_code} - {msg}')
                return None
            items = root.findall('.//item')
            if not items:
                return pd.DataFrame()
            rows = []
            for item in items:
                row = {}
                for child in item:
                    row[child.tag] = child.text
                rows.append(row)
            df = pd.DataFrame(rows)
            return df
        except Exception as e:
            logger.warning(f'  관세청 API 오류: {e}', exc_info=True)
            return None

    def get_exports_by_item(self, hs_code: str, start_ym: str, end_ym: str, country: str='') -> Optional[pd.DataFrame]:
        """
        품목 코드별 수출 실적.
        hs_code: HS 코드 (예: '8541')
        start_ym: 시작 연월 (예: '202501')
        end_ym: 종료 연월 (예: '202512')
        country: 국가 코드 (예: 'CN')
        """
        params = {'strtYymm': start_ym, 'endYymm': end_ym, 'hsSgn': hs_code}
        if country:
            params['cntyCd'] = country
        df = self._call_api(params)
        if df is not None and len(df) > 0:
            for col in ['expDlr', 'impDlr', 'expWgt', 'impWgt', 'blncDlr']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            logger.info(f'  📦 수출입: HS {hs_code} ({start_ym}~{end_ym}) {len(df)}행')
        return df

    def get_semiconductor_exports(self, start_ym: str=None, end_ym: str=None) -> Optional[pd.DataFrame]:
        """반도체(HS 8541 + 8542) 수출 합산."""
        if not start_ym:
            start_ym = (datetime.now() - timedelta(days=365)).strftime('%Y%m')
        if not end_ym:
            end_ym = datetime.now().strftime('%Y%m')
        dfs = []
        for hs in ['8541', '8542']:
            df = self.get_exports_by_item(hs, start_ym, end_ym)
            if df is not None and len(df) > 0:
                df['hs_category'] = hs
                dfs.append(df)
        if dfs:
            return pd.concat(dfs, ignore_index=True)
        return None

    def get_auto_exports(self, start_ym: str=None, end_ym: str=None) -> Optional[pd.DataFrame]:
        """자동차(HS 8703) 수출."""
        if not start_ym:
            start_ym = (datetime.now() - timedelta(days=365)).strftime('%Y%m')
        if not end_ym:
            end_ym = datetime.now().strftime('%Y%m')
        return self.get_exports_by_item('8703', start_ym, end_ym)

    def get_total_trade(self, start_ym: str=None, end_ym: str=None) -> Dict:
        """전체 수출입 총괄 (주요 품목 합산)."""
        if not start_ym:
            start_ym = (datetime.now() - timedelta(days=365)).strftime('%Y%m')
        if not end_ym:
            end_ym = datetime.now().strftime('%Y%m')
        result = {}
        for name, hs in self.HS_CODES.items():
            df = self.get_exports_by_item(hs, start_ym, end_ym)
            if df is not None and len(df) > 0:
                if 'expDlr' in df.columns:
                    result[name] = {'total_export_usd': int(df['expDlr'].sum()), 'months': len(df)}
                    if 'impDlr' in df.columns:
                        result[name]['total_import_usd'] = int(df['impDlr'].sum())
                    if 'blncDlr' in df.columns:
                        result[name]['trade_balance'] = int(df['blncDlr'].sum())
        return result

    def get_trade_features(self, sector: str='') -> Dict:
        """
        수출입 기반 예측 피처 생성.

        Returns:
            반도체/자동차/철강 수출 증감률, 대중국 변화 등
        """
        if not self.is_available:
            return self._fallback_features()
        features = {}
        now = datetime.now()
        end = now.strftime('%Y%m')
        mid = (now - timedelta(days=90)).strftime('%Y%m')
        start = (now - timedelta(days=180)).strftime('%Y%m')
        try:
            semi = self.get_semiconductor_exports(start, end)
            if semi is not None and 'expDlr' in semi.columns and (len(semi) >= 4):
                if 'cmpdTyCd' in semi.columns:
                    semi = semi.sort_values('cmpdTyCd')
                total = semi['expDlr'].values
                mid_point = len(total) // 2
                recent = total[mid_point:].mean()
                past = total[:mid_point].mean()
                if past > 0:
                    features['semi_export_growth'] = round(float((recent - past) / past), 4)
                features['semi_export_recent'] = float(recent)
                features['semi_export_available'] = 1
        except Exception as e:
            logger.error(f'  반도체 수출 피처 실패: {e}', exc_info=True)
        if sector in ('', 'auto'):
            try:
                auto = self.get_auto_exports(start, end)
                if auto is not None and 'expDlr' in auto.columns and (len(auto) >= 4):
                    total = auto['expDlr'].values
                    mid_point = len(total) // 2
                    recent = total[mid_point:].mean()
                    past = total[:mid_point].mean()
                    if past > 0:
                        features['auto_export_growth'] = round(float((recent - past) / past), 4)
                    features['auto_export_available'] = 1
            except Exception as e:
                logger.error(f'  자동차 수출 피처 실패: {e}', exc_info=True)
        try:
            cn_semi = self.get_exports_by_item('8541', mid, end, 'CN')
            if cn_semi is not None and 'expDlr' in cn_semi.columns:
                features['china_semi_export'] = float(cn_semi['expDlr'].sum())
                features['china_trade_available'] = 1
        except Exception as e:
            logger.error(f'  대중국 수출 실패: {e}', exc_info=True)
        if not features:
            return self._fallback_features()
        logger.info(f'  📦 수출입 피처 {len(features)}개 생성')
        return features

    def _fallback_features(self) -> Dict:
        """API 미사용 시 프록시 피처."""
        return {'semi_export_growth': float('nan'), 'auto_export_growth': float('nan'), 'china_semi_export': float('nan'), 'semi_export_available': 0, 'auto_export_available': 0, 'china_trade_available': 0}

    def build_historical_trade(self, start_year: int=2016, output_dir: str=None) -> Optional[pd.DataFrame]:
        """
        과거 수출입 데이터 일괄 수집 (10년).
        반도체/자동차/철강 월별 수출입 추이.
        """
        if not self.is_available:
            logger.warning('  API 키 필요')
            return None
        if output_dir is None:
            project_root = Path(__file__).resolve().parent.parent.parent
            output_dir = project_root / 'data' / 'historical_10y'
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        all_data = []
        for name, hs in self.HS_CODES.items():
            logger.info(f'  📦 {name} (HS {hs}) 수집 중...')
            for year in range(start_year, now.year + 1):
                start_ym = f'{year}01'
                end_ym = f'{year}12' if year < now.year else now.strftime('%Y%m')
                df = self.get_exports_by_item(hs, start_ym, end_ym)
                if df is not None and len(df) > 0:
                    df['category'] = name
                    df['hs_code'] = hs
                    all_data.append(df)
                time.sleep(0.5)
        if all_data:
            result = pd.concat(all_data, ignore_index=True)
            path = output_dir / 'kr_trade_exports.parquet'
            result.to_parquet(path)
            logger.info(f'  ✅ 수출입 데이터: {len(result)}행 → {path}')
            return result
        return None

def test_connection():
    """관세청 API 연결 테스트."""
    client = CustomsTradeClient()
    if not client.is_available:
        logger.warning('⚠️ 관세청 API 키 미설정')
        logger.info('\n설정 방법:')
        logger.info('  1. data.go.kr 회원가입')
        logger.info("  2. '관세청_품목별 국가별 수출입실적' 검색")
        logger.info('  3. 활용 신청 → API 키 발급 (즉시)')
        logger.info('  4. .env에 추가:')
        logger.info('     DATA_GO_KR_API_KEY=발급받은키')
        return False
    now = datetime.now()
    start = (now - timedelta(days=180)).strftime('%Y%m')
    end = now.strftime('%Y%m')
    semi = client.get_semiconductor_exports(start, end)
    if semi is not None and len(semi) > 0:
        logger.info(f'✅ 관세청 API 연결 성공!')
        logger.info(f'   반도체 수출: {len(semi)}행')
        if 'expDlr' in semi.columns:
            total = semi['expDlr'].sum()
            logger.info(f'   총 수출액: ${total:,.0f}')
        return True
    logger.error('❌ 데이터 조회 실패')
    return False
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    test_connection()