"""
KRX Collector Mixin
=====================
대량 수집 메서드 및 파생상품/ETF/ESG/금시장 API 호출.
KRXApiClient 에서 mixin 으로 상속하여 사용.
"""
import json as _json
import logging
from pathlib import Path
from typing import Dict, Optional
import pandas as pd
logger = logging.getLogger(__name__)

class KRXCollectorMixin:
    """KRX 대량 수집·파생상품 로직 mixin.

    KRXApiClient 가 다중상속으로 사용합니다.
    self._call_api(), self.SERVICES, self.get_stock_daily(),
    self.get_kosdaq_daily(), self.get_kospi_index(), self.get_kosdaq_index(),
    self.get_futures() 등은 KRXApiClient 에 정의.
    """

    def collect_all_stock_daily(self, date: str) -> Dict[str, pd.DataFrame]:
        """유가증권 + 코스닥 전종목 일별 데이터 수집 및 저장.
        
        KRX API 미게재 시(빈 결과) → yfinance 폴백으로 주요 종목 데이터 확보.
        """
        results = {}
        save_dir = Path(__file__).resolve().parent.parent.parent / 'data' / 'raw' / 'krx_stock_daily'
        save_dir.mkdir(parents=True, exist_ok=True)
        kospi = self.get_stock_daily(date)
        if kospi is not None and len(kospi) > 0:
            path = save_dir / f'kospi_{date}.csv'
            kospi.to_csv(path, index=False, encoding='utf-8-sig')
            results['kospi'] = kospi
            logger.info(f'  💾 저장: {path.name}')
        else:
            kospi_yf = self._collect_kospi_via_yfinance(date)
            if kospi_yf is not None and len(kospi_yf) > 0:
                path = save_dir / f'kospi_{date}.csv'
                kospi_yf.to_csv(path, index=False, encoding='utf-8-sig')
                results['kospi'] = kospi_yf
                logger.info(f'  💾 [yfinance 폴백] kospi_{date}.csv ({len(kospi_yf)}종목)')
            else:
                logger.warning(f'  ⚠️ KRX 20260414 데이터 없음, 다음 날짜 시도')
        kosdaq = self.get_kosdaq_daily(date)
        if kosdaq is not None and len(kosdaq) > 0:
            path = save_dir / f'kosdaq_{date}.csv'
            kosdaq.to_csv(path, index=False, encoding='utf-8-sig')
            results['kosdaq'] = kosdaq
            logger.info(f'  💾 저장: {path.name}')
        if results:
            total = sum((len(df) for df in results.values()))
            logger.info(f'  ✅ 전종목 일별 수집 완료: {total}종목')
        return results

    def get_options(self, date: str) -> Optional[pd.DataFrame]:
        """옵션 전종목 일별 매매 데이터."""
        data = self._call_api(self.SERVICES['options_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'IMP_VOLT', 'ACC_TRDVOL', 'ACC_TRDVAL', 'ACC_OPNINT_QTY']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 옵션: {len(df)}종목 ({date})')
        return df

    def collect_options_daily(self, date: str) -> Dict:
        """옵션 데이터 수집 + PCR/IV 요약 계산."""
        save_dir = Path(__file__).resolve().parent.parent.parent / 'data' / 'raw' / 'krx_options'
        save_dir.mkdir(parents=True, exist_ok=True)
        df = self.get_options(date)
        if df is None or len(df) == 0:
            return {}
        path = save_dir / f'options_{date}.csv'
        df.to_csv(path, index=False, encoding='utf-8-sig')
        logger.info(f'  💾 옵션 저장: {path.name} ({len(df)}건)')
        summary = {}
        kospi_opt = df[df['PROD_NM'].str.contains('코스피200', na=False)]
        if len(kospi_opt) > 0:
            calls = kospi_opt[kospi_opt['RGHT_TP_NM'] == 'CALL']
            puts = kospi_opt[kospi_opt['RGHT_TP_NM'] == 'PUT']
            call_vol = calls['ACC_TRDVOL'].sum()
            put_vol = puts['ACC_TRDVOL'].sum()
            call_oi = calls['ACC_OPNINT_QTY'].sum()
            put_oi = puts['ACC_OPNINT_QTY'].sum()
            pcr_vol = put_vol / call_vol if call_vol > 0 else 0
            pcr_oi = put_oi / call_oi if call_oi > 0 else 0
            traded = kospi_opt[kospi_opt['ACC_TRDVOL'] > 0]
            avg_iv = traded['IMP_VOLT'].mean() if len(traded) > 0 else 0
            call_iv = traded[traded['RGHT_TP_NM'] == 'CALL']['IMP_VOLT'].mean() if len(traded) > 0 else 0
            put_iv = traded[traded['RGHT_TP_NM'] == 'PUT']['IMP_VOLT'].mean() if len(traded) > 0 else 0
            summary = {'date': date, 'total_options': len(df), 'kospi200_options': len(kospi_opt), 'pcr_volume': round(pcr_vol, 3), 'pcr_open_interest': round(pcr_oi, 3), 'call_volume': int(call_vol), 'put_volume': int(put_vol), 'call_oi': int(call_oi), 'put_oi': int(put_oi), 'avg_iv': round(avg_iv, 2), 'call_iv': round(call_iv, 2), 'put_iv': round(put_iv, 2), 'iv_skew': round(put_iv - call_iv, 2)}
            summary_path = save_dir / f'options_summary_{date}.json'
            with open(summary_path, 'w', encoding='utf-8') as f:
                _json.dump(summary, f, ensure_ascii=False, indent=2)
            logger.info(f'  📊 PCR(거래량): {pcr_vol:.3f} | PCR(미결제): {pcr_oi:.3f}')
            logger.info(f'  📊 IV평균: {avg_iv:.1f}% | 스큐: {summary['iv_skew']:.1f}%')
        return summary

    def get_etf_daily(self, date: str) -> Optional[pd.DataFrame]:
        """ETF 전종목 일별 매매 데이터 (NAV 포함)."""
        data = self._call_api(self.SERVICES['etf_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'NAV', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'MKTCAP', 'INVSTASST_NETASST_TOTAMT', 'LIST_SHRS']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 ETF 일별: {len(df)}종목 ({date})')
        return df

    def get_esg_index(self, date: str) -> Optional[pd.DataFrame]:
        """ESG 지수 일별 데이터."""
        data = self._call_api(self.SERVICES['esg_index'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['CLSPRC_IDX', 'PRV_DD_CMPR', 'UPDN_RATE', 'TRD_ISU_CNT', 'ACC_TRDVOL', 'ACC_TRDVAL']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 ESG 지수: {len(df)}개 ({date})')
        return df

    def get_kosdaq_info(self, date: str) -> Optional[pd.DataFrame]:
        """코스닥 종목 기본정보."""
        data = self._call_api(self.SERVICES['kosdaq_info'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        logger.info(f'  📊 코스닥 기본정보: {len(df)}종목 ({date})')
        return df

    def get_gold_daily(self, date: str) -> Optional[pd.DataFrame]:
        """KRX 금시장 일별 매매 (금 99.99)."""
        data = self._call_api(self.SERVICES['gold_daily'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'ACC_TRDVOL', 'ACC_TRDVAL']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 금시장: {len(df)}건 ({date})')
        return df

    def get_kosdaq_futures(self, date: str) -> Optional[pd.DataFrame]:
        """코스닥 주식선물 일별 매매."""
        data = self._call_api(self.SERVICES['kosdaq_futures'], {'basDd': date})
        if not data:
            return None
        items = data.get('OutBlock_1', [])
        if not items:
            return None
        df = pd.DataFrame(items)
        num_cols = ['TDD_CLSPRC', 'CMPPREVDD_PRC', 'TDD_OPNPRC', 'TDD_HGPRC', 'TDD_LWPRC', 'SPOT_PRC', 'SETL_PRC', 'ACC_TRDVOL', 'ACC_TRDVAL', 'ACC_OPNINT_QTY']
        for col in num_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')
        logger.info(f'  📊 코스닥 주식선물: {len(df)}종목 ({date})')
        return df

    def collect_all_daily(self, date: str) -> Dict:
        """승인된 모든 KRX 서비스 통합 수집."""
        save_base = Path(__file__).resolve().parent.parent.parent / 'data' / 'raw'
        collected = {}
        logger.info(f'\n📡 KRX 전체 수집 — {date}')
        logger.info('=' * 50)
        stock_results = self.collect_all_stock_daily(date)
        collected['stock'] = {k: len(v) for k, v in stock_results.items()}
        for name, func in [('kospi_index', self.get_kospi_index), ('kosdaq_index', self.get_kosdaq_index)]:
            try:
                df = func(date)
                if df is not None:
                    save_dir = save_base / 'krx_index'
                    save_dir.mkdir(parents=True, exist_ok=True)
                    df.to_csv(save_dir / f'{name}_{date}.csv', index=False, encoding='utf-8-sig')
                    collected[name] = len(df)
            except Exception as e:
                logger.warning(f'  ⚠️ {name}: {e}', exc_info=True)
        try:
            ft = self.get_futures(date)
            if ft is not None:
                save_dir = save_base / 'krx_futures'
                save_dir.mkdir(parents=True, exist_ok=True)
                ft.to_csv(save_dir / f'futures_{date}.csv', index=False, encoding='utf-8-sig')
                collected['futures'] = len(ft)
        except Exception as e:
            logger.warning(f'  ⚠️ 선물: {e}', exc_info=True)
        try:
            opt_summary = self.collect_options_daily(date)
            if opt_summary:
                collected['options'] = opt_summary
        except Exception as e:
            logger.warning(f'  ⚠️ 옵션: {e}', exc_info=True)
        try:
            etf = self.get_etf_daily(date)
            if etf is not None:
                save_dir = save_base / 'krx_etf'
                save_dir.mkdir(parents=True, exist_ok=True)
                etf.to_csv(save_dir / f'etf_{date}.csv', index=False, encoding='utf-8-sig')
                collected['etf'] = len(etf)
        except Exception as e:
            logger.warning(f'  ⚠️ ETF: {e}', exc_info=True)
        try:
            esg = self.get_esg_index(date)
            if esg is not None:
                save_dir = save_base / 'krx_esg'
                save_dir.mkdir(parents=True, exist_ok=True)
                esg.to_csv(save_dir / f'esg_index_{date}.csv', index=False, encoding='utf-8-sig')
                collected['esg_index'] = len(esg)
        except Exception as e:
            logger.warning(f'  ⚠️ ESG: {e}', exc_info=True)
        try:
            kinfo = self.get_kosdaq_info(date)
            if kinfo is not None:
                save_dir = save_base / 'krx_stock_daily'
                save_dir.mkdir(parents=True, exist_ok=True)
                kinfo.to_csv(save_dir / f'kosdaq_info_{date}.csv', index=False, encoding='utf-8-sig')
                collected['kosdaq_info'] = len(kinfo)
        except Exception as e:
            logger.warning(f'  ⚠️ 코스닥 기본정보: {e}', exc_info=True)
        try:
            gold = self.get_gold_daily(date)
            if gold is not None:
                save_dir = save_base / 'krx_gold'
                save_dir.mkdir(parents=True, exist_ok=True)
                gold.to_csv(save_dir / f'gold_{date}.csv', index=False, encoding='utf-8-sig')
                collected['gold'] = len(gold)
        except Exception as e:
            logger.warning(f'  ⚠️ 금시장: {e}', exc_info=True)
        try:
            ksq_fut = self.get_kosdaq_futures(date)
            if ksq_fut is not None:
                save_dir = save_base / 'krx_futures'
                save_dir.mkdir(parents=True, exist_ok=True)
                ksq_fut.to_csv(save_dir / f'kosdaq_futures_{date}.csv', index=False, encoding='utf-8-sig')
                collected['kosdaq_futures'] = len(ksq_fut)
        except Exception as e:
            logger.warning(f'  ⚠️ 주식선물 코스닥: {e}', exc_info=True)
        logger.info(f'\n📊 수집 결과:')
        for k, v in collected.items():
            if isinstance(v, dict):
                logger.info(f'  {k}: {v}')
            else:
                logger.info(f'  {k}: {v}건')
        return collected