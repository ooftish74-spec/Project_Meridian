"""
Project_First — Investment Universe
=====================================
투자 유니버스 정의. 모든 거래 가능 종목을 중앙 관리.

Usage:
    from config.universe import Universe
    u = Universe()
    etfs = u.get_a1_etfs('bull')
    sectors = u.get_sector_etfs()
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ETFInfo:
    """ETF 종목 정보."""
    ticker: str
    name: str
    category: str           # directional, sector, bond, gold, dollar, cash, global
    leverage: float = 1.0   # 1.0, 2.0, -1.0, -2.0
    expense_ratio: float = 0.003
    market: str = 'KR'      # KR, US (KR상장 해외ETF도 KR)
    sector: Optional[str] = None
    notes: str = ''


@dataclass
class AccountConfig:
    """절세계좌 설정."""
    name: str
    tax_free_limit: int = 0
    tax_rate: float = 0.154
    risk_asset_limit: float = 1.0
    rebalance_frequency: str = 'monthly'


class Universe:
    """투자 유니버스 관리자."""

    # ═══════════════════════════════════════════════════════
    # A1: 방향성 ETF
    # ═══════════════════════════════════════════════════════
    A1_DIRECTIONAL: Dict[str, ETFInfo] = {
        # 지수 레버리지/인버스
        'bull_2x':    ETFInfo('122630', 'KODEX 레버리지', 'directional', 2.0, 0.0064),
        'bear_2x':    ETFInfo('252670', 'KODEX 인버스2X', 'directional', -2.0, 0.0064),
        'bull_1x':    ETFInfo('069500', 'KODEX 200', 'directional', 1.0, 0.0015),
        'bear_1x':    ETFInfo('114800', 'KODEX 인버스', 'directional', -1.0, 0.0045),
        # KOSDAQ
        'kosdaq_2x':  ETFInfo('233740', 'KODEX 코스닥150 레버리지', 'directional', 2.0, 0.0064),
        # KR 상장 해외 지수
        'nasdaq_1x':  ETFInfo('133690', 'TIGER 나스닥100', 'global', 1.0, 0.0007),
        'sp500_1x':   ETFInfo('360200', 'KODEX 미국S&P500', 'global', 1.0, 0.0009),
        'nasdaq_2x':  ETFInfo('453010', 'TIGER 미국나스닥100레버리지', 'global', 2.0, 0.0045),
        # 단일 종목 인버스 (S1 Tactic C용)
        'sec_inv':    ETFInfo('470460', 'KODEX 삼성전자 인버스', 'directional', -1.0),
        'hynix_inv':  ETFInfo('470490', 'TIGER SK하이닉스 인버스', 'directional', -1.0),
    }

    # ═══════════════════════════════════════════════════════
    # A2: 섹터 로테이션 ETF
    # ═══════════════════════════════════════════════════════
    A2_SECTORS: Dict[str, ETFInfo] = {
        'semiconductor': ETFInfo('091160', 'KODEX 반도체', 'sector', sector='반도체'),
        'battery':       ETFInfo('305540', 'TIGER 2차전지테마', 'sector', sector='2차전지'),
        'bio':           ETFInfo('266420', 'KODEX 바이오', 'sector', sector='바이오'),
        'energy':        ETFInfo('117460', 'KODEX 에너지화학', 'sector', sector='에너지화학'),
        'auto':          ETFInfo('091170', 'KODEX 자동차', 'sector', sector='자동차'),
        'steel':         ETFInfo('117680', 'KODEX 철강', 'sector', sector='철강'),
        'bank':          ETFInfo('091220', 'KODEX 은행', 'sector', sector='금융'),
        'construction':  ETFInfo('117700', 'KODEX 건설', 'sector', sector='건설'),
        'consumer':      ETFInfo('227560', 'TIGER 200 생활소비재', 'sector', sector='소비재'),
        'it':            ETFInfo('139260', 'TIGER 200 IT', 'sector', sector='IT'),
        'media':         ETFInfo('228810', 'TIGER 미디어콘텐츠', 'sector', sector='미디어'),
        'defense':       ETFInfo('449450', 'PLUS K방산', 'sector', sector='방산'),
    }

    # ═══════════════════════════════════════════════════════
    # 자산배분 ETF (리스크 관리용)
    # ═══════════════════════════════════════════════════════
    ASSET_ALLOCATION: Dict[str, ETFInfo] = {
        'kr_bond_10y':   ETFInfo('148070', 'KODEX 국고채10년', 'bond'),
        'us_bond_10y':   ETFInfo('305080', 'TIGER 미국채10년선물', 'bond'),
        'gold':          ETFInfo('132030', 'KODEX 골드선물(H)', 'gold'),
        'dollar':        ETFInfo('261240', 'KODEX 미국달러선물', 'dollar'),
        'short_bond':    ETFInfo('214980', 'KODEX 단기채권PLUS', 'cash'),
        'money_market':  ETFInfo('357870', 'TIGER CD금리투자KIS(합성)', 'cash'),
    }

    # ═══════════════════════════════════════════════════════
    # 슬리브 B: 절세계좌 ETF
    # ═══════════════════════════════════════════════════════
    SLEEVE_B_ETFS: Dict[str, ETFInfo] = {
        # 글로벌
        'tiger_nasdaq':  ETFInfo('133690', 'TIGER 나스닥100', 'global'),
        'kodex_sp500':   ETFInfo('360200', 'KODEX 미국S&P500', 'global'),
        'tiger_msci':    ETFInfo('195930', 'TIGER MSCI선진국', 'global'),
        # KR 배당
        'kodex_dividend': ETFInfo('279530', 'KODEX 고배당', 'dividend'),
        'tiger_dividend': ETFInfo('161510', 'TIGER 배당성장', 'dividend'),
        # ★ 미국 배당/성장 ETF (KR 상장, Phase 2 확장)
        'tiger_us_dividend':  ETFInfo('458730', 'TIGER 미국배당다우존스', 'us_dividend',
                                       notes='미국 배당귀족 기반, 분기 배당'),
        'kodex_us_dividend':  ETFInfo('441640', 'KODEX 미국배당프리미엄액티브', 'us_dividend',
                                       notes='커버드콜+배당, 월배당'),
        'tiger_us_tech':      ETFInfo('381170', 'TIGER 미국테크TOP10 INDXX', 'us_growth',
                                       notes='AAPL/MSFT/NVDA 등 집중'),
        'kodex_us_semi':      ETFInfo('453640', 'KODEX 미국반도체MV', 'us_sector',
                                       notes='미국 반도체 섹터'),
        'tiger_covered_call': ETFInfo('166400', 'TIGER 200 커버드콜ATM', 'covered_call',
                                       notes='인컴+헤지, 월배당'),
        # 채권/안전자산
        'kr_bond':       ETFInfo('148070', 'KODEX 국고채10년', 'bond'),
        'us_bond':       ETFInfo('305080', 'TIGER 미국채10년선물', 'bond'),
        'gold':          ETFInfo('132030', 'KODEX 골드선물(H)', 'gold'),
        'tiger_reits':       ETFInfo('329200', 'TIGER 리츠부동산인프라', 'reits'),
    }

    # ═══════════════════════════════════════════════════════
    # 시그널 전용 (거래하지 않음)
    # ═══════════════════════════════════════════════════════
    # ── 모닝 수집 (06:00 KST = US 장 마감 후) ──
    SIGNAL_MORNING: Dict[str, str] = {
        'VIX':     '^VIX',
        'SP500':   '^GSPC',
        'NASDAQ':  '^IXIC',
        'SOX':     '^SOX',       # ★ 필라델피아 반도체 — KOSPI 반도체 35% 직결
        'DJI':     '^DJI',       # ★ 다우존스
        'US10Y':   '^TNX',
        'US5Y':    '^FVX',      # ★ 5년물 → Yield Curve 10Y-5Y spread 계산
        'US30Y':   '^TYX',      # ★ 30년물 → 장기 금리 추적
        'DXY':     'DX-Y.NYB',
        'WTI':     'CL=F',
        'COPPER':  'HG=F',       # ★ Dr. Copper — 글로벌 경기 선행
        'GOLD_US': 'GC=F',
        'SILVER':  'SI=F',       # ★ 은 — 산업 수요 + 인플레
        'USDKRW':  'KRW=X',
        'USDJPY':  'USDJPY=X',   # ★ 엔캐리 해소 시그널
        'EURUSD':  'EURUSD=X',   # ★ 달러 사이클
        'EWY':     'EWY',        # ★ SGX KOSPI 프록시 (상관 0.95+)
        'FLKR':    'FLKR',       # ★ SGX 보조 프록시
        'FXI':     'FXI',        # ★ 중국 대형주 → 한국 수출 25%
        'KOSPI':   '^KS11',      # ★ KOSPI 종합지수 — 핵심 지표 직접 수집
        'KOSDAQ':  '^KQ11',      # ★ KOSDAQ 종합지수
    }

    # ── 이브닝 수집 (17:00 KST = 아시아 장 마감 후) ──
    SIGNAL_EVENING: Dict[str, str] = {
        'TAIEX':    '^TWII',     # ★ 대만 — TSMC-삼성 반도체 싸이클 (13:30 KST 마감)
        'NIKKEI':   '^N225',     # ★ 닛케이 — 아시아 센티먼트 (15:00 KST 마감)
        'HANGSENG': '^HSI',      # ★ 항셍 — 중국 경기 프록시 (16:00 KST 마감)
    }

    # ── 전체 (legacy 호환) ──
    SIGNAL_ONLY: Dict[str, str] = {
        **SIGNAL_MORNING,
        **SIGNAL_EVENING,
    }

    # ═══════════════════════════════════════════════════════
    # 계좌 설정
    # ═══════════════════════════════════════════════════════
    ACCOUNTS: Dict[str, AccountConfig] = {
        'ISA':       AccountConfig('ISA', tax_free_limit=2_000_000, tax_rate=0.099),
        'PENSION':   AccountConfig('개인연금', risk_asset_limit=0.70),
        'IRP':       AccountConfig('IRP', risk_asset_limit=0.30),
        'BROKERAGE': AccountConfig('종합계좌', tax_rate=0.154, rebalance_frequency='quarterly'),
        'CMA':       AccountConfig('CMA', rebalance_frequency='none'),
    }

    # ═══════════════════════════════════════════════════════
    # 매크로 → 섹터 연동 규칙
    # ═══════════════════════════════════════════════════════
    MACRO_SECTOR_RULES: Dict[str, Dict[str, float]] = {
        # 레짐+매크로 조건 → 섹터 가중치 보너스
        'bull_rate_down':  {'semiconductor': 0.3, 'battery': 0.2, 'it': 0.2, 'bio': 0.1},
        'bull_rate_up':    {'bank': 0.3, 'construction': 0.2, 'steel': 0.1},
        'bear_oil_up':     {'energy': 0.3, 'steel': 0.1},
        'bear_oil_down':   {'consumer': 0.2, 'bio': 0.1},
        'crash':           {},  # 모든 섹터 언더웨이트
    }

    def get_a1_etfs(self, direction: str = 'bull') -> List[ETFInfo]:
        """A1 방향별 ETF 목록."""
        if direction == 'bull':
            return [self.A1_DIRECTIONAL[k] for k in ['bull_2x', 'bull_1x', 'nasdaq_1x']]
        elif direction == 'bear':
            return [self.A1_DIRECTIONAL[k] for k in ['bear_2x', 'bear_1x']]
        return list(self.A1_DIRECTIONAL.values())

    def get_sector_etfs(self) -> List[ETFInfo]:
        """A2 섹터 ETF 전체 목록."""
        return list(self.A2_SECTORS.values())

    def get_sector_tickers(self) -> List[str]:
        """A2 섹터 ETF 티커 목록."""
        return [etf.ticker for etf in self.A2_SECTORS.values()]

    def get_asset_allocation_etfs(self) -> Dict[str, ETFInfo]:
        """자산배분 ETF."""
        return dict(self.ASSET_ALLOCATION)

    def get_account(self, name: str) -> Optional[AccountConfig]:
        """계좌 설정 조회."""
        return self.ACCOUNTS.get(name)

    def get_all_kr_tickers(self) -> List[str]:
        """모든 KR 거래 가능 티커."""
        tickers = set()
        for etf in self.A1_DIRECTIONAL.values():
            tickers.add(etf.ticker)
        for etf in self.A2_SECTORS.values():
            tickers.add(etf.ticker)
        for etf in self.ASSET_ALLOCATION.values():
            tickers.add(etf.ticker)
        for etf in self.SLEEVE_B_ETFS.values():
            tickers.add(etf.ticker)
        return sorted(tickers)

    def lookup_ticker(self, ticker: str) -> Optional[ETFInfo]:
        """티커로 ETF 정보 조회."""
        for store in [self.A1_DIRECTIONAL, self.A2_SECTORS,
                      self.ASSET_ALLOCATION, self.SLEEVE_B_ETFS]:
            for etf in store.values():
                if etf.ticker == ticker:
                    return etf
        return None


if __name__ == '__main__':
    u = Universe()
    print(f"A1 Bull ETFs: {[e.name for e in u.get_a1_etfs('bull')]}")
    print(f"A2 Sectors: {[e.name for e in u.get_sector_etfs()]}")
    print(f"All KR tickers: {len(u.get_all_kr_tickers())}종목")
    print(f"Accounts: {list(u.ACCOUNTS.keys())}")
