#!/usr/bin/env python3
"""
V6 보조 데이터 백필 스크립트
============================
OHLCV 10년 데이터 → Sentiment price_proxy + DART daily_signal 재생성
→ ML 학습 커버리지를 6개월→10년으로 확장

공식 (기존 daily_signal.csv에서 역추적):
  sentiment_mean = 0.5 + 2.0 * ret_5d
  sentiment_std  = 4.743 * rolling_std_20d(daily_return)
  volume_ratio   = volume / volume_20d_ma

Usage:
  python scripts/backfill_aux_data.py              # 전체 백필
  python scripts/backfill_aux_data.py --sentiment   # Sentiment만
  python scripts/backfill_aux_data.py --dart        # DART만
  python scripts/backfill_aux_data.py --flow        # Flow만
  python scripts/backfill_aux_data.py --retrain     # 백필 후 즉시 재학습
"""

import argparse
from src.infra.safe_io import atomic_write_dataframe
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def _load_ohlcv(path: Path) -> pd.DataFrame:
    """Parquet 로드 + date 컬럼 표준화.

    일부 파일은 date가 컬럼이고, 일부는 인덱스입니다.
    어느 경우든 'date' 컬럼이 있는 DataFrame을 반환합니다.
    """
    df = pd.read_parquet(path)

    # date가 인덱스인 경우
    if 'date' not in df.columns:
        if df.index.name == 'date' or pd.api.types.is_datetime64_any_dtype(df.index):
            df = df.reset_index()
            if df.columns[0] != 'date':
                df = df.rename(columns={df.columns[0]: 'date'})
        else:
            # 인덱스가 날짜형인지 확인
            try:
                df.index = pd.to_datetime(df.index)
                df = df.reset_index()
                df = df.rename(columns={df.columns[0]: 'date'})
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                raise KeyError("date column not found")

    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    return df



# ═══════════════════════════════════════════════════════
# 1. Sentiment price_proxy 백필 (OHLCV → 10년)
# ═══════════════════════════════════════════════════════

