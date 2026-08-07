import json
import logging
from pathlib import Path
from datetime import datetime
import pandas as pd
import csv

logger = logging.getLogger("CorpLedger")

class CorpLedgerManager:
    """
    법인 장부(원장 및 일계표) 자동 생성 모듈.
    더존(Douzone) / 위하고(WEHAGO) 세무 프로그램에 호환되는 CSV 파일 형식으로 출력합니다.
    """
    
    def __init__(self, data_dir: str = None, output_dir: str = None):
        self.base_path = Path(__file__).resolve().parent.parent.parent
        self.data_dir = Path(data_dir) if data_dir else self.base_path / "results"
        self.output_dir = Path(output_dir) if output_dir else self.base_path / "data" / "ledger"
        
        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.trades_file = self.data_dir / "shadow_trades.json"
        self.portfolio_file = self.data_dir / "shadow_portfolio.json"

    def _calculate_tax(self, row: pd.Series) -> float:
        """
        한국 세법상 증권거래세 계산.
        - 매도(SELL) 시에만 부과.
        - 주식: 0.18% (2024년 기준, 향후 변경 가능성 있으나 현행 유지)
        - ETF: 면제 (0원)
        """
        if row['action'].upper() != 'SELL':
            return 0.0
            
        name = str(row.get('name', ''))
        # ETF는 이름에 KODEX, TIGER, KBSTAR, ACE, ARIRANG, KOSEF 등이 들어감
        etf_keywords = ['KODEX', 'TIGER', 'KBSTAR', 'ACE', 'ARIRANG', 'KOSEF', 'HANARO', 'SOL', 'TIMEFOLIO']
        is_etf = any(kw in name for kw in etf_keywords)
        
        if is_etf:
            return 0.0
        else:
            return float(row['amount']) * 0.0018

    def generate_trade_journal(self, target_month: str = None) -> Path:
        """
        target_month (YYYY-MM) 에 해당하는 거래 원장을 CSV로 추출.
        """
        if not target_month:
            target_month = datetime.now().strftime("%Y-%m")
            
        if not self.trades_file.exists():
            logger.error(f"Trades file not found: {self.trades_file}")
            return None
            
        try:
            with open(self.trades_file, 'r', encoding='utf-8') as f:
                trades_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load trades data: {e}")
            return None
            
        if not trades_data:
            logger.info("No trades data available.")
            return None
            
        df = pd.DataFrame(trades_data)
        
        # 기본 컬럼이 없는 경우 빈 문자열/0으로 처리
        for col in ['date', 'time', 'ticker', 'name', 'action', 'quantity', 'price', 'amount', 'commission', 'realized_pnl', 'stream']:
            if col not in df.columns:
                df[col] = '' if col in ['date', 'time', 'ticker', 'name', 'action', 'stream'] else 0.0
                
        # Fill missing time if any
        if 'time' not in df.columns or df['time'].replace('', pd.NA).isnull().all():
            df['time'] = '15:30:00' # Default to close time if not recorded
        else:
            df['time'] = df['time'].replace('', '15:30:00')
            
        # Extract YYYY-MM
        df['month'] = df['date'].str[:7]
        df_month = df[df['month'] == target_month].copy()
        
        if df_month.empty:
            logger.info(f"No trades found for month: {target_month}")
            return None
            
        # Calculate Tax
        df_month['Tax'] = df_month.apply(self._calculate_tax, axis=1)
        
        # Calculate Net Amount
        def calc_net_amount(row):
            if row['action'].upper() == 'BUY':
                return float(row['amount']) + float(row['commission'])
            else:
                return float(row['amount']) - float(row['commission']) - float(row['Tax'])
                
        df_month['Net_Amount'] = df_month.apply(calc_net_amount, axis=1)
        
        # Format output dataframe
        output_df = pd.DataFrame({
            'Date': df_month['date'],
            'Time': df_month['time'],
            'Ticker': df_month['ticker'],
            'Name': df_month['name'],
            'Type': df_month['action'].str.upper(),
            'Quantity': df_month['quantity'],
            'Price': df_month['price'].round(0),
            'Amount': df_month['amount'].round(0),
            'Commission': df_month['commission'].round(0),
            'Tax': df_month['Tax'].round(0),
            'Net_Amount': df_month['Net_Amount'].round(0),
            'Realized_PnL': df_month['realized_pnl'].round(0),
            'Strategy_ID': df_month['stream']
        })
        
        output_path = self.output_dir / f"corp_trades_{target_month.replace('-', '')}.csv"
        output_df.to_csv(output_path, index=False, encoding='utf-8-sig') # UTF-8-SIG for Excel compatibility in Korea
        logger.info(f"✅ Generated Corporate Trade Journal: {output_path}")
        
        return output_path

    def generate_daily_summary(self, target_month: str = None) -> Path:
        """
        target_month (YYYY-MM) 에 해당하는 일계표(손익 요약)를 CSV로 추출.
        """
        if not target_month:
            target_month = datetime.now().strftime("%Y-%m")
            
        if not self.portfolio_file.exists():
            logger.error(f"Portfolio file not found: {self.portfolio_file}")
            return None
            
        try:
            with open(self.portfolio_file, 'r', encoding='utf-8') as f:
                ptf_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to load portfolio data: {e}")
            return None
            
        snapshots = ptf_data.get('daily_snapshots', [])
        if not snapshots:
            logger.warning("No daily_snapshots found in portfolio data. Generating summary from trades instead (Limited info).")
            return self._generate_summary_from_trades(target_month)
            
        df = pd.DataFrame(snapshots)
        if 'date' not in df.columns:
            logger.error("Snapshots missing 'date' column.")
            return None
            
        df['month'] = df['date'].str[:7]
        df_month = df[df['month'] == target_month].copy()
        
        if df_month.empty:
            logger.info(f"No snapshots found for month: {target_month}")
            return None
            
        output_df = pd.DataFrame({
            'Date': df_month['date'],
            'Total_NAV': df_month.get('total_nav', 0).round(0),
            'Cash_Balance': df_month.get('cash', 0).round(0),
            'Stock_Value': (df_month.get('total_nav', 0) - df_month.get('cash', 0)).round(0),
            'Daily_Realized_PnL': df_month.get('realized_pnl', 0).round(0),
            'Daily_Unrealized_PnL': df_month.get('unrealized_pnl', 0).round(0),
            'Total_Commission': df_month.get('total_commission', 0).round(0)
        })
        
        output_path = self.output_dir / f"corp_daily_summary_{target_month.replace('-', '')}.csv"
        output_df.to_csv(output_path, index=False, encoding='utf-8-sig')
        logger.info(f"✅ Generated Corporate Daily Summary: {output_path}")
        
        return output_path

    def _generate_summary_from_trades(self, target_month: str) -> Path:
        """Fallback: 일별 스냅샷이 없을 경우 거래 내역 기반으로 손익 일계표 생성"""
        if not self.trades_file.exists():
            return None
            
        with open(self.trades_file, 'r', encoding='utf-8') as f:
            trades_data = json.load(f)
            
        df = pd.DataFrame(trades_data)
        if df.empty or 'date' not in df.columns:
            return None
            
        df['month'] = df['date'].str[:7]
        df_month = df[df['month'] == target_month].copy()
        
        if df_month.empty:
            return None
            
        df_month['Tax'] = df_month.apply(self._calculate_tax, axis=1)
        daily_agg = df_month.groupby('date').agg({
            'realized_pnl': 'sum',
            'commission': 'sum',
            'Tax': 'sum'
        }).reset_index()
        
        output_df = pd.DataFrame({
            'Date': daily_agg['date'],
            'Total_NAV': 0, # Cannot know without snapshots
            'Cash_Balance': 0,
            'Stock_Value': 0,
            'Daily_Realized_PnL': daily_agg['realized_pnl'].round(0),
            'Daily_Unrealized_PnL': 0,
            'Total_Commission': daily_agg['commission'].round(0),
            'Total_Tax': daily_agg['Tax'].round(0)
        })
        
        output_path = self.output_dir / f"corp_daily_summary_{target_month.replace('-', '')}.csv"
        output_df.to_csv(output_path, index=False, encoding='utf-8-sig')
        logger.info(f"✅ Generated Corporate Daily Summary (Fallback): {output_path}")
        
        return output_path
