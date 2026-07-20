import logging
from typing import Dict, List, Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

class TaxOptimizer:
    """
    [Project Meridian] Tax-Alpha (손익통산 자동화) 전담 모듈
    S0/S1의 확정 이익(Realized Gain)과 S3/S10의 미실현 손실(Unrealized Loss)을 상계하여
    법인세 과세표준 구간을 낮추는 (Tax Loss Harvesting) 기능을 수행합니다.
    """
    def __init__(self, target_tax_bracket_profit: float = 200_000_000.0, harvest_loss_threshold: float = -0.10):
        # 법인세 9.9% (2억 이하) 유지 목표
        self.target_tax_bracket_profit = target_tax_bracket_profit 
        # 손실 확정을 시작할 최소 마이너스 수익률 (-10%)
        self.harvest_loss_threshold = harvest_loss_threshold

    def calculate_tax_alpha_signals(self, 
                                    realized_gains: float, 
                                    portfolio: Dict[str, Dict], 
                                    historical_prices: pd.DataFrame,
                                    current_prices: Dict[str, float]) -> List[Dict]:
        """
        확정 이익을 기반으로 미실현 손실을 수확(Sell)하고 대체재(Buy)를 찾는 시그널을 생성.
        
        Args:
            realized_gains: 올해 누적 확정 이익
            portfolio: 현재 보유 중인 현물 포트폴리오 (S3, S10) - {'ticker': {'qty': 100, 'entry_price': 50000}}
            historical_prices: 최근 60일 이상의 일간 종가 데이터프레임 (상관계수 계산용)
            current_prices: 현재가 정보
            
        Returns:
            생성된 매도/매수 시그널 리스트
        """
        signals = []
        
        # 1. 과표 초과분 확인
        if realized_gains <= self.target_tax_bracket_profit:
            logger.debug(f"[Tax-Alpha] 확정 이익({realized_gains:,.0f}원)이 타겟 과표({self.target_tax_bracket_profit:,.0f}원) 이하입니다. 손실 수확 스킵.")
            return signals
            
        excess_profit = realized_gains - self.target_tax_bracket_profit
        logger.info(f"[Tax-Alpha] 과표 초과분 발생! {excess_profit:,.0f}원. Tax Loss Harvesting 가동.")
        
        # 2. 미실현 손실 스캔
        unrealized_losses = []
        for ticker, pos in portfolio.items():
            entry_price = pos['entry_price']
            current_price = current_prices.get(ticker, entry_price)
            qty = pos['qty']
            
            pnl_pct = (current_price / entry_price) - 1.0
            pnl_amt = (current_price - entry_price) * qty
            
            if pnl_pct <= self.harvest_loss_threshold and pnl_amt < 0:
                unrealized_losses.append({
                    'ticker': ticker,
                    'pnl_pct': pnl_pct,
                    'pnl_amt': pnl_amt,
                    'qty': qty,
                    'current_price': current_price
                })
        
        # 손실이 큰 순서대로 정렬 (가장 효율적인 절세)
        unrealized_losses.sort(key=lambda x: x['pnl_amt'])
        
        # 3. 손실 확정 및 Proxy 매수 시그널 생성
        harvested_loss = 0.0
        
        for loss_pos in unrealized_losses:
            if harvested_loss <= -excess_profit:
                break # 이미 목표만큼 세금을 줄였으면 중단
                
            ticker = loss_pos['ticker']
            pnl_amt = loss_pos['pnl_amt']
            
            # 매도(SELL) 시그널
            signals.append({
                'ticker': ticker,
                'action': 'SELL',
                'qty': loss_pos['qty'],
                'reason': 'TAX_LOSS_HARVESTING',
                'confidence': 1.0 # 강제 집행
            })
            harvested_loss += pnl_amt
            logger.info(f"[Tax-Alpha] 손실 확정 매도: {ticker} (예상 손실액: {pnl_amt:,.0f}원)")
            
            # 대체재(Proxy) 매수 (Wash-Sale 회피)
            exclude_tickers = [x['ticker'] for x in unrealized_losses]
            proxy_ticker = self._find_dynamic_proxy(ticker, historical_prices, exclude_tickers=exclude_tickers)
            
            if proxy_ticker:
                # 판 금액 그대로 대체재 매수 (베타 복구)
                harvest_capital = loss_pos['qty'] * loss_pos['current_price']
                proxy_price = current_prices.get(proxy_ticker, historical_prices[proxy_ticker].iloc[-1] if proxy_ticker in historical_prices else 1.0)
                proxy_qty = int(harvest_capital / proxy_price) if proxy_price > 0 else 0
                
                if proxy_qty > 0:
                    signals.append({
                        'ticker': proxy_ticker,
                        'action': 'BUY',
                        'qty': proxy_qty,
                        'reason': 'TAX_PROXY_REPLACEMENT',
                        'confidence': 1.0
                    })
                    logger.info(f"[Tax-Alpha] Wash-Sale 회피 대체재 매수: {proxy_ticker} (매수 금액: {harvest_capital:,.0f}원)")
            else:
                logger.warning(f"[Tax-Alpha] {ticker}에 대한 적절한 Proxy(상관계수 0.85 이상)를 찾지 못했습니다. 현금 보유.")
                
        logger.info(f"[Tax-Alpha] 총 {len(signals)//2} 종목 교체 완료. (예상 절세액: {abs(harvested_loss) * 0.19:,.0f}원)")
        return signals

    def _find_dynamic_proxy(self, target_ticker: str, historical_prices: pd.DataFrame, min_correlation: float = 0.85, exclude_tickers: List[str] = None) -> Optional[str]:
        """
        가장매매(Wash-Sale) 규정을 피하기 위해, 타겟 종목과 상관계수가 가장 높은 다른 종목(Proxy)을 실시간으로 찾습니다.
        실제 퀀트 펀드의 Direct Indexing 기법(Tracking Error 최소화)을 모사합니다.
        """
        if historical_prices.empty or target_ticker not in historical_prices.columns:
            return None
            
        exclude_tickers = exclude_tickers or []
        target_series = historical_prices[target_ticker]
        
        best_proxy = None
        best_corr = -1.0
        
        for candidate in historical_prices.columns:
            if candidate == target_ticker or candidate in exclude_tickers:
                continue
                
            # 수익률(Return) 기준 상관계수 계산
            target_returns = target_series.pct_change().dropna()
            candidate_returns = historical_prices[candidate].pct_change().dropna()
            
            # 데이터 길이가 다를 수 있으므로 정렬
            aligned = pd.concat([target_returns, candidate_returns], axis=1, join='inner').dropna()
            if len(aligned) < 20: # 최소 20일 데이터 필요
                continue
                
            corr = aligned.iloc[:, 0].corr(aligned.iloc[:, 1])
            
            if corr >= min_correlation and corr > best_corr:
                best_corr = corr
                best_proxy = candidate
                
        if best_proxy:
            logger.debug(f"[Tax-Alpha] {target_ticker} Proxy 매칭 완료: {best_proxy} (상관계수: {best_corr:.3f})")
            
        return best_proxy
