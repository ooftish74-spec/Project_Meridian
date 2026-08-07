"""
Feature Store — DuckDB 기반 피처 관리 시스템
=============================================

기능:
  - 매일 수집 피처를 DuckDB에 체계적 저장
  - 학습/서빙/백테스트 간 일관성 보장
  - 데이터 리크 방지 (point-in-time query)
  - 피처 버전 관리 + 메타데이터
  - 결측치 통계 + 피처 드리프트 감지

Usage:
    from src.data_collection.feature_store import FeatureStore

    fs = FeatureStore()
    fs.save_features('005930', features_dict, date='2026-03-02')
    df = fs.get_features('005930', start='2025-01-01', end='2026-03-01')
    stats = fs.get_stats()
"""

from src.utils.file_ops import atomic_write_parquet

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# DuckDB 사용 (미설치 시 SQLite fallback)
try:
    import duckdb
    DB_ENGINE = 'duckdb'
except ImportError as e:
    DB_ENGINE = 'sqlite'


class FeatureStore:
    """
    DuckDB/SQLite 기반 피처 스토어.

    테이블 구조:
      features:     (date, ticker, feature_name, value)
      metadata:     (feature_name, description, source, created_at, version)
      snapshots:    (snapshot_id, date, ticker, n_features, created_at)
      drift_log:    (date, feature_name, mean, std, drift_flag)
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            project_root = Path(__file__).parent.parent.parent
            db_dir = project_root / 'data' / 'feature_store'
            db_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(db_dir / 'features.db')

        self.db_path = db_path
        self.engine = DB_ENGINE
        self._init_db()
        logger.info(f"  📦 Feature Store 초기화: {self.engine} ({db_path})")

    def _get_conn(self):
        """연결 생성."""
        if self.engine == 'duckdb':
            return duckdb.connect(self.db_path)
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        """테이블 생성."""
        conn = self._get_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS features (
                    date       VARCHAR NOT NULL,
                    ticker     VARCHAR NOT NULL,
                    feature    VARCHAR NOT NULL,
                    value      DOUBLE,
                    PRIMARY KEY (date, ticker, feature)
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    feature     VARCHAR PRIMARY KEY,
                    description VARCHAR,
                    source      VARCHAR,
                    category    VARCHAR,
                    created_at  VARCHAR,
                    version     INTEGER DEFAULT 1
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id          INTEGER PRIMARY KEY,
                    date        VARCHAR NOT NULL,
                    ticker      VARCHAR NOT NULL,
                    n_features  INTEGER,
                    created_at  VARCHAR
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS drift_log (
                    date         VARCHAR,
                    feature      VARCHAR,
                    current_mean DOUBLE,
                    hist_mean    DOUBLE,
                    current_std  DOUBLE,
                    hist_std     DOUBLE,
                    drift_score  DOUBLE,
                    drift_flag   INTEGER DEFAULT 0,
                    PRIMARY KEY (date, feature)
                )
            """)

            # 인덱스 (SQLite)
            if self.engine == 'sqlite':
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_feat_date
                    ON features (date, ticker)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_feat_ticker
                    ON features (ticker, feature)
                """)

            conn.commit()
        finally:
            conn.close()

    # ──────────────────────────────────────────────
    # 저장
    # ──────────────────────────────────────────────

    def save_features(self, ticker: str, features: Dict[str, float],
                      date: str = None) -> int:
        """
        피처 딕셔너리를 DB에 저장.

        Args:
            ticker: 종목 코드 (예: '005930')
            features: {feature_name: value}
            date: 날짜 (기본: 오늘)

        Returns:
            저장된 피처 수
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')

        conn = self._get_conn()
        try:
            saved = 0
            for feat, val in features.items():
                if val is None or (isinstance(val, float) and np.isnan(val)):
                    val = None  # NaN → NULL
                elif isinstance(val, str):
                    # 문자열은 DOUBLE 컬럼에 저장 불가 → 스킵
                    continue
                elif not isinstance(val, (int, float, np.integer, np.floating)):
                    # 기타 비수치 타입 → 스킵
                    continue

                if self.engine == 'duckdb':
                    conn.execute("""
                        INSERT OR REPLACE INTO features (date, ticker, feature, value)
                        VALUES (?, ?, ?, ?)
                    """, [date, ticker, feat, val])
                else:
                    conn.execute("""
                        INSERT OR REPLACE INTO features (date, ticker, feature, value)
                        VALUES (?, ?, ?, ?)
                    """, (date, ticker, feat, val))
                saved += 1

            # 스냅샷 기록
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("""
                INSERT INTO snapshots (date, ticker, n_features, created_at)
                VALUES (?, ?, ?, ?)
            """, [date, ticker, saved, now] if self.engine == 'duckdb'
                else (date, ticker, saved, now))

            conn.commit()
            logger.info(f"    💾 {ticker} {date}: {saved}개 피처 저장")
            return saved

        finally:
            conn.close()

    def save_metadata(self, feature: str, description: str = '',
                      source: str = '', category: str = ''):
        """피처 메타데이터 등록."""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("""
                INSERT OR REPLACE INTO metadata
                (feature, description, source, category, created_at, version)
                VALUES (?, ?, ?, ?, ?, 1)
            """, [feature, description, source, category, now]
                if self.engine == 'duckdb'
                else (feature, description, source, category, now))
            conn.commit()
        finally:
            conn.close()

    def register_all_metadata(self, features: Dict[str, float],
                               source: str = 'enhanced_collector',
                               category: str = 'auto'):
        """피처 딕셔너리의 모든 키를 메타데이터에 등록."""
        conn = self._get_conn()
        try:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for feat in features.keys():
                conn.execute("""
                    INSERT OR IGNORE INTO metadata
                    (feature, description, source, category, created_at, version)
                    VALUES (?, ?, ?, ?, ?, 1)
                """, [feat, '', source, category, now]
                    if self.engine == 'duckdb'
                    else (feat, '', source, category, now))
            conn.commit()
        finally:
            conn.close()

    # ──────────────────────────────────────────────
    # 조회 (Point-in-Time 보장)
    # ──────────────────────────────────────────────

    def get_features(self, ticker: str, start: str = None,
                     end: str = None, features: List[str] = None) -> pd.DataFrame:
        """
        Point-in-time 피처 조회.
        데이터 리크 방지: end 날짜까지의 피처만 반환.

        Returns:
            DataFrame (index=date, columns=feature_names)
        """
        conn = self._get_conn()
        try:
            query = "SELECT date, feature, value FROM features WHERE ticker = ?"
            params = [ticker]

            if start:
                query += " AND date >= ?"
                params.append(start)
            if end:
                query += " AND date <= ?"
                params.append(end)
            if features:
                placeholders = ','.join(['?' for _ in features])
                query += f" AND feature IN ({placeholders})"
                params.extend(features)

            query += " ORDER BY date, feature"

            if self.engine == 'duckdb':
                df = conn.execute(query, params).fetchdf()
            else:
                df = pd.read_sql_query(query, conn, params=params)

            if df.empty:
                return pd.DataFrame()

            # 피벗: (date, feature) → 넓은 형태
            pivot = df.pivot_table(
                index='date', columns='feature', values='value',
                aggfunc='first'
            )
            pivot.index = pd.to_datetime(pivot.index)
            pivot.sort_index(inplace=True)

            return pivot

        finally:
            conn.close()

    def get_latest(self, ticker: str, n_days: int = 1) -> Dict[str, float]:
        """최근 N일 피처 (서빙용)."""
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=n_days + 5)).strftime('%Y-%m-%d')

        df = self.get_features(ticker, start, end)
        if df.empty:
            return {}

        # 최근 행
        latest = df.iloc[-1].to_dict()
        return {k: v for k, v in latest.items() if pd.notna(v)}

    def get_training_data(self, ticker: str, train_end: str,
                          features: List[str] = None) -> pd.DataFrame:
        """
        학습용 데이터 (리크 방지).
        train_end까지만 반환하여 미래 데이터 유입 차단.
        """
        return self.get_features(ticker, end=train_end, features=features)

    # ──────────────────────────────────────────────
    # 통계 & 모니터링
    # ──────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Feature Store 전체 통계."""
        conn = self._get_conn()
        try:
            stats = {}

            # 전체 행 수
            r = conn.execute("SELECT COUNT(*) FROM features").fetchone()
            stats['total_rows'] = r[0]

            # 종목 수
            r = conn.execute(
                "SELECT COUNT(DISTINCT ticker) FROM features").fetchone()
            stats['n_tickers'] = r[0]

            # 피처 수
            r = conn.execute(
                "SELECT COUNT(DISTINCT feature) FROM features").fetchone()
            stats['n_features'] = r[0]

            # 날짜 범위
            r = conn.execute(
                "SELECT MIN(date), MAX(date) FROM features").fetchone()
            stats['date_range'] = {'min': r[0], 'max': r[1]}

            # 스냅샷 수
            r = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
            stats['n_snapshots'] = r[0]

            # 결측 비율
            r = conn.execute(
                "SELECT COUNT(*) FROM features WHERE value IS NULL").fetchone()
            null_count = r[0]
            stats['null_rate'] = round(
                null_count / max(stats['total_rows'], 1) * 100, 2)

            # DB 크기
            db_size = os.path.getsize(self.db_path) / (1024 * 1024)
            stats['db_size_mb'] = round(db_size, 2)

            return stats

        finally:
            conn.close()

    def get_feature_coverage(self, ticker: str,
                              last_n_days: int = 30) -> pd.DataFrame:
        """피처별 결측률 (최근 N일)."""
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(days=last_n_days + 5)).strftime('%Y-%m-%d')

        df = self.get_features(ticker, start, end)
        if df.empty:
            return pd.DataFrame()

        coverage = pd.DataFrame({
            'non_null': df.notna().sum(),
            'total': len(df),
            'coverage_pct': (df.notna().sum() / len(df) * 100).round(1),
        }).sort_values('coverage_pct')

        return coverage

    def detect_drift(self, ticker: str, date: str = None,
                     window: int = 60, threshold: float = 2.0) -> List[Dict]:
        """
        피처 드리프트 감지.
        최근 값과 과거 window일 평균/표준편차 비교.
        """
        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')

        end = date
        start = (pd.to_datetime(date) - timedelta(days=window + 30)).strftime('%Y-%m-%d')

        df = self.get_features(ticker, start, end)
        if len(df) < 10:
            return []

        alerts = []
        latest = df.iloc[-1]
        historical = df.iloc[:-1]

        conn = self._get_conn()
        try:
            for col in df.columns:
                hist_vals = historical[col].dropna()
                if len(hist_vals) < 5:
                    continue

                current = latest[col]
                if pd.isna(current):
                    continue

                hist_mean = float(hist_vals.mean())
                hist_std = float(hist_vals.std())

                if hist_std < 1e-10:
                    continue

                drift_score = abs(current - hist_mean) / hist_std
                is_drift = drift_score > threshold

                if is_drift:
                    alerts.append({
                        'feature': col,
                        'current': round(float(current), 4),
                        'hist_mean': round(hist_mean, 4),
                        'hist_std': round(hist_std, 4),
                        'drift_score': round(drift_score, 2),
                    })

                # 로그 저장
                conn.execute("""
                    INSERT OR REPLACE INTO drift_log
                    (date, feature, current_mean, hist_mean, current_std,
                     hist_std, drift_score, drift_flag)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, [date, col, float(current), hist_mean,
                      float(hist_std), hist_std, drift_score,
                      1 if is_drift else 0]
                    if self.engine == 'duckdb'
                    else (date, col, float(current), hist_mean,
                          float(hist_std), hist_std, drift_score,
                          1 if is_drift else 0))

            conn.commit()
        finally:
            conn.close()

        if alerts:
            logger.warning(
                f"  ⚠️ 드리프트 감지 {len(alerts)}개: "
                f"{[a['feature'] for a in alerts[:3]]}")

        return sorted(alerts, key=lambda x: x['drift_score'], reverse=True)

    # ──────────────────────────────────────────────
    # 유틸리티
    # ──────────────────────────────────────────────

    def list_features(self) -> List[str]:
        """등록된 피처 목록."""
        conn = self._get_conn()
        try:
            r = conn.execute(
                "SELECT DISTINCT feature FROM features ORDER BY feature"
            ).fetchall()
            return [row[0] for row in r]
        finally:
            conn.close()

    def list_tickers(self) -> List[str]:
        """등록된 종목 목록."""
        conn = self._get_conn()
        try:
            r = conn.execute(
                "SELECT DISTINCT ticker FROM features ORDER BY ticker"
            ).fetchall()
            return [row[0] for row in r]
        finally:
            conn.close()

    def delete_old(self, days: int = 365 * 3) -> int:
        """오래된 데이터 삭제 기능 제거 (Append-only 원칙 적용)."""
        logger.info("  ⚠️ Data deletion is disabled. Following Append-only principle.")
        return 0

    def export_parquet(self, ticker: str, date: str = None) -> str:
        """피처를 날짜 파티션(YYYY-MM-DD) Parquet으로 내보내기 (Append-Only Time-travel)."""
        df = self.get_features(ticker)
        if df.empty:
            return ''

        if date is None:
            date = datetime.now().strftime('%Y-%m-%d')
            
        project_root = Path(__file__).parent.parent.parent
        base_dir = project_root / 'data' / 'feature_store'
        
        # 날짜별 파티션
        date_dir = base_dir / date
        date_dir.mkdir(parents=True, exist_ok=True)
        
        output_path = date_dir / f'{ticker}_features.parquet'
        atomic_write_parquet(df, str(output_path))
        
        # latest 심볼릭 링크 업데이트 (ml_stream.py 호환용)
        latest_dir = base_dir / 'latest'
        if latest_dir.exists() or latest_dir.is_symlink():
            try:
                latest_dir.unlink()
            except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as e:
                import logging
                logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                import shutil
                shutil.rmtree(latest_dir, ignore_errors=True)
        try:
            latest_dir.symlink_to(date_dir.resolve())
        except OSError:
            # 심볼릭 링크 실패 시(Windows 등) 강제 복사
            import shutil
            shutil.copytree(str(date_dir), str(latest_dir), dirs_exist_ok=True)
            
        logger.info(f"  📤 {ticker}: {len(df)}행 → {output_path} (Time-travel Partitioned)")
        return str(output_path)

    def __repr__(self):
        stats = self.get_stats()
        return (
            f"FeatureStore({self.engine}, "
            f"{stats['n_tickers']}종목, "
            f"{stats['n_features']}피처, "
            f"{stats['total_rows']}행, "
            f"{stats['db_size_mb']}MB)"
        )