def backfill_sentiment(ohlcv_dir: Path, sentiment_dir: Path,
                       min_days: int = 100) -> dict:
    """OHLCV 데이터로 price_proxy sentiment를 10년치 생성.

    기존 real news sentiment가 있는 날은 보존하고,
    price_proxy 구간만 10년으로 확장합니다.

    Args:
        ohlcv_dir: data/kr_markets
        sentiment_dir: data/sentiment
        min_days: 최소 OHLCV 행 수

    Returns:
        {'backfilled': int, 'skipped': int, 'total_rows': int}
    """
    logger.info("═══ Sentiment price_proxy 백필 ═══")
    stats = {'backfilled': 0, 'skipped': 0, 'total_rows': 0}

    parquet_files = sorted(ohlcv_dir.glob('kr_*.parquet'))
    total = len(parquet_files)
    logger.info(f"  대상: {total}종목")

    for i, pf in enumerate(parquet_files, 1):
        ticker = pf.stem.replace('kr_', '')
        try:
            df = _load_ohlcv(pf)
            if len(df) < min_days:
                stats['skipped'] += 1
                continue

            # ── price_proxy 계산 ──
            close = pd.to_numeric(df['close'], errors='coerce')
            volume = pd.to_numeric(df['volume'], errors='coerce')

            ret_1d = close.pct_change()
            ret_5d = close.pct_change(5)
            ret_std_20d = ret_1d.rolling(20).std()
            vol_20d_ma = volume.rolling(20).mean()

            # 공식: 기존 daily_signal에서 역추적 (R²=1.0)
            sent_mean = 0.5 + 2.0 * ret_5d
            sent_std = 4.743 * ret_std_20d
            vol_ratio = volume / vol_20d_ma

            # clamp
            sent_mean = sent_mean.clip(0.0, 1.0)
            sent_std = sent_std.clip(0.0, 0.5)
            vol_ratio = vol_ratio.clip(0.0, 10.0)

            # DataFrame 생성 (20일 이후부터 유효)
            proxy_df = pd.DataFrame({
                'date': df['date'].dt.strftime('%Y-%m-%d'),
                'news_sentiment_mean': sent_mean.round(4),
                'news_sentiment_std': sent_std.round(4),
                'news_count': 0,
                'volume_ratio': vol_ratio.round(4),
                'source': 'price_proxy',
                'news_positive_total': np.nan,
                'news_negative_total': np.nan,
                'news_pos_ratio': np.nan,
                'news_intensity': np.nan,
            })

            # 유효한 행만 (20일 이후)
            proxy_df = proxy_df.dropna(subset=['news_sentiment_mean',
                                                'news_sentiment_std',
                                                'volume_ratio'])

            # ── 기존 real news 보존 ──
            out_dir = sentiment_dir / ticker
            out_file = out_dir / 'daily_signal.csv'

            if out_file.exists():
                existing = pd.read_csv(out_file)
                if 'source' in existing.columns:
                    # real news (source != price_proxy)만 보존
                    real_news = existing[existing['source'] != 'price_proxy']
                    if len(real_news) > 0:
                        real_dates = set(real_news['date'])
                        proxy_df = proxy_df[~proxy_df['date'].isin(real_dates)]
                        proxy_df = pd.concat([proxy_df, real_news],
                                             ignore_index=True)
                else:
                    # source 컬럼 없는 경우 — 모든 기존 데이터 보존
                    existing_dates = set(existing['date'])
                    proxy_df = proxy_df[~proxy_df['date'].isin(existing_dates)]
                    proxy_df = pd.concat([proxy_df, existing],
                                         ignore_index=True)

            # 정렬 후 저장
            proxy_df = proxy_df.sort_values('date').reset_index(drop=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_dataframe(proxy_df, out_file, file_format='csv', index=False)

            stats['backfilled'] += 1
            stats['total_rows'] += len(proxy_df)

            if i % 50 == 0:
                logger.info(f"  [{i}/{total}] {ticker}: {len(proxy_df)}행")

        except Exception as e:
            logger.warning(f"  {ticker}: 실패 — {e}")
            stats['skipped'] += 1

    logger.info(f"  ✅ Sentiment 백필: {stats['backfilled']}종목, "
                f"총 {stats['total_rows']:,}행")
    return stats


# ═══════════════════════════════════════════════════════
# 2. DART daily_signal 백필 (insider_trades → 2024-03~)
# ═══════════════════════════════════════════════════════

def backfill_dart(dart_dir: Path, ohlcv_dir: Path) -> dict:
    """DART raw 데이터(insider_trades, financial_summary)로 daily_signal 재생성.

    기존 daily_signal은 2개월만 있지만, insider_trades raw는 2024-03부터 존재.
    이를 활용해 daily_signal을 전체 기간으로 확장합니다.

    Args:
        dart_dir: data/dart
        ohlcv_dir: data/kr_markets (거래일 참조용)

    Returns:
        {'backfilled': int, 'total_rows': int}
    """
    logger.info("\n═══ DART daily_signal 백필 ═══")
    stats = {'backfilled': 0, 'skipped': 0, 'total_rows': 0}

    ticker_dirs = sorted([d for d in dart_dir.iterdir() if d.is_dir()])
    total = len(ticker_dirs)
    logger.info(f"  대상: {total}종목")

    for i, td in enumerate(ticker_dirs, 1):
        ticker = td.name
        try:
            # ── 거래일 목록 가져오기 ──
            pf = ohlcv_dir / f'kr_{ticker}.parquet'
            if not pf.exists():
                stats['skipped'] += 1
                continue

            ohlcv = _load_ohlcv(pf)
            ohlcv['date_str'] = ohlcv['date'].dt.strftime('%Y-%m-%d')
            trading_dates = sorted(ohlcv['date_str'].unique())

            # ── insider_trades 로드 ──
            insider_file = td / 'insider_trades.csv'
            insider_dates = set()
            insider_buy_dates = set()
            insider_sell_dates = set()

            if insider_file.exists():
                insider = pd.read_csv(insider_file)
                if 'date' in insider.columns and 'change_dir' in insider.columns:
                    for _, row in insider.iterrows():
                        d = str(row['date'])[:10]
                        insider_dates.add(d)
                        cd = str(row.get('change_dir', '')).lower()
                        if '취득' in cd or '매수' in cd or 'buy' in cd:
                            insider_buy_dates.add(d)
                        elif '처분' in cd or '매도' in cd or 'sell' in cd:
                            insider_sell_dates.add(d)

            # ── buyback 로드 ──
            buyback_file = td / 'buyback_events.csv'
            buyback_dates = set()
            if buyback_file.exists():
                try:
                    bb = pd.read_csv(buyback_file)
                    if 'date' in bb.columns:
                        buyback_dates = set(bb['date'].astype(str).str[:10])
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass

            # ── major_shareholders 로드 ──
            major_file = td / 'major_shareholders.csv'
            major_dates = set()
            if major_file.exists():
                try:
                    mj = pd.read_csv(major_file)
                    if 'date' in mj.columns:
                        major_dates = set(mj['date'].astype(str).str[:10])
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass

            # ── financial_summary → earnings_surprise 근사 ──
            fin_file = td / 'financial_summary.csv'
            earnings_dates = {}  # date → surprise_score
            if fin_file.exists():
                try:
                    fin = pd.read_csv(fin_file)
                    if 'date' in fin.columns or 'period' in fin.columns:
                        date_col = 'date' if 'date' in fin.columns else 'period'
                        for _, row in fin.iterrows():
                            d = str(row[date_col])[:10]
                            earnings_dates[d] = 0.0  # 기본 neutral
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass

            # ── 최소 raw 시작일 결정 ──
            all_raw_dates = (insider_dates | buyback_dates |
                             major_dates | set(earnings_dates.keys()))
            if not all_raw_dates and not insider_file.exists():
                stats['skipped'] += 1
                continue

            # raw 데이터가 있으면 그 시작일부터, 없으면 2024-01-01부터
            if all_raw_dates:
                min_date = min(all_raw_dates)
            else:
                min_date = '2024-01-01'

            # ── daily_signal 생성 ──
            rows = []
            for td_str in trading_dates:
                if td_str < min_date:
                    continue

                # dart_insider: 매수 +1, 매도 -1, 없으면 0
                dart_insider = 0.0
                if td_str in insider_buy_dates:
                    dart_insider = 1.0
                elif td_str in insider_sell_dates:
                    dart_insider = -1.0

                # dart_buyback: 자사주 매입 +1
                dart_buyback = 1.0 if td_str in buyback_dates else 0.0

                # dart_major: 대량보유 변동
                dart_major = 1.0 if td_str in major_dates else 0.0

                # dart_earnings_surprise
                dart_earnings = earnings_dates.get(td_str, 0.0)

                # dart_composite: 가중 합산
                dart_composite = (dart_insider * 0.4 +
                                  dart_buyback * 0.3 +
                                  dart_major * 0.2 +
                                  dart_earnings * 0.1)

                rows.append({
                    'date': td_str,
                    'dart_insider': dart_insider,
                    'dart_buyback': dart_buyback,
                    'dart_major': dart_major,
                    'dart_earnings_surprise': dart_earnings,
                    'dart_composite': round(dart_composite, 4),
                })

            if not rows:
                stats['skipped'] += 1
                continue

            signal_df = pd.DataFrame(rows)
            signal_df = signal_df.sort_values('date').reset_index(drop=True)

            # 저장 (기존 파일 덮어쓰기)
            out_file = td / 'daily_signal.csv'
            atomic_write_dataframe(signal_df, out_file, file_format='csv', index=False)

            stats['backfilled'] += 1
            stats['total_rows'] += len(signal_df)

            if i % 50 == 0:
                logger.info(f"  [{i}/{total}] {ticker}: {len(signal_df)}행 "
                            f"({signal_df['date'].iloc[0]}~{signal_df['date'].iloc[-1]})")

        except Exception as e:
            logger.warning(f"  {ticker}: 실패 — {e}")
            stats['skipped'] += 1

    logger.info(f"  ✅ DART 백필: {stats['backfilled']}종목, "
                f"총 {stats['total_rows']:,}행")
    return stats


# ═══════════════════════════════════════════════════════
# 3. Investor Flow 백필 (OHLCV proxy)
# ═══════════════════════════════════════════════════════

def backfill_flow(ohlcv_dir: Path, flow_dir: Path,
                  min_days: int = 100) -> dict:
    """OHLCV에서 외국인/기관 수급 proxy 생성.

    pykrx API가 현재 환경에서 작동하지 않으므로,
    가격/거래량 기반 proxy를 생성합니다:
      - foreign_net_buy_norm ≈ 상승일 거래량 비율 기반
      - inst_net_buy_norm ≈ 대량거래 + 상승 패턴 기반
      - foreign_ratio_feat ≈ 거래량 가중 수익률 proxy
      - short_proxy_score ≈ 급락 + 거래량 급증 패턴

    Args:
        ohlcv_dir: data/kr_markets
        flow_dir: data/investor_flow

    Returns:
        {'backfilled': int, 'total_rows': int}
    """
    logger.info("\n═══ Investor Flow proxy 백필 ═══")
    stats = {'backfilled': 0, 'skipped': 0, 'total_rows': 0}

    parquet_files = sorted(ohlcv_dir.glob('kr_*.parquet'))
    total = len(parquet_files)
    logger.info(f"  대상: {total}종목")

    for i, pf in enumerate(parquet_files, 1):
        ticker = pf.stem.replace('kr_', '')
        try:
            df = _load_ohlcv(pf)
            if len(df) < min_days:
                stats['skipped'] += 1
                continue

            close = pd.to_numeric(df['close'], errors='coerce')
            volume = pd.to_numeric(df['volume'], errors='coerce')
            high = pd.to_numeric(df['high'], errors='coerce')
            low = pd.to_numeric(df['low'], errors='coerce')

            ret_1d = close.pct_change()
            vol_20 = volume.rolling(20).mean()
            vol_ratio = volume / vol_20

            # ── proxy 피처 계산 ──

            # foreign_net_buy_norm: 상승일 거래량 가중 수익률의 20일 EMA
            up_vol = np.where(ret_1d > 0, volume * ret_1d, 0)
            down_vol = np.where(ret_1d < 0, volume * ret_1d.abs(), 0)
            net_flow_raw = pd.Series(up_vol - down_vol, index=df.index)
            net_flow_norm = net_flow_raw / (volume * close + 1e-10)
            foreign_proxy = net_flow_norm.ewm(span=20).mean().clip(-1, 1)

            # inst_net_buy_norm: 대량거래(vol_ratio>1.5) + 상승 패턴
            big_vol_up = (vol_ratio > 1.5) & (ret_1d > 0)
            big_vol_down = (vol_ratio > 1.5) & (ret_1d < 0)
            inst_signal = big_vol_up.astype(float) - big_vol_down.astype(float)
            inst_proxy = inst_signal.rolling(10).mean().clip(-1, 1)

            # foreign_ratio_feat: 거래량 가중 모멘텀 (5일 vs 20일)
            vwap_5 = (close * volume).rolling(5).sum() / volume.rolling(5).sum()
            vwap_20 = (close * volume).rolling(20).sum() / volume.rolling(20).sum()
            foreign_ratio = ((vwap_5 / vwap_20) - 1).clip(-0.2, 0.2)

            # short_proxy_score: 급락 + 거래량 급증 패턴
            sharp_drop = (ret_1d < -0.02) & (vol_ratio > 2.0)
            short_proxy = sharp_drop.astype(float).rolling(10).mean().clip(0, 1)

            flow_df = pd.DataFrame({
                'date': df['date'].dt.strftime('%Y-%m-%d'),
                'close': close,
                'volume': volume,
                'inst_net_buy': 0,
                'foreign_net_buy': 0,
                'foreign_net_buy_norm': foreign_proxy.round(6),
                'inst_net_buy_norm': inst_proxy.round(6),
                'foreign_ratio_feat': foreign_ratio.round(6),
                'short_proxy_score': short_proxy.round(6),
                'source': 'ohlcv_proxy',
            })

            flow_df = flow_df.dropna(subset=['foreign_net_buy_norm',
                                              'inst_net_buy_norm'])

            # ── 기존 real flow 보존 ──
            out_dir = flow_dir / ticker
            out_file = out_dir / 'daily_flow.csv'

            if out_file.exists():
                existing = pd.read_csv(out_file)
                if 'source' in existing.columns:
                    real = existing[existing['source'] != 'ohlcv_proxy']
                    if len(real) > 0:
                        real_dates = set(real['date'].astype(str))
                        flow_df = flow_df[~flow_df['date'].isin(real_dates)]
                        flow_df = pd.concat([flow_df, real], ignore_index=True)

            flow_df = flow_df.sort_values('date').reset_index(drop=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_dataframe(flow_df, out_file, file_format='csv', index=False)

            stats['backfilled'] += 1
            stats['total_rows'] += len(flow_df)

            if i % 50 == 0:
                logger.info(f"  [{i}/{total}] {ticker}: {len(flow_df)}행")

        except Exception as e:
            logger.warning(f"  {ticker}: 실패 — {e}")
            stats['skipped'] += 1

    logger.info(f"  ✅ Flow proxy 백필: {stats['backfilled']}종목, "
                f"총 {stats['total_rows']:,}행")
    return stats


# ═══════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='V6 보조 데이터 백필')
    parser.add_argument('--sentiment', action='store_true',
                        help='Sentiment price_proxy만 백필')
    parser.add_argument('--dart', action='store_true',
                        help='DART daily_signal만 백필')
    parser.add_argument('--flow', action='store_true',
                        help='Investor Flow proxy만 백필')
    parser.add_argument('--retrain', action='store_true',
                        help='백필 후 즉시 재학습')
    args = parser.parse_args()

    ohlcv_dir = _PROJECT_ROOT / 'data' / 'kr_markets'
    sentiment_dir = _PROJECT_ROOT / 'data' / 'sentiment'
    dart_dir = _PROJECT_ROOT / 'data' / 'dart'
    flow_dir = _PROJECT_ROOT / 'data' / 'investor_flow'

    do_all = not (args.sentiment or args.dart or args.flow)

    start = time.time()
    results = {}

    if do_all or args.sentiment:
        results['sentiment'] = backfill_sentiment(ohlcv_dir, sentiment_dir)

    if do_all or args.dart:
        results['dart'] = backfill_dart(dart_dir, ohlcv_dir)

    if do_all or args.flow:
        results['flow'] = backfill_flow(ohlcv_dir, flow_dir)

    elapsed = time.time() - start
    logger.info(f"\n═══ 백필 완료 ({elapsed:.1f}초) ═══")
    for key, val in results.items():
        logger.info(f"  {key}: {val}")

    # 재학습
    if args.retrain:
        logger.info("\n═══ V6 재학습 시작 ═══")
        import subprocess
        subprocess.run([
            sys.executable,
            str(_PROJECT_ROOT / 'scripts' / 'train_ensemble.py'),
            '--force',
        ], cwd=str(_PROJECT_ROOT))


if __name__ == '__main__':
    main()
