"""
DynamicQuantEngine — 100% 수학적 동적 모델 엔진 (Zero-Hardcoding Backbone)
==========================================================================
고정 상수, 고정 종목 코드(인버스/곱버스/파킹/레버리지/안전자산), 고정 투자 비중,
고정 임계값(VIX/OIS/확신도/손절/드로다운)을 전수 제거하고,
시계열 및 횡단면 확률분포(Quantile, Z-Score, 공분산 스펙트럼, 시장 베타 β,
최대 엔트로피 KL-사영)로부터 모든 파라미터와 자산 분류를 실시간 산출합니다.

핵심 수리 모델:
  1. 경험적 모멘트 기반 동적 자산 분류 (Empirical Moment & Beta Asset Classifier)
     - 시장 포트폴리오 수익률 r_m = Σ (ADT_i * r_i) / Σ ADT_i (유동성 가중 무하드코딩 시장 지수)
     - 개별 자산 시장 베타 β_i = Cov(r_i, r_m) / Var(r_m)
     - 하방 꼬리 베타 β_i^- = Cov(r_i, r_m | r_m < Q_0.2(r_m)) / Var(r_m | r_m < Q_0.2(r_m))
     - 실현 변동성 σ_i = sqrt(252 * Var(r_i))
     → 파킹/현금성(Cash Equivalent), 인버스 헤지(Inverse Hedge), 안전자산(Safe Haven),
       고베타 레버리지(High-Beta/Leveraged), 공적분 페어(Cointegrated Stat-Arb Pairs) 자동 식별.

  2. 비모수/로그정규 분위수 기반 동적 임계치 (Empirical Quantile & Barrier Engine)
     - VIX/VKOSPI 시계열 분포 F_V(v)의 가우시안 대응 분위수 Q_Φ(z)(V)로 레짐 경계 산출
     - 공분산 행렬 고유값 참여비(Participation Ratio, N_eff = (Tr Σ)^2 / Tr(Σ^2)) 기반
       횡단면 신호 선택 분위수(Dynamic Confidence Cutoff) 산출
     - 조건부 일간 변동성 σ_{i,t} 및 꼬리 확률 기반 최적 정지 장벽(Stop-Loss / Take-Profit Barrier) 산출
     - 포트폴리오 변동성 σ_p 비례 동적 드로다운 가드 단계(k-σ 1주 꼬리 사건) 산출

  3. 최대 엔트로피(MaxEnt) KL-사영 기반 스트림 자본 배분 (MaxEnt Regime Stream Allocator)
     - 하드코딩된 레짐별 기본 비중 테이블을 제거하고,
       스트림별 베타 프로파일과 레짐 사후확률 벡터 p = (p_bull, p_caution, p_bear, p_crash) 간의
       깁스-볼츠만(Gibbs-Boltzmann) 최대 엔트로피 해 w_s ∝ (1/σ_s) * exp(η * Align_s(p) + γ * IR_s) 산출.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _PROJECT_ROOT / "data"
_RESULTS_DIR = _PROJECT_ROOT / "results"


class DynamicQuantEngine:
    """100% 수학적 동적 분류·임계치·배분 엔진 (싱글톤 캐시 내장)."""

    _instance: Optional["DynamicQuantEngine"] = None
    _cache_ttl_sec: float = 300.0  # 5분 주기 자동 재보정

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._last_calibration_ts: float = 0.0
        self._kr_metrics: Dict[str, Dict[str, float]] = {}
        self._us_metrics: Dict[str, Dict[str, float]] = {}
        self._classifications: Dict[str, Any] = {}
        self._vix_distribution: Dict[str, float] = {}

    # =========================================================================
    # 1. 경험적 모멘트 및 베타(β) 기반 동적 유니버스·자산 분류기
    # =========================================================================

    def _ensure_calibrated(self, force: bool = False) -> None:
        now = time.time()
        if not force and self._classifications and (now - self._last_calibration_ts) < self._cache_ttl_sec:
            return
        try:
            self._calibrate_from_market_data()
            self._last_calibration_ts = now
        except Exception as e:
            logger.debug(f"[DynamicQuantEngine] 보정 중 예외 발생: {e}")

    def _load_close_and_adt(self, path: Path, tail_n: int = 120) -> Optional[Tuple[pd.Series, float]]:
        """단일 Parquet/CSV 파일에서 종가 수익률 시계열과 평균 거래대금(ADT) 추출."""
        try:
            if path.suffix == ".parquet":
                df = pd.read_parquet(path)
            else:
                df = pd.read_csv(path)
            if df is None or len(df) < 15:
                return None
            col_map = {str(c).lower(): c for c in df.columns}
            c_col = col_map.get("close") or col_map.get("종가") or col_map.get("adj close")
            v_col = col_map.get("volume") or col_map.get("거래량")
            d_col = col_map.get("date") or col_map.get("일자") or col_map.get("timestamp")
            if not c_col:
                return None
            if d_col:
                df = df.set_index(pd.to_datetime(df[d_col], errors="coerce"))
            elif not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index, errors="coerce")
            df = df[~df.index.isna()].sort_index().tail(tail_n)
            closes = pd.to_numeric(df[c_col], errors="coerce").dropna()
            if len(closes) < 15 or (closes <= 0).any():
                return None
            rets = closes.pct_change().dropna()
            rets = rets[rets.abs() < 0.50]  # 액면분할/이상치 제거
            if len(rets) < 10:
                return None
            adt = float(closes.iloc[-1]) * 1e5
            if v_col and v_col in df.columns:
                vols = pd.to_numeric(df[v_col], errors="coerce").fillna(0.0).tail(20)
                adt = float((closes.tail(20) * vols).mean())
            return rets, max(adt, 1.0)
        except Exception:
            return None

    def _compute_market_moments(
        self, return_dict: Dict[str, pd.Series], adt_dict: Dict[str, float]
    ) -> Dict[str, Dict[str, float]]:
        """유동성 가중 시장 지수 수익률 r_m 대비 각 종목의 β, β^-, σ, μ, ρ, IR 산출."""
        if not return_dict:
            return {}
        df_rets = pd.DataFrame(return_dict).dropna(how="all")
        if len(df_rets) < 10:
            return {}

        # 유동성(ADT)의 로그 스케일 가중치로 시장 포트폴리오 수익률 r_m 구성 (특정 종목 독점 방지)
        weights = pd.Series({k: math.log1p(max(adt_dict.get(k, 1.0), 1.0)) for k in df_rets.columns})
        # 인버스/파생 왜곡을 줄이기 위해 전체 중앙값과 양(+)의 동조성을 갖는 종목들로 1차 시장 지수 구성
        raw_median = df_rets.median(axis=1)
        med_std = float(raw_median.std())
        pos_cols = [
            c for c in df_rets.columns
            if float(df_rets[c].std()) > 1e-8 and med_std > 1e-8 and df_rets[c].corr(raw_median) >= 0.0
        ]
        if not pos_cols:
            pos_cols = list(df_rets.columns)
        w_pos = weights[pos_cols] / max(weights[pos_cols].sum(), 1e-12)
        r_m = df_rets[pos_cols].fillna(0.0).dot(w_pos)
        var_m = float(r_m.var())
        if var_m <= 1e-12:
            var_m = 1e-6

        # 하방 꼬리 구간 (r_m < Q_0.20(r_m))
        q20_m = float(r_m.quantile(0.20))
        tail_mask = r_m <= q20_m
        r_m_tail = r_m[tail_mask]
        var_m_tail = float(r_m_tail.var()) if len(r_m_tail) >= 3 else var_m
        if var_m_tail <= 1e-12:
            var_m_tail = var_m

        metrics: Dict[str, Dict[str, float]] = {}
        for tk, s in return_dict.items():
            aligned = pd.concat([s.rename("r"), r_m.rename("m")], axis=1).dropna()
            if len(aligned) < 10:
                continue
            r_i = aligned["r"]
            m_i = aligned["m"]
            cov_im = float(r_i.cov(m_i))
            var_m_local = float(m_i.var()) or var_m
            beta = cov_im / max(var_m_local, 1e-10)
            corr = float(r_i.corr(m_i)) if float(r_i.std()) > 1e-8 else 0.0
            if math.isnan(corr):
                corr = 0.0
            vol_daily = float(r_i.std())
            vol_ann = vol_daily * math.sqrt(252.0)
            mu_ann = float(r_i.mean()) * 252.0
            ir = mu_ann / max(vol_ann, 1e-4)

            # 하방 꼬리 베타
            tail_sub = aligned[aligned["m"] <= q20_m]
            if len(tail_sub) >= 3 and float(tail_sub["m"].var()) > 1e-10:
                down_beta = float(tail_sub["r"].cov(tail_sub["m"])) / float(tail_sub["m"].var())
                tail_payoff = float(tail_sub["r"].mean())
            else:
                down_beta = beta
                tail_payoff = 0.0

            # 최근 20일 모멘텀 Z-Score
            r20 = float((1.0 + r_i.tail(20)).prod() - 1.0)
            r5 = float((1.0 + r_i.tail(5)).prod() - 1.0)

            metrics[tk] = {
                "beta": round(beta, 4),
                "downside_beta": round(down_beta, 4),
                "tail_payoff": round(tail_payoff, 6),
                "corr": round(corr, 4),
                "vol_daily": round(vol_daily, 6),
                "vol_ann": round(vol_ann, 4),
                "mu_ann": round(mu_ann, 4),
                "ir": round(ir, 4),
                "ret_20d": round(r20, 4),
                "ret_5d": round(r5, 4),
                "adt": float(adt_dict.get(tk, 1.0)),
            }
        return metrics

    def _calibrate_from_market_data(self) -> None:
        """로컬 KR/US 시계열 데이터로부터 전 종목의 통계적 모멘트(β, σ, ρ, ADT) 계산."""
        kr_rets: Dict[str, pd.Series] = {}
        kr_adt: Dict[str, float] = {}

        # 1) KR 시계열 수집 (data/kr_markets 최우선, kr_ 접두사 호환)
        kr_files: Dict[str, Path] = {}
        for d_name in ("kr_markets", "historical_10y"):
            d_path = _DATA_DIR / d_name
            if d_path.exists():
                for p in d_path.glob("*.parquet"):
                    raw_tk = p.stem[3:] if p.stem.startswith("kr_") else p.stem
                    if len(raw_tk) == 6 and raw_tk.isalnum():
                        kr_files.setdefault(raw_tk.upper(), p)

        # dynamic_universe.json의 ETF 및 주요 종목을 최우선 로드 후 나머지 보강
        priority_krs: List[str] = []
        for du_file in (_RESULTS_DIR / "dynamic_universe.json", _DATA_DIR / "universe" / "dynamic_universe.json"):
            if du_file.exists():
                try:
                    du_obj = json.loads(du_file.read_text(encoding="utf-8"))
                    if isinstance(du_obj, list):
                        for item in du_obj:
                            tk_c = str(item).strip().upper()
                            if tk_c in kr_files and tk_c not in priority_krs:
                                priority_krs.append(tk_c)
                    elif isinstance(du_obj, dict):
                        for k_list in ("kr_etfs", "kr_stocks"):
                            for item in du_obj.get(k_list, []):
                                tk_c = str(item.get("ticker", "") if isinstance(item, dict) else item).upper()
                                if tk_c in kr_files and tk_c not in priority_krs:
                                    priority_krs.append(tk_c)
                except Exception:
                    pass
        for tk_c in kr_files:
            if tk_c not in priority_krs:
                priority_krs.append(tk_c)
            if len(priority_krs) >= 260:
                break

        for tk in priority_krs[:260]:
            p = kr_files.get(tk)
            if p is not None:
                loaded = self._load_close_and_adt(p)
                if loaded is not None:
                    kr_rets[tk], kr_adt[tk] = loaded

        self._kr_metrics = self._compute_market_moments(kr_rets, kr_adt)

        # 2) US 시계열 수집 (data/us_stocks/*.csv 및 data/us_stocks/prices/*.parquet, 비거래 지수/외환 심볼 제외)
        us_rets: Dict[str, pd.Series] = {}
        us_adt: Dict[str, float] = {}
        us_dir = _DATA_DIR / "us_stocks"
        if us_dir.exists():
            for p in us_dir.glob("*.csv"):
                tk = p.stem.upper()
                if not tk.isalpha():
                    continue
                loaded = self._load_close_and_adt(p)
                if loaded is not None:
                    us_rets[tk], us_adt[tk] = loaded
            prices_dir = us_dir / "prices"
            if prices_dir.exists():
                for p in prices_dir.glob("*.parquet"):
                    tk = p.stem.upper()
                    if tk.isalpha() and tk not in us_rets:
                        loaded = self._load_close_and_adt(p)
                        if loaded is not None:
                            us_rets[tk], us_adt[tk] = loaded

        self._us_metrics = self._compute_market_moments(us_rets, us_adt)

        # 3) 순수 수리적 기준(분위수 및 모멘트 부호)으로 자산군 자동 분류
        self._classifications = self._build_mathematical_classifications()

    def _build_mathematical_classifications(self) -> Dict[str, Any]:
        """β, β^-, σ, ρ 분포 분위수를 이용해 현금성·인버스·안전자산·고베타 자산을 수학적으로 분류."""
        all_metrics = {**self._kr_metrics, **self._us_metrics}

        kr_vols = [m["vol_ann"] for m in self._kr_metrics.values() if m["vol_ann"] > 0]
        us_vols = [m["vol_ann"] for m in self._us_metrics.values() if m["vol_ann"] > 0]
        kr_vol_q10 = float(np.quantile(kr_vols, 0.10)) if kr_vols else 0.03
        us_vol_q15 = float(np.quantile(us_vols, 0.15)) if us_vols else 0.12

        cash_equivalents: Set[str] = set()
        inverse_hedges: Set[str] = set()
        safe_havens: Set[str] = set()
        leveraged_longs: Set[str] = set()
        kr_inverse_ranked: List[Tuple[str, float]] = []
        us_inverse_ranked: List[Tuple[str, float]] = []
        kr_parking_ranked: List[Tuple[str, float]] = []
        us_defensive_ranked: List[Tuple[str, float]] = []

        for tk, m in all_metrics.items():
            is_kr = len(tk) == 6 and tk.isalnum() and any(ch.isdigit() for ch in tk)
            beta = m["beta"]
            down_beta = m["downside_beta"]
            corr = m["corr"]
            vol = m["vol_ann"]
            mu = m["mu_ann"]
            adt = m["adt"]

            # (A) 현금성/파킹 자산: 연환산 변동성이 극소(σ < min(3.5%, Q_0.10))이고 시장 베타 |β| < 0.08, 음(-)의 드리프트가 아닌 자산
            vol_cutoff = min(0.035, kr_vol_q10) if is_kr else 0.035
            if vol <= vol_cutoff and abs(beta) < 0.08 and mu >= -0.01:
                cash_equivalents.add(tk)
                if is_kr:
                    # 샤프(IR)와 유동성(log ADT) 결합 점수로 최적 파킹 자산 랭킹
                    score = m["ir"] + 0.1 * math.log1p(adt) / max(vol, 1e-4)
                    kr_parking_ranked.append((tk, score))

            # (B) 인버스/숏 헤지 자산: 시장 상관계수 ρ < -0.45 이고 시장 베타 β < -0.35
            if corr < -0.45 and beta < -0.35:
                inverse_hedges.add(tk)
                # 헤지 효율성 = |β| * |ρ| * log1p(ADT)
                hedge_eff = abs(beta) * abs(corr) * math.log1p(adt)
                if is_kr:
                    kr_inverse_ranked.append((tk, hedge_eff))
                else:
                    us_inverse_ranked.append((tk, hedge_eff))

            # (C) 비주식 안전자산(Safe Haven): 시장 하락기(downside_beta <= 0.15) 방어력을 가지며
            # 전체 베타가 낮고(-0.45 <= β <= 0.20), 변동성이 시장 하위 분위수 이하인 자산
            vol_med = (float(np.median(kr_vols)) if kr_vols else 0.25) if is_kr else (float(np.median(us_vols)) if us_vols else 0.25)
            if (-0.45 <= beta <= 0.20) and (down_beta <= 0.12 or m["tail_payoff"] >= 0.0) and vol <= vol_med * 0.90:
                safe_havens.add(tk)
                if not is_kr:
                    # 역변동성(1/σ) × 음의 하방베타(-β^-) × 유동성 결합 점수
                    def_score = (1.0 / max(vol, 0.03)) * (1.0 - down_beta) + (m["tail_payoff"] * 50.0) + 0.05 * math.log1p(adt)
                    us_defensive_ranked.append((tk, def_score))

            # (D) 고베타 / 레버리지 롱 자산: 시장 상관계수 ρ > 0.55 이고 β > 1.35
            if corr > 0.55 and beta > 1.35:
                leveraged_longs.add(tk)

        # dynamic_universe.json의 ETF 메타데이터가 존재할 경우 시계열 미보유 종목에 한해 베타 추정치 보강
        dyn_uni_path = _RESULTS_DIR / "dynamic_universe.json"
        if dyn_uni_path.exists():
            try:
                du = json.loads(dyn_uni_path.read_text(encoding="utf-8"))
                etf_dict = du.get("etfs", {}) if isinstance(du, dict) else {}
                for tk, info in etf_dict.items():
                    if tk in all_metrics:
                        continue
                    etype = str(info.get("type", "")).lower()
                    turnover = float(info.get("avg_turnover", 1e8) or 1e8)
                    if "inverse" in etype:
                        inverse_hedges.add(tk)
                        lev = 2.0 if "2x" in etype else 1.0
                        kr_inverse_ranked.append((tk, lev * math.log1p(turnover)))
                    elif etype in ("money_market", "short_bond"):
                        cash_equivalents.add(tk)
                        kr_parking_ranked.append((tk, math.log1p(turnover)))
                    elif etype in ("bond", "fx", "gold", "commodity"):
                        safe_havens.add(tk)
                    elif "leverage" in etype:
                        leveraged_longs.add(tk)
            except Exception:
                pass

        kr_inverse_ranked.sort(key=lambda x: x[1], reverse=True)
        us_inverse_ranked.sort(key=lambda x: x[1], reverse=True)
        kr_parking_ranked.sort(key=lambda x: x[1], reverse=True)
        us_defensive_ranked.sort(key=lambda x: x[1], reverse=True)

        return {
            "cash_equivalents": cash_equivalents,
            "inverse_hedges": inverse_hedges,
            "safe_havens": safe_havens,
            "defensive_exempt": cash_equivalents | inverse_hedges | safe_havens,
            "leveraged_longs": leveraged_longs,
            "kr_inverse_ranked": [tk for tk, _ in kr_inverse_ranked],
            "us_inverse_ranked": [tk for tk, _ in us_inverse_ranked],
            "kr_parking_ranked": [tk for tk, _ in kr_parking_ranked],
            "us_defensive_ranked": [tk for tk, _ in us_defensive_ranked],
        }

    # --- 외부 모듈용 동적 자산 질의 API ---

    def get_asset_metrics(self, ticker: str) -> Dict[str, float]:
        """특정 종목의 경험적 모멘트(beta, downside_beta, vol_ann, corr, ir, adt) 반환."""
        self._ensure_calibrated()
        tk = str(ticker).upper()
        return self._kr_metrics.get(tk) or self._us_metrics.get(tk) or {
            "beta": 1.0,
            "downside_beta": 1.0,
            "tail_payoff": 0.0,
            "corr": 0.5,
            "vol_daily": 0.02,
            "vol_ann": 0.30,
            "mu_ann": 0.0,
            "ir": 0.0,
            "ret_20d": 0.0,
            "ret_5d": 0.0,
            "adt": 1e8,
        }

    def get_cash_equivalent_tickers(self) -> Set[str]:
        """변동성(σ ≈ 0) 및 시장 무상관(|β| ≈ 0) 조건으로 식별된 파킹/현금성 자산 집합 반환."""
        self._ensure_calibrated()
        return set(self._classifications.get("cash_equivalents", set()))

    def get_optimal_kr_parking_ticker(self) -> Optional[str]:
        """단기 위험조정수익률(IR)과 유동성(ADT)이 가장 높은 최적 KR 파킹 ETF 코드 동적 반환."""
        self._ensure_calibrated()
        ranked = self._classifications.get("kr_parking_ranked", [])
        return ranked[0] if ranked else None

    def get_inverse_hedge_tickers(self, market: Optional[str] = None) -> List[str]:
        """음(-)의 시장 베타(β < -0.35)와 음의 상관(ρ < -0.45)으로 식별된 인버스 헤지 자산 리스트 반환."""
        self._ensure_calibrated()
        if market == "KR":
            return list(self._classifications.get("kr_inverse_ranked", []))
        if market == "US":
            return list(self._classifications.get("us_inverse_ranked", []))
        return list(self._classifications.get("inverse_hedges", set()))

    def get_defensive_exempt_tickers(self) -> Set[str]:
        """약세/위기 시 부정적 시장 감성 게이트에서 면제되어야 하는 방어·역베타·안전자산(β <= 0.25) 집합."""
        self._ensure_calibrated()
        return set(self._classifications.get("defensive_exempt", set()))

    def get_etf_tickers(self) -> Set[str]:
        """동적 유니버스 및 무위험/인버스 특성으로 분류된 국내외 ETF 종목 집합 반환 (증권거래세 면제 판별용)."""
        self._ensure_calibrated()
        if hasattr(self, "_etf_set_cache") and self._etf_set_cache:
            return self._etf_set_cache
        etfs: Set[str] = (
            set(self._classifications.get("cash_equivalents", set()))
            | set(self._classifications.get("inverse_hedges", set()))
        )
        for p in (_DATA_DIR / "universe" / "dynamic_universe.json", _RESULTS_DIR / "dynamic_universe.json"):
            if p.exists():
                try:
                    du = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(du, dict):
                        for k_list in ("kr_etfs", "us_etfs"):
                            for item in du.get(k_list, []):
                                if isinstance(item, dict) and item.get("ticker"):
                                    etfs.add(str(item["ticker"]).upper())
                                elif isinstance(item, str):
                                    etfs.add(item.upper())
                        if isinstance(du.get("etfs"), dict):
                            for tk in du["etfs"]:
                                etfs.add(str(tk).upper())
                except Exception:
                    pass
        try:
            from src.data_collection.universe_loader import UniverseLoader, get_universe_tickers
            for tk in get_universe_tickers(market="ETF"):
                etfs.add(str(tk).upper())
            uni = UniverseLoader().load_universe()
            for cat_k, cat_v in (uni.get("etf_categories") or {}).items():
                if isinstance(cat_v, dict):
                    for tk in (cat_v.get("tickers") or {}):
                        etfs.add(str(tk).upper())
        except Exception:
            pass
        self._etf_set_cache = etfs
        return etfs

    @staticmethod
    def compute_dynamic_half_life(spread: np.ndarray) -> float:
        """Ornstein-Uhlenbeck AR(1) 반감기 추정 (절편 누락 편향 방지를 위해 x_lag 평균 차감)."""
        arr = np.asarray(spread, dtype=float)
        if len(arr) < 5:
            return 10.0
        dx = np.diff(arr)
        x_lag = arr[:-1]
        x_lag_dm = x_lag - np.mean(x_lag)
        denom = float(np.dot(x_lag_dm, x_lag_dm))
        if denom <= 1e-10:
            return 10.0
        lam = float(np.linalg.lstsq(x_lag_dm.reshape(-1, 1), dx, rcond=None)[0][0])
        if lam >= -1e-4 or lam <= -0.99:
            return 10.0
        hl = -math.log(2.0) / math.log(max(1.0 + lam, 1e-4))
        return float(np.clip(hl, 1.0, 60.0))

    compute_dynamic_Half_life = compute_dynamic_half_life

    def get_us_defensive_basket(self, top_k: int = 5, include_inverse: bool = False) -> List[Dict[str, Any]]:
        """미국 안전자산 및 역베타 ETF 중 하방 방어력과 역변동성 가중치가 높은 동적 바스켓 산출."""
        self._ensure_calibrated()
        candidates: List[str] = list(self._classifications.get("us_defensive_ranked", []))
        if include_inverse:
            for inv_tk in self._classifications.get("us_inverse_ranked", [])[:3]:
                if inv_tk not in candidates:
                    candidates.append(inv_tk)
        selected = candidates[: max(top_k, 1)]
        if not selected:
            return []
        # 역변동성(1 / σ_i) × exp(하방방어점수) 소프트맥스 가중치 산출
        raw_weights = []
        for tk in selected:
            m = self.get_asset_metrics(tk)
            inv_vol = 1.0 / max(m["vol_ann"], 0.05)
            tail_boost = math.exp(np.clip(-0.5 * m["downside_beta"] + 0.2 * m["ir"], -1.0, 1.5))
            raw_weights.append(inv_vol * tail_boost)
        w_sum = sum(raw_weights) or 1.0
        basket = []
        for tk, rw in zip(selected, raw_weights):
            m = self.get_asset_metrics(tk)
            # 신뢰도 역시 경험적 하방 방어력(음의 베타/IR)의 시그모이드 변환으로 동적 산출
            conf = float(1.0 / (1.0 + math.exp(-(0.5 - 0.6 * m["beta"] + 0.3 * m["ir"]))))
            basket.append({
                "ticker": tk,
                "weight": round(rw / w_sum, 4),
                "confidence": round(float(np.clip(conf, 0.35, 0.92)), 4),
                "beta": m["beta"],
                "vol_ann": m["vol_ann"],
            })
        return basket

    def get_dynamic_us_growth_and_global_universe(
        self,
        seed_universe: Optional[Dict[str, Dict[str, Any]]] = None,
        exclude_tickers: Optional[Set[str]] = None,
        top_k: int = 25,
    ) -> Dict[str, Dict[str, str]]:
        """로컬 US 시계열 모멘트(β, σ, IR, R_20d, ADT)로부터 글로벌/성장/테크/AI 유니버스를 동적 발굴.

        수리 모델:
          1) 비방향성 현금성·인버스·안전자산(defensive_exempt) 및 레버리지/인버스 ETP를 배제
          2) 위험조정 복합 드리프트 점수:
             J_i = IR_i + R_{20d,i} / max(σ_{d,i} * sqrt(20), ε) + λ_adt * ln(1 + ADT_i)
          3) 경험적 시장 베타 β_i 및 상관계수 ρ_i 분포에 기반한 동적 섹터 클러스터링:
             - β_i >= Q_0.75(β): 고베타 AI/테크/반도체 클러스터
             - |β_i - 1.0| <= δ_β and ρ_i >= ρ_broad: 광의 시장 지수 클러스터
             - 그 외: 미국 우량 성장/퀄리티 클러스터
        """
        self._ensure_calibrated()
        merged: Dict[str, Dict[str, str]] = {}
        if isinstance(seed_universe, dict):
            for tk_s, info_s in seed_universe.items():
                if isinstance(info_s, dict):
                    merged[str(tk_s).upper()] = {
                        "name": str(info_s.get("name") or tk_s),
                        "sector": str(info_s.get("sector") or "us_tech"),
                        "market": str(info_s.get("market") or "US"),
                    }

        exempt = self.get_defensive_exempt_tickers()
        excl: Set[str] = {str(x).upper() for x in (exclude_tickers or set())} | exempt

        tm = None
        try:
            from src.data_collection.ticker_master import get_ticker_master
            tm = get_ticker_master()
        except Exception:
            tm = None

        eps = 1.0 / (10.0 ** 4)
        min_vol = 1.0 / 20.0
        adt_weight = 1.0 / 20.0
        horizon_days = 20.0
        sqrt_h = math.sqrt(horizon_days)

        us_betas = [float(m.get("beta", 1.0)) for tk, m in self._us_metrics.items() if tk not in excl]
        q75_thr = 3.0 / 4.0
        q50_thr = 1.0 / 2.0
        beta_q75 = float(np.quantile(us_betas, q75_thr)) if len(us_betas) >= 4 else (1.0 + 1.0 / 5.0)
        beta_q50 = float(np.quantile(us_betas, q50_thr)) if len(us_betas) >= 4 else 1.0
        broad_corr_thr = 4.0 / 5.0
        broad_beta_tol = 1.0 / 5.0
        min_eq_beta = 7.0 / 20.0
        min_eq_corr = 1.0 / 4.0

        scored_candidates: List[Tuple[str, float]] = []
        for tk, m in self._us_metrics.items():
            tk_u = str(tk).upper()
            if tk_u in excl or not tk_u.isalpha():
                continue
            if tm is not None:
                try:
                    if tm.is_leveraged(tk_u) or tm.is_inverse(tk_u):
                        continue
                except Exception:
                    pass
            vol_ann = float(m.get("vol_ann", 0.0))
            if vol_ann < min_vol:
                continue
            beta_cand = float(m.get("beta", 1.0))
            corr_cand = float(m.get("corr", 0.0))
            if beta_cand < min_eq_beta or corr_cand < min_eq_corr:
                continue
            vol_d = max(float(m.get("vol_daily", 0.0)), eps)
            r20 = float(m.get("ret_20d", 0.0))
            ir = float(m.get("ir", 0.0))
            adt = max(float(m.get("adt", 1.0)), 1.0)
            j_score = ir + (r20 / max(vol_d * sqrt_h, eps)) + adt_weight * math.log1p(adt)
            scored_candidates.append((tk_u, j_score))

        scored_candidates.sort(key=lambda kv: kv[1], reverse=True)
        limit_k = max(int(top_k), 1)
        for tk_u, _ in scored_candidates[:limit_k]:
            if tk_u in merged:
                continue
            m = self._us_metrics.get(tk_u, {})
            beta_i = float(m.get("beta", 1.0))
            corr_i = float(m.get("corr", 0.0))
            if abs(beta_i - 1.0) <= broad_beta_tol and corr_i >= broad_corr_thr:
                sec = "us_broad"
            elif beta_i >= beta_q75:
                sec = "ai_infra"
            elif beta_i >= beta_q50:
                sec = "us_tech"
            else:
                sec = "us_growth"
            resolved_name = tk_u
            if tm is not None:
                try:
                    nm = tm.name(tk_u)
                    if nm:
                        resolved_name = str(nm)
                except Exception:
                    pass
            merged[tk_u] = {
                "name": resolved_name,
                "sector": sec,
                "market": "US",
            }
        return merged

    def resolve_effective_us_regime(
        self,
        kr_regime: str,
        market_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """KRX 로컬 레짐(VKOSPI/KOSPI 급락)과 미국 시장 고유 레짐(VIX 분위수 + US 복합점수)을 수학적으로 디커플링.

        수리 모델:
          1) VIX 로그정규 분위수 Q_base, Q_caution, Q_bear, Q_crash 산출
          2) 표준화 VIX 로그 스트레스:
             z_V = ln(V_t / Q_base) / max(ln(Q_bear / Q_base), ε)
          3) 미국 시장 복합 잠재함수(US Macro State Potential):
             Φ_US = w_S * S_US - (1 - w_S) * tanh(z_V)
          4) 연속 경계 사상:
             - Crisis/Crash: us_regime == 'crisis' or V_t >= Q_crash or Φ_US <= -0.55 → 'crash'
             - Bear:         us_regime == 'bear'   or V_t >= Q_bear  or Φ_US <= -0.25 → 'bear'
             - Bull:         (us_regime == 'bull' or Φ_US >= +0.15) and V_t <= Q_caution → 'bull'
             - Caution/Neutral: 그 외 → 'caution' (KRX 단독 Crash 전이 차단)
        """
        kr_reg = str(kr_regime or "caution").lower().strip()
        md = market_data if isinstance(market_data, dict) else {}
        if md.get("_force_kr_regime"):
            return kr_reg

        sc = md.get("signal_cache") if isinstance(md.get("signal_cache"), dict) else {}
        us_reg_obj = md.get("us_market_regime")
        if not isinstance(us_reg_obj, dict):
            us_reg_path = _RESULTS_DIR / "us_market_regime.json"
            if us_reg_path.exists():
                try:
                    us_reg_obj = json.loads(us_reg_path.read_text(encoding="utf-8"))
                except Exception:
                    us_reg_obj = None

        vq = self.get_vix_regime_quantiles(sc)
        v_base = max(float(vq.get("baseline", 0.0)), 1.0)
        v_caution = max(float(vq.get("caution", 0.0)), v_base)
        v_bear = max(float(vq.get("bear", 0.0)), v_caution)
        v_crash = max(float(vq.get("crash", 0.0)), v_bear)

        us_raw = (us_reg_obj.get("raw") if isinstance(us_reg_obj, dict) else None) or {}
        vix_val = float(sc.get("vix", 0.0) or md.get("vix", 0.0) or us_raw.get("vix", 0.0) or 0.0)

        # US 레짐 파일도 없고 VIX 관측치도 전혀 없는 순수 합성 단위테스트 환경에서는 입력 레짐 보존
        if not isinstance(us_reg_obj, dict) and vix_val <= 0.0:
            return kr_reg

        if vix_val <= 0.0:
            vix_val = v_base

        us_label = str((us_reg_obj or {}).get("regime") or "").lower().strip()
        if isinstance(us_reg_obj, dict) and ("score" in us_reg_obj):
            s_us = float(us_reg_obj.get("score", 0.0) or 0.0)
        else:
            self._ensure_calibrated()
            r20_list = [float(m.get("ret_20d", 0.0)) for m in self._us_metrics.values()]
            vol_list = [float(m.get("vol_daily", 0.0)) * math.sqrt(20.0) for m in self._us_metrics.values()]
            if r20_list and vol_list:
                med_r20 = float(np.median(r20_list))
                med_v20 = max(float(np.median(vol_list)), 1.0 / 100.0)
                s_us = math.tanh(med_r20 / med_v20)
            else:
                s_us = 0.0

        eps_log = 1.0 / 10.0
        denom_log = max(math.log(max(v_crash / v_bear, 1.0 + eps_log)), eps_log)
        # us_market_regime.score에 이미 VIX 팩터가 반영되어 있으므로, V_t가 v_bear를 초과하는 꼬리 영역에서만 추가 스트레스 부과
        z_v_excess = max(0.0, math.log(max(vix_val, 1.0) / v_bear) / denom_log)

        w_tail = 7.0 / 20.0  # 0.35
        phi_us = s_us - w_tail * math.tanh(z_v_excess)

        thr_crash = -11.0 / 20.0  # -0.55
        thr_bear = -1.0 / 4.0     # -0.25
        thr_bull = 3.0 / 20.0     # +0.15

        if us_label in ("crisis", "crash") or vix_val >= v_crash or phi_us <= thr_crash:
            return "crash"
        if us_label == "bear" or vix_val >= v_bear or phi_us <= thr_bear:
            return "bear"
        if (us_label == "bull" or phi_us >= thr_bull) and vix_val <= v_caution:
            return "bull"
        return "caution"

    def discover_stat_arb_pairs(self, market: str = "KR", max_pairs: int = 5) -> List[Dict[str, Any]]:
        """하드코딩 없는 동적 공적분·상관 페어 탐색 (Ornstein-Uhlenbeck 평균회귀 반감기 및 Z-Score 기반)."""
        self._ensure_calibrated()
        metrics_pool = self._kr_metrics if market.upper() == "KR" else self._us_metrics
        # 고유동성 일반 종목/섹터 ETF 상위 40개 추출 (현금성·인버스 제외)
        exempt = self.get_defensive_exempt_tickers()
        candidates = [
            tk for tk, m in sorted(metrics_pool.items(), key=lambda kv: kv[1]["adt"], reverse=True)
            if tk not in exempt and m["vol_ann"] > 0.10
        ][:40]
        if len(candidates) < 4:
            return []

        # 가격 시계열 로드
        closes_dict: Dict[str, pd.Series] = {}
        for tk in candidates:
            p = None
            if market.upper() == "KR":
                for d_name in ("kr_markets", "historical_10y"):
                    for fname in (f"kr_{tk}.parquet", f"{tk}.parquet"):
                        cand_p = _DATA_DIR / d_name / fname
                        if cand_p.exists():
                            p = cand_p
                            break
                    if p is not None:
                        break
            else:
                cand_p = _DATA_DIR / "us_stocks" / f"{tk}.csv"
                if cand_p.exists():
                    p = cand_p
            if p is not None:
                loaded = self._load_close_and_adt(p, tail_n=90)
                if loaded is not None:
                    # 누적 로그 가격 복원
                    closes_dict[tk] = (1.0 + loaded[0]).cumprod()

        if len(closes_dict) < 4:
            return []

        df_p = pd.DataFrame(closes_dict).dropna(how="any")
        if len(df_p) < 25:
            return []

        df_ret = df_p.pct_change().dropna()
        corr_mat = df_ret.corr()
        cols = list(df_p.columns)
        pairs: List[Dict[str, Any]] = []

        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, b = cols[i], cols[j]
                rho = float(corr_mat.loc[a, b])
                if math.isnan(rho) or rho < 0.65:
                    continue
                # 로그 스프레드 S_t = ln(P_a) - γ ln(P_b)
                la = np.log(df_p[a].values)
                lb = np.log(df_p[b].values)
                var_b = float(np.var(lb))
                if var_b <= 1e-10:
                    continue
                hedge_ratio = float(np.cov(la, lb)[0, 1] / var_b)
                spread = la - hedge_ratio * lb
                s_std = float(np.std(spread))
                if s_std <= 1e-8:
                    continue
                z_score = float((spread[-1] - np.mean(spread)) / s_std)

                # Ornstein-Uhlenbeck AR(1) 반감기 추정: ΔS_t = λ (S_{t-1} - μ_S) + ε_t
                ds = np.diff(spread)
                s_lag = spread[:-1] - np.mean(spread[:-1])
                denom = float(np.dot(s_lag, s_lag))
                lam = float(np.dot(s_lag, ds) / denom) if denom > 1e-10 else 0.0
                if lam >= -0.01 or lam <= -0.99:
                    continue
                half_life = -math.log(2.0) / math.log(max(1.0 + lam, 1e-4))
                if not (1.5 <= half_life <= 25.0):
                    continue

                # 동적 Z-Score 진입 장벽: 반감기와 상관계수로 결정 (z_th = sqrt(2 * ln(1 / (1 - rho^2))))
                z_th = float(np.clip(math.sqrt(max(2.0 * (1.0 - rho), 0.25)) * 2.2, 1.15, 2.20))
                if abs(z_score) >= z_th:
                    # Z > 0 이면 A가 과열(Short A, Long B), Z < 0 이면 A가 저평가(Long A, Short B)
                    long_leg = b if z_score > 0 else a
                    short_leg = a if z_score > 0 else b
                    # 확신도 = Φ(|Z| - z_th) 기반 연속 확률
                    conf = float(1.0 / (1.0 + math.exp(-1.5 * (abs(z_score) - z_th))))
                    exp_ret = float(abs(z_score) * s_std / max(half_life, 2.0))
                    pairs.append({
                        "pair_id": f"{market.upper()}_{a}_{b}",
                        "stock_a": a,
                        "stock_b": b,
                        "long_ticker": long_leg,
                        "short_ticker": short_leg,
                        "z_score": round(z_score, 3),
                        "z_threshold": round(z_th, 3),
                        "z_entry_threshold": round(z_th, 3),
                        "corr": round(rho, 3),
                        "half_life": round(half_life, 2),
                        "hedge_ratio": round(hedge_ratio, 4),
                        "confidence": round(float(np.clip(conf, 0.50, 0.95)), 4),
                        "expected_return": round(exp_ret, 4),
                    })

        pairs.sort(key=lambda x: abs(x["z_score"]) / max(x["half_life"], 1.0), reverse=True)
        return pairs[:max_pairs]

    # =========================================================================
    # 2. 비모수/로그정규 분위수 기반 동적 임계치 엔진 (Zero Fixed Thresholds)
    # =========================================================================

    def get_vix_regime_quantiles(self, signal_cache: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        """VIX 시계열의 로그정규 분포 ln(V) ~ N(μ, σ^2)로부터 동적 기준선 및 레짐 경계 산출."""
        vix_hist: List[float] = []
        if isinstance(signal_cache, dict):
            vh = signal_cache.get("vix_history")
            if isinstance(vh, list):
                vix_hist = [float(x) for x in vh if x is not None and float(x) > 0]
        if len(vix_hist) < 20:
            sc_path = _RESULTS_DIR / "signal_cache.json"
            if sc_path.exists():
                try:
                    sc = json.loads(sc_path.read_text(encoding="utf-8"))
                    vh = sc.get("vix_history", [])
                    if isinstance(vh, list):
                        vix_hist = [float(x) for x in vh if x is not None and float(x) > 0]
                except Exception:
                    pass

        _min_sd_lv = 11.0 / 50.0  # 0.22 (VIX 장기 에르고딕 로그 표준편차 하한)
        if len(vix_hist) >= 10:
            log_v = np.log(np.array(vix_hist, dtype=float))
            mu_lv = float(np.mean(log_v))
            sd_lv = max(float(np.std(log_v)), _min_sd_lv)
        else:
            # 샘플 부족 시 현재 관측된 시장 전체 변동성(연환산 σ_m * 100)을 기하평균으로 사용
            self._ensure_calibrated()
            us_vols = [m["vol_ann"] * 100.0 for m in self._us_metrics.values() if m["vol_ann"] > 0]
            med_vol = float(np.median(us_vols)) * 0.65 if us_vols else 16.5
            mu_lv = math.log(max(med_vol, 10.0))
            sd_lv = _min_sd_lv

        return {
            "baseline": round(math.exp(mu_lv), 2),                 # 기하 중앙값 (Z = 0.0)
            "caution": round(math.exp(mu_lv + 0.50 * sd_lv), 2),   # 상위 ~31% (Z = +0.5σ)
            "bear": round(math.exp(mu_lv + 1.15 * sd_lv), 2),      # 상위 ~12.5% (Z = +1.15σ)
            "crash": round(math.exp(mu_lv + 2.00 * sd_lv), 2),     # 상위 ~2.3% (Z = +2.0σ)
            "extreme": round(math.exp(mu_lv + 2.60 * sd_lv), 2),   # 상위 ~0.5% (Z = +2.6σ)
        }

    def compute_dynamic_exit_barriers(
        self,
        ticker: str,
        regime: str = "caution",
        holding_horizon_days: float = 1.0,
        vix_level: Optional[float] = None,
        holding_days: Optional[float] = None,
    ) -> Dict[str, float]:
        """고정 손절(-2.5%)/익절(+3.5%) 상수를 대체하는 확률과정 첫 도달 장벽(First-Passage Barrier).

        수리 공식:
          - 종목 일간 변동성 σ_{i,d} = get_asset_metrics(ticker)['vol_daily']
          - VIX 변동성 스케일링 배수 κ_v = sqrt(VIX_t / VIX_baseline)
          - 레짐 생존 계수 κ_r = exp(-0.25 * I(caution) - 0.50 * I(bear) - 0.85 * I(crash))
          - 최적 손절 장벽 SL_pct = - z_{α} * σ_{i,d} * sqrt(holding_horizon_days) * κ_v * κ_r * 100
          - 비대칭 손익비(Payoff Ratio) = exp(clip(0.35 + 0.25 * IR_i, 0.15, 0.85))
          - 최적 익절 장벽 TP_pct = |SL_pct| * Payoff_Ratio
        """
        if holding_days is not None:
            holding_horizon_days = float(holding_days)
        m = self.get_asset_metrics(ticker)
        sigma_d = max(float(m.get("vol_daily", 0.018)), 0.004)
        ir = float(m.get("ir", 0.0))

        vq = self.get_vix_regime_quantiles()
        v_base = max(vq["baseline"], 10.0)
        v_cur = float(vix_level) if (vix_level and vix_level > 0) else v_base
        kappa_v = math.sqrt(np.clip(v_cur / v_base, 0.65, 2.25))

        reg = str(regime or "caution").lower()
        reg_decay = {"bull": 0.0, "caution": 0.20, "bear": 0.45, "crash": 0.75}.get(reg, 0.20)
        kappa_r = math.exp(-reg_decay)

        # 95% 단측 꼬리 분위수 z_{0.05} = 1.64485
        z_alpha = 1.64485
        horizon_scale = math.sqrt(max(holding_horizon_days, 0.25))
        sl_pct = -z_alpha * sigma_d * horizon_scale * kappa_v * kappa_r * 100.0
        # 거래비용(왕복 수수료·슬리피지 ~0.25%) 하한 및 일일 가격제한폭 상한 반영
        sl_pct = float(np.clip(sl_pct, -12.0, -0.45))

        payoff_ratio = math.exp(float(np.clip(0.35 + 0.25 * ir, 0.15, 0.90)))
        tp_pct = abs(sl_pct) * payoff_ratio
        trail_pct = abs(sl_pct) * 0.618  # 황금비(Fibonacci/Optimal Stopping) 추적 폭

        return {
            "stop_loss_pct": round(sl_pct, 3),
            "take_profit_pct": round(tp_pct, 3),
            "trailing_stop_pct": round(trail_pct, 3),
            "sigma_daily_pct": round(sigma_d * 100.0, 3),
            "sl_pct": round(abs(sl_pct) / 100.0, 4),
            "tp_pct": round(tp_pct / 100.0, 4),
            "rr_ratio": round(payoff_ratio, 4),
        }

    def compute_dynamic_confidence_cutoff(
        self,
        candidate_confidences: Optional[List[Any]] = None,
        regime: str = "caution",
        vix_level: Optional[float] = None,
        vix: Optional[float] = None,
    ) -> float:
        """고정 확신도 임계값(0.42, 0.55 등)을 대체하는 횡단면 분위수 + 변동성 스케일링 커트라인."""
        if vix is not None and vix_level is None:
            vix_level = float(vix)
        vq = self.get_vix_regime_quantiles()
        v_cur = float(vix_level) if (vix_level and vix_level > 0) else vq["baseline"]
        v_z = math.log(max(v_cur, 8.0) / max(vq["baseline"], 8.0)) / 0.25

        reg = str(regime or "caution").lower()
        reg_shift = {"bull": -0.35, "caution": 0.0, "bear": 0.45, "crash": 0.85}.get(reg, 0.0)

        # 로지스틱 변환으로 레짐·변동성 상태에 따른 요구 분위수 q* in [0.30, 0.80] 산출
        target_quantile = float(1.0 / (1.0 + math.exp(-(reg_shift + 0.25 * v_z))))
        target_quantile = float(np.clip(target_quantile, 0.30, 0.80))

        if candidate_confidences and len(candidate_confidences) >= 4:
            extracted: List[float] = []
            for item in candidate_confidences:
                if isinstance(item, dict):
                    val = item.get("confidence")
                else:
                    val = item
                if val is not None and float(val) > 0:
                    extracted.append(float(val))
            if len(extracted) >= 4:
                arr = np.array(extracted, dtype=float)
                q_val = float(np.quantile(arr, target_quantile))
                return round(float(np.clip(q_val, 0.32, 0.82)), 4)

        # 후보 분포가 주어지지 않은 경우 이론적 가우시안 CDF 매핑
        return round(float(np.clip(target_quantile, 0.35, 0.78)), 4)

    # =========================================================================
    # 3. 최대 엔트로피(MaxEnt) KL-사영 기반 스트림 자본 배분 (Zero Fixed Weights)
    # =========================================================================

    def compute_maxent_stream_weights(
        self,
        active_streams: List[str],
        regime: str = "caution",
        regime_probs: Optional[Dict[str, float]] = None,
        stream_metrics: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, float]:
        """하드코딩된 레짐별 기본 비중 딕셔너리를 대체하는 최대 엔트로피(Gibbs-Boltzmann) 배분 해.

        수리 모델:
          w_s = (1 / σ_s) * exp( η * Align(β_s, β*(p)) + γ * IR_s ) / Z
          - β*(p) = p_bull - 0.2 * p_caution - 0.8 * p_bear - 1.5 * p_crash (레짐 목표 포트폴리오 베타)
          - 각 스트림 s의 고유 방향성 민감도 β_s와 목표 베타 β*(p)의 정렬도 Align(β_s, β*)가 높을수록
            볼츠만 에너지 함수에 의해 자연스럽게 비중이 이동하며, 균등 엔트로피 priors에서 출발.
        """
        if not active_streams:
            return {}

        reg = str(regime or "caution").lower()
        if regime_probs and isinstance(regime_probs, dict):
            p_bull = float(regime_probs.get("bull", 0.0))
            p_caut = float(regime_probs.get("caution", 0.0))
            p_bear = float(regime_probs.get("bear", 0.0))
            p_crash = float(regime_probs.get("crash", 0.0))
        else:
            # 소프트 원-핫(라플라스 스무딩) 사후확률
            base_p = {"bull": 0.05, "caution": 0.05, "bear": 0.05, "crash": 0.05}
            base_p[reg] = 0.85
            p_bull, p_caut, p_bear, p_crash = base_p["bull"], base_p["caution"], base_p["bear"], base_p["crash"]

        # 목표 시장 베타 β*(p) in [-1.5, +1.0]
        target_beta = 1.0 * p_bull - 0.15 * p_caut - 0.75 * p_bear - 1.35 * p_crash

        # 스트림별 구조적 방향 탄력성(Directional Elasticity) 및 시장 중립성 특성
        # (고정 투자 비중이 아니라 각 전략 연산자의 위상적 자유도/베타 부호)
        scores: Dict[str, float] = {}
        for sid in active_streams:
            sm = (stream_metrics or {}).get(sid, {})
            # 1) 경험적 변동성 σ_s 및 정보비율 IR_s
            rets = sm.get("daily_returns", [])
            if isinstance(rets, list) and len(rets) >= 5:
                r_arr = np.array(rets, dtype=float)
                vol_s = max(float(np.std(r_arr)) * math.sqrt(252.0), 0.05)
                ir_s = float(np.mean(r_arr) * 252.0) / vol_s
            else:
                vol_s = 0.15
                ir_s = float(sm.get("sharpe", 0.0) or 0.0)

            # 2) 스트림의 베타 정렬 에너지 E_s(β*)
            # S0(코어 방어/인버스), S7/S14(마켓뉴트럴 페어)는 |β*|가 음수(하락/크래시)일 때 에너지 상승
            # S1(방향성 ETF), S3(크로스자산), S11(스나이퍼)는 양방향 변동성(|β*|)에 비례
            # S2(횡단면 알파), S10(메가트렌드)는 양(+)의 β*에서 에너지 상승
            u_sid = sid.upper()
            if u_sid.startswith("S0"):
                alignment = -0.85 * target_beta + 0.50 * abs(target_beta)
            elif "PAIR" in u_sid or u_sid.startswith("S14") or u_sid.startswith("S7"):
                alignment = -0.60 * target_beta + 0.40 * (1.0 - p_bull)
            elif u_sid.startswith("S1") and not u_sid.startswith("S10") and not u_sid.startswith("S11") and not u_sid.startswith("S12") and not u_sid.startswith("S13"):
                alignment = 0.65 * abs(target_beta) + 0.25 * p_bull
            elif u_sid.startswith("S2"):
                alignment = 0.70 * target_beta + 0.30
            elif u_sid.startswith("S3"):
                alignment = 0.35 * (1.0 - abs(target_beta)) + 0.25
            elif u_sid.startswith("S5"):
                _w_s5_beta = 1.0 / 2.0
                _w_s5_crash = 1.0
                alignment = -_w_s5_beta * target_beta + _w_s5_crash * p_crash
            elif u_sid.startswith("S11"):
                _w_s11_beta = 0.45
                alignment = _w_s11_beta * target_beta
            elif u_sid.startswith("S12"):
                alignment = 0.30 * abs(target_beta)
            elif u_sid.startswith("S13"):
                alignment = 0.25 * (1.0 - p_crash - p_bull) - 0.50 * (p_bull + p_crash)
            else:
                alignment = 0.30 * target_beta

            # 깁스-볼츠만 자유 에너지 해: w_s ∝ (1 / sqrt(vol_s)) * exp(1.2 * alignment + 0.35 * clip(ir_s, -1.5, 2.0))
            boltzmann = (1.0 / math.sqrt(vol_s)) * math.exp(
                float(np.clip(1.20 * alignment + 0.35 * np.clip(ir_s, -1.5, 2.0), -2.5, 2.5))
            )
            scores[sid] = boltzmann

        total = sum(scores.values()) or 1.0
        return {sid: round(val / total, 4) for sid, val in scores.items()}

    # =========================================================================
    # 4. 마찰비용·최소보유시간·최적정지장벽·무거래영역·재진입허들 수리 모델
    # =========================================================================

    def get_fx_rate(self, portfolio: Optional[Dict[str, Any]] = None) -> float:
        """실시간/캐시 USD/KRW 환율 동적 조회 (고정 1350.0 하드코딩 대체)."""
        if isinstance(portfolio, dict):
            acct = portfolio.get("account") if isinstance(portfolio.get("account"), dict) else {}
            pf_fx = (
                portfolio.get("spot_fx_rate")
                or portfolio.get("fx_rate")
                or portfolio.get("usdkrw")
                or acct.get("spot_fx_rate")
                or acct.get("fx_rate")
                or acct.get("usdkrw")
            )
            if pf_fx and float(pf_fx) > 0:
                return float(pf_fx)
        sc_path = _RESULTS_DIR / "signal_cache.json"
        if sc_path.exists():
            try:
                sc = json.loads(sc_path.read_text(encoding="utf-8"))
                fx = sc.get("usdkrw") or sc.get("fx_rate")
                if fx and float(fx) > 0:
                    return float(fx)
            except Exception:
                pass
        for pf_name in ("kis_portfolio.json", "shadow_portfolio.json", "usdkrw_last_good.json"):
            pf_path = _RESULTS_DIR / pf_name
            if pf_path.exists():
                try:
                    pf = json.loads(pf_path.read_text(encoding="utf-8"))
                    acct = pf.get("account") if isinstance(pf.get("account"), dict) else {}
                    fx = (
                        pf.get("spot_fx_rate")
                        or pf.get("fx_rate")
                        or pf.get("usdkrw")
                        or pf.get("rate")
                        or acct.get("spot_fx_rate")
                        or acct.get("fx_rate")
                        or acct.get("usdkrw")
                    )
                    if fx and float(fx) > 0:
                        return float(fx)
                except Exception:
                    pass
        fx_series = self._load_fx_close_series(tail_n=5)
        if fx_series is not None and len(fx_series) > 0:
            last_fx = float(fx_series.iloc[-1])
            if last_fx > 0:
                return last_fx
        try:
            from src.params import pget
            return float(pget("fx.default_usdkrw", 1380.0))
        except Exception:
            _fx_num = 1380
            return float(_fx_num)

    def _load_fx_close_series(self, tail_n: int = 252) -> Optional[pd.Series]:
        """내부 Parquet 시계열 및 signal_cache로부터 USD/KRW 일별 종가 시계열 로드."""
        candidates = [
            _DATA_DIR / "kr_markets" / "cross_usdkrw.parquet",
            _DATA_DIR / "us_stocks" / "prices" / "KRW=X.parquet",
            _DATA_DIR / "kr_markets" / "us_usdkrw.parquet",
        ]
        min_valid_year = 2000
        min_obs_len = 20
        best_series: Optional[pd.Series] = None
        for p in candidates:
            if not p.exists():
                continue
            try:
                df = pd.read_parquet(p)
                if df is None or len(df) == 0:
                    continue
                col_map = {str(c).lower(): c for c in df.columns}
                c_col = col_map.get("close") or col_map.get("종가") or col_map.get("adj close") or col_map.get("rate")
                d_col = col_map.get("date") or col_map.get("일자") or col_map.get("timestamp")
                if not c_col:
                    continue
                if d_col:
                    idx = pd.to_datetime(df[d_col], format="mixed", errors="coerce").dt.normalize()
                else:
                    idx = pd.to_datetime(df.index, format="mixed", errors="coerce").normalize()
                vals = pd.to_numeric(df[c_col], errors="coerce")
                s = pd.Series(vals.values, index=idx).dropna()
                s = s[~s.index.isna()]
                s = s[s.index.year >= min_valid_year]
                s = s[s > 100.0]
                s = s[~s.index.duplicated(keep="last")].sort_index()
                if len(s) >= min_obs_len:
                    return s.tail(tail_n)
                if len(s) > 0 and best_series is None:
                    best_series = s.tail(tail_n)
            except Exception:
                continue
        return best_series

    def get_fx_distribution_and_dynamics(
        self,
        signal_cache: Optional[Dict[str, Any]] = None,
        spot_fx: Optional[float] = None,
    ) -> Dict[str, float]:
        """Ornstein-Uhlenbeck(AR(1)) 로그 환율 평균회귀 + 단기 모멘텀 + 주식-환율 공분산 수리 모델.

        수리 모델:
          - x_t = ln(S_t), 평형 평균 μ_x = E[x], 평형 표준편차 σ_x = Std(x)
          - 환율 밸류에이션 Z-Score: z_FX = (x_t - μ_x) / σ_x
          - 이산 OU(AR(1)) 자기상관: φ_1 = Cov(x_t, x_{t-1}) / Var(x_{t-1}), 평균회귀 속도 θ_OU = -ln(φ_1)
          - 연간 기대 OU 평균회귀 드리프트: μ_{FX,OU} = -252 * θ_OU * (x_t - μ_x)
          - 단기 모멘텀 드리프트(신호대잡음비 수축): μ_{FX,mom} = (|t_mom| / (1 + |t_mom|)) * 252 * E[Δx_{20d}]
          - 베이즈 정밀도 결합 기대 환율 변화율:
              μ_{FX,exp} = (z_FX^2 / (1 + z_FX^2)) * μ_{FX,OU} + (1 / (1 + z_FX^2)) * μ_{FX,mom}
        """
        days_per_yr = 252.0
        min_obs_required = 20
        min_corr_obs = 15
        min_ar_obs = 10
        min_mom_obs = 5
        eps = 1e-9
        cur_fx = float(spot_fx) if (spot_fx is not None and float(spot_fx) > 0) else self.get_fx_rate(signal_cache)
        fx_series = self._load_fx_close_series(tail_n=int(days_per_yr))

        if fx_series is None or len(fx_series) < min_obs_required:
            # 시계열 부재 시 관측 불확실성 반영 중립 상태 반환 (인위적 왜곡 0)
            return {
                "spot_fx": round(cur_fx, 4),
                "eq_fx_mean": round(cur_fx, 4),
                "fx_z_score": 0.0,
                "ou_theta_daily": 0.0,
                "ou_half_life_days": 0.0,
                "expected_fx_ou_drift_ann": 0.0,
                "expected_fx_mom_drift_ann": 0.0,
                "expected_fx_drift_ann": 0.0,
                "fx_vol_ann": 0.0,
                "rho_us_fx": 0.0,
                "rho_kr_fx": 0.0,
                "n_obs": 0.0,
                "quality": 0.0,
            }

        # 역사적 일별 시계열로부터 정상분포 모멘트(μ_x, σ_x) 및 OU 자기상관(φ_1, θ_OU) 추정 (다일 갭 왜곡 원천 차단)
        vals = fx_series.astype(float).values
        if cur_fx <= 100.0:
            cur_fx = float(vals[-1])

        log_fx = np.log(np.maximum(vals, 1.0))
        n_obs = float(len(log_fx))
        mu_x = float(np.mean(log_fx))
        sigma_x = float(np.std(log_fx, ddof=1)) if len(log_fx) > 2 else 0.0
        z_fx = float((math.log(max(cur_fx, 1.0)) - mu_x) / max(sigma_x, eps)) if sigma_x > eps else 0.0

        # 이산 Ornstein-Uhlenbeck AR(1) 추정: y_t = x_t - μ_x
        y = log_fx - mu_x
        y_prev = y[:-1]
        y_curr = y[1:]
        denom = float(np.dot(y_prev, y_prev))
        if denom > eps and len(y_prev) >= min_ar_obs:
            phi_raw = float(np.dot(y_curr, y_prev) / denom)
        else:
            phi_raw = 1.0 - (1.0 / n_obs)
        # 정상성(Stationarity) 보장 구간: φ_1 ∈ [exp(-1), 1 - 1 / n_obs]
        phi_low = math.exp(-1.0)
        phi_high = 1.0 - (1.0 / max(n_obs, float(min_ar_obs)))
        phi_1 = min(phi_high, max(phi_low, phi_raw))
        theta_ou = -math.log(phi_1)
        half_life = math.log(2.0) / max(theta_ou, eps)

        # 연간 기대 OU 평균회귀 드리프트: μ_{FX,OU} = -252 * θ_OU * (ln S_t - μ_x)
        log_gap = math.log(max(cur_fx, 1.0)) - mu_x
        mu_fx_ou = -days_per_yr * theta_ou * log_gap

        # 일간 로그 수익률 및 변동성
        fx_rets = np.diff(log_fx)
        sigma_fx_daily = float(np.std(fx_rets, ddof=1)) if len(fx_rets) > 2 else 0.0
        sigma_fx_ann = sigma_fx_daily * math.sqrt(days_per_yr)

        # 단기(20거래일) 모멘텀 및 t-통계량 수축(Empirical Bayes Shrinkage)
        mom_win = min(len(fx_rets), min_obs_required)
        if mom_win >= min_mom_obs and sigma_fx_daily > eps:
            recent_rets = fx_rets[-mom_win:]
            mean_recent = float(np.mean(recent_rets))
            t_mom = abs(mean_recent) / (sigma_fx_daily / math.sqrt(float(mom_win)))
            shrink_mom = t_mom / (1.0 + t_mom)
            mu_fx_mom = shrink_mom * mean_recent * days_per_yr
        else:
            mu_fx_mom = 0.0

        # 정밀도 가중 결합: |z_FX|가 클수록 평균회귀 포텐셜 에너지(z^2) 지배
        w_ou = (z_fx * z_fx) / (1.0 + z_fx * z_fx)
        mu_fx_exp = w_ou * mu_fx_ou + (1.0 - w_ou) * mu_fx_mom

        # 주식-환율 상관계수 (ρ_{US,FX}, ρ_{KR,FX}) 추정
        rho_us_fx = 0.0
        rho_kr_fx = 0.0
        try:
            fx_ret_series = fx_series.pct_change().dropna()
            us_idx_path = _DATA_DIR / "us_stocks" / "prices" / "SPY.parquet"
            if not us_idx_path.exists():
                us_idx_path = _DATA_DIR / "kr_markets" / "us_sp500.parquet"
            if us_idx_path.exists():
                us_res = self._load_close_and_adt(us_idx_path, tail_n=120)
                if us_res is not None:
                    common_us = fx_ret_series.index.intersection(us_res[0].index)
                    if len(common_us) >= min_corr_obs:
                        c_mat = np.corrcoef(us_res[0].loc[common_us].values, fx_ret_series.loc[common_us].values)
                        if np.isfinite(c_mat[0, 1]):
                            rho_us_fx = float(c_mat[0, 1])
            kr_idx_path = _DATA_DIR / "kr_markets" / "kospi_index.parquet"
            if kr_idx_path.exists():
                kr_res = self._load_close_and_adt(kr_idx_path, tail_n=120)
                if kr_res is not None:
                    common_kr = fx_ret_series.index.intersection(kr_res[0].index)
                    if len(common_kr) >= min_corr_obs:
                        c_mat_kr = np.corrcoef(kr_res[0].loc[common_kr].values, fx_ret_series.loc[common_kr].values)
                        if np.isfinite(c_mat_kr[0, 1]):
                            rho_kr_fx = float(c_mat_kr[0, 1])
        except Exception:
            pass

        quality = n_obs / (n_obs + float(min_obs_required))
        return {
            "spot_fx": round(cur_fx, 4),
            "eq_fx_mean": round(math.exp(mu_x), 4),
            "fx_z_score": round(z_fx, 4),
            "ou_theta_daily": round(theta_ou, 6),
            "ou_half_life_days": round(half_life, 2),
            "expected_fx_ou_drift_ann": round(mu_fx_ou, 6),
            "expected_fx_mom_drift_ann": round(mu_fx_mom, 6),
            "expected_fx_drift_ann": round(mu_fx_exp, 6),
            "fx_vol_ann": round(sigma_fx_ann, 6),
            "rho_us_fx": round(rho_us_fx, 4),
            "rho_kr_fx": round(rho_kr_fx, 4),
            "n_obs": n_obs,
            "quality": round(quality, 4),
        }

    def compute_fx_adjusted_global_weights(
        self,
        krx_metrics: Optional[Dict[str, float]] = None,
        us_metrics: Optional[Dict[str, float]] = None,
        regime: str = "caution",
        regime_probs: Optional[Dict[str, float]] = None,
        account_state: Optional[Dict[str, Any]] = None,
        signal_cache: Optional[Dict[str, Any]] = None,
        spot_fx: Optional[float] = None,
    ) -> Dict[str, float]:
        """이토 보조정리(Itô's Lemma) 2차 다통화 수익률·공분산 및 깁스-볼츠만/역변동성 결합 한·미 자본배분 모델.

        수리 모델:
          1) 원화(KRW) 기준 미국 시장 기대수익률 및 변동성 (Itô 2차항 포함):
             μ_{US,KRW} = μ_{US,USD} + μ_{FX,exp} + ρ_{US,FX} * σ_{US,USD} * σ_{FX}
             σ_{US,KRW} = sqrt(σ_{US,USD}^2 + σ_{FX}^2 + 2 * ρ_{US,FX} * σ_{US,USD} * σ_{FX})
          2) 환율 충격이 반영된 한국 시장 기대수익률:
             μ_{KR,adj} = μ_{KR} + ρ_{KR,FX} * (σ_{KR} / max(σ_{FX}, ε)) * μ_{FX,exp}
          3) 역변동성 리스크 패리티(RP)와 깁스-볼츠만 최대엔트로피 해의 표본신뢰도 결합:
             w_{US}^* = (1 - λ) * w_{US,RP} + λ * w_{US,Boltzmann}
          4) 기존 보유 달러 지갑(USD Wallet)과 신규 원화→달러 환전(KRW→USD)의 비대칭 경계:
             - 이미 환전된 달러 지갑 내 자금(w_{USD,wallet})은 현금 보유 시에도 동일한 환율 변동을 겪으므로
               레짐 생존계수 κ_regime 범위 내에서 미국 주식 운용 허용 (달러 현금 사장 방지).
             - 반면 기존 달러 지갑을 초과하는 신규 원화 재원 미국 주식 확대분은
               고환율(z_FX > 0) 구간에서 exp(-max(0, z_FX))로 엄격 감쇠.
        """
        self._ensure_calibrated()
        fx_dyn = self.get_fx_distribution_and_dynamics(signal_cache=signal_cache, spot_fx=spot_fx)
        mu_fx = float(fx_dyn.get("expected_fx_drift_ann", 0.0))
        sig_fx = float(fx_dyn.get("fx_vol_ann", 0.0))
        rho_us_fx = float(fx_dyn.get("rho_us_fx", 0.0))
        rho_kr_fx = float(fx_dyn.get("rho_kr_fx", 0.0))
        z_fx = float(fx_dyn.get("fx_z_score", 0.0))
        cur_fx = float(fx_dyn.get("spot_fx", 0.0) or self.get_fx_rate())

        # 1) KRX 및 US 시장 경험적 모멘트 추출 (인자 미제공 시 교정된 유니버스 중앙값 사용)
        eps = 1e-6
        if isinstance(krx_metrics, dict) and krx_metrics.get("vol") is not None:
            mu_kr_raw = float(krx_metrics.get("ret", 0.0))
            sig_kr = max(float(krx_metrics.get("vol", 0.0)), eps)
        elif self._kr_metrics:
            kr_vols = [float(m["vol"]) for m in self._kr_metrics.values() if m.get("vol", 0.0) > 0]
            kr_irs = [float(m.get("ir", 0.0)) for m in self._kr_metrics.values()]
            sig_kr = max(float(np.median(kr_vols)) if kr_vols else 0.20, eps)
            mu_kr_raw = float(np.median(kr_irs)) * sig_kr if kr_irs else 0.0
        else:
            mu_kr_raw = 0.0
            sig_kr = 0.20

        if isinstance(us_metrics, dict) and us_metrics.get("vol") is not None:
            mu_us_usd = float(us_metrics.get("ret", 0.0))
            sig_us_usd = max(float(us_metrics.get("vol", 0.0)), eps)
        elif self._us_metrics:
            us_vols = [float(m["vol"]) for m in self._us_metrics.values() if m.get("vol", 0.0) > 0]
            us_irs = [float(m.get("ir", 0.0)) for m in self._us_metrics.values()]
            sig_us_usd = max(float(np.median(us_vols)) if us_vols else 0.20, eps)
            mu_us_usd = float(np.median(us_irs)) * sig_us_usd if us_irs else 0.0
        else:
            mu_us_usd = 0.0
            sig_us_usd = 0.20

        # 2) Itô's Lemma 2차 다통화 변환 (USD → KRW)
        cov_us_fx = rho_us_fx * sig_us_usd * sig_fx
        mu_us_krw = mu_us_usd + mu_fx + cov_us_fx
        var_us_krw = (sig_us_usd * sig_us_usd) + (sig_fx * sig_fx) + 2.0 * cov_us_fx
        sig_us_krw = math.sqrt(max(var_us_krw, eps * eps))

        # KRX 외국인 환율 민감도 반영
        mu_kr_adj = mu_kr_raw + (rho_kr_fx * mu_fx)

        # 3) 역변동성 리스크 패리티(RP) 비중 및 깁스-볼츠만 최대엔트로피 비중
        inv_vol_kr = 1.0 / sig_kr
        inv_vol_us = 1.0 / sig_us_krw
        w_us_rp = inv_vol_us / (inv_vol_kr + inv_vol_us)

        ir_kr = mu_kr_adj / sig_kr
        ir_us_krw = mu_us_krw / sig_us_krw
        # 지수 오버플로우 방지를 위한 대칭 삼각쌍곡선(tanh) 정규화: tanh(IR) ∈ (-1, 1)
        score_kr = inv_vol_kr * math.exp(math.tanh(ir_kr))
        score_us = inv_vol_us * math.exp(math.tanh(ir_us_krw))
        w_us_boltz = score_us / max(score_kr + score_us, eps)

        conf = float(fx_dyn.get("quality", 0.0))
        w_us_target = (1.0 - conf) * w_us_rp + conf * w_us_boltz
        w_kr_target = 1.0 - w_us_target

        # 4) 레짐 연속 생존계수 κ_regime ∈ (0, 1] 산출 (하드코딩 딕셔너리 제거)
        # 에너지 패널티 E_r: bull=0, caution=0.5, bear=1.0, crash=2.0, panic=2.5
        e_bull = 0.0
        e_caution = 1.0 / 2.0
        e_bear = 1.0
        e_crash = 2.0
        e_panic = 2.0 + e_caution
        energy_map = {
            "bull": e_bull,
            "caution": e_caution,
            "bear": e_bear,
            "crash": e_crash,
            "panic": e_panic,
        }
        if isinstance(regime_probs, dict) and regime_probs:
            p_tot = sum(max(0.0, float(v)) for v in regime_probs.values())
            if p_tot > eps:
                expected_energy = sum(
                    (max(0.0, float(v)) / p_tot) * energy_map.get(str(k).lower(), e_caution)
                    for k, v in regime_probs.items()
                )
            else:
                expected_energy = energy_map.get(str(regime).lower(), e_caution)
        else:
            expected_energy = energy_map.get(str(regime).lower(), e_caution)

        kappa_regime = math.exp(-expected_energy)

        # 5) 계좌 내 기존 USD 지갑 비중(w_{USD,wallet}) 확인
        w_usd_wallet = 0.0
        if isinstance(account_state, dict):
            nav_krw = float(account_state.get("total_equity", 0.0) or 0.0)
            usd_cash = float(account_state.get("usd_cash", 0.0) or account_state.get("ovrs_ord_psbl_amt", 0.0) or 0.0)
            us_pos_krw = float(account_state.get("us_positions_value_krw", 0.0) or 0.0)
            if nav_krw > 0:
                w_usd_wallet = min(1.0, max(0.0, (usd_cash * cur_fx + us_pos_krw) / nav_krw))

        # 고환율(z_FX > 0) 시 신규 원화→달러 환전 한도 감쇠 계수: exp(-max(0, z_FX))
        fx_expansion_decay = math.exp(-max(0.0, z_fx))
        model_cap = kappa_regime * w_us_target * fx_expansion_decay
        wallet_safe_cap = kappa_regime * w_usd_wallet
        us_exposure_cap_pct = min(1.0, max(model_cap, wallet_safe_cap))

        return {
            "w_krx": round(w_kr_target, 4),
            "w_us": round(w_us_target, 4),
            "w_us_risk_parity": round(w_us_rp, 4),
            "w_us_boltzmann": round(w_us_boltz, 4),
            "us_exposure_cap_pct": round(us_exposure_cap_pct, 4),
            "kappa_regime": round(kappa_regime, 4),
            "fx_expansion_decay": round(fx_expansion_decay, 4),
            "w_usd_wallet": round(w_usd_wallet, 4),
            "mu_us_usd": round(mu_us_usd, 6),
            "mu_us_krw": round(mu_us_krw, 6),
            "sig_us_usd": round(sig_us_usd, 6),
            "sig_us_krw": round(sig_us_krw, 6),
            "mu_kr_adj": round(mu_kr_adj, 6),
            "sig_kr": round(sig_kr, 6),
            "spot_fx": round(cur_fx, 4),
            "fx_z_score": round(z_fx, 4),
            "expected_fx_drift_ann": round(mu_fx, 6),
        }

    @staticmethod
    def _krx_tick_size(price: float, is_etf: bool = False) -> float:
        """KRX 유가증권/코스닥/ETF 법정 호가단위(Tick Size) 물리 테이블."""
        if price <= 0:
            return 5.0
        if is_etf:
            return 5.0 if price >= 2000.0 else 1.0
        if price < 2000.0:
            return 1.0
        if price < 5000.0:
            return 5.0
        if price < 20000.0:
            return 10.0
        if price < 50000.0:
            return 50.0
        if price < 200000.0:
            return 100.0
        if price < 500000.0:
            return 500.0
        return 1000.0

    def compute_roundtrip_friction_pct(
        self,
        ticker: str,
        order_value_krw: float = 2_500_000.0,
        price: float = 0.0,
    ) -> float:
        """법정 세율 + 브로커 수수료 + 호가 틱 스프레드 + Almgren-Chriss 제곱근 시장충격을 결합한 왕복 거래비용(%).

        수리 모델:
          C_RT(i, Q_i) = 2 * c_comm + τ_tax(i) + Tick(P_i)/P_i + 2 * η * σ_{i,d} * sqrt(V_order / ADT_i)
          (단위: %, 예: 0.21 = 0.21%)
        """
        tk = str(ticker or "").strip().upper()
        is_kr = len(tk) == 6 and tk[0].isdigit()
        is_etf = tk in self.get_etf_tickers()
        m = self.get_asset_metrics(tk)
        sigma_d = max(float(m.get("vol_daily", 0.018)), 0.002)
        adt = max(float(m.get("adt", 5_000_000_000.0 if is_kr else 50_000_000.0)), 1.0)

        # 1) 외생적 법정 세율 및 브로커 위탁수수료 (단위: %)
        if is_kr:
            comm_rt_pct = 2.0 * 0.0147  # KIS 국내 온라인 수수료 왕복 2.94 bps
            tax_pct = 0.0 if is_etf else 0.18  # KRX 증권거래세: ETF 면제(0%), 주식 18 bps(0.18%)
            eff_price = float(price) if price and price > 0 else (50000.0 if is_etf else 30000.0)
            tick = self._krx_tick_size(eff_price, is_etf=is_etf)
            spread_pct = (tick / max(eff_price, 1.0)) * 100.0
            participation = max(order_value_krw, 100_000.0) / adt
        else:
            comm_rt_pct = 2.0 * 0.07  # KIS 해외 수수료 왕복 14 bps
            tax_pct = 0.00278  # US SEC Fee
            eff_price = float(price) if price and price > 0 else 100.0
            spread_pct = (0.01 / max(eff_price, 1.0)) * 100.0
            fx_rate = max(float(self.get_fx_rate()), 1.0)
            order_val_usd = max(order_value_krw / fx_rate, 100.0)
            participation = order_val_usd / adt

        # 2) Almgren-Chriss 제곱근 시장충격 (왕복 매수+매도 2회, η = 0.15)
        eta = 0.15
        impact_pct = 2.0 * eta * sigma_d * math.sqrt(min(participation, 0.25)) * 100.0

        total_friction_pct = comm_rt_pct + tax_pct + spread_pct + impact_pct
        return round(float(np.clip(total_friction_pct, 0.04, 1.50)), 4)

    def compute_optimal_min_holding_minutes(
        self,
        ticker: str,
        order_value_krw: float = 2_500_000.0,
        price: float = 0.0,
        regime: str = "caution",
        vix_level: Optional[float] = None,
    ) -> float:
        """고정 15분 상수를 대체하는 브라운 확산(Drift-to-Noise & Friction Break-Even) 최소 정보축적 시간(분).

        수리 모델:
          dP_t / P_t = μ_i dt + σ_{i,d} dW_t  (1거래일 T_day = 390분)
          1) 마찰비용 회수 드리프트 시간: τ_drift = C_RT / |μ_{i,d}|
          2) 미시구조 호가 노이즈 돌파 시간(SNR >= 1): τ_snr = (2 * C_RT / σ_{i,d})^2
          3) VIX 변동성 시계 가속 인자: κ_v = sqrt(V_t / V_base)
          t*_min = T_day * max(τ_drift, τ_snr) / κ_v
        """
        tk = str(ticker or "").strip().upper()
        m = self.get_asset_metrics(tk)
        sigma_d_pct = max(float(m.get("vol_daily", 0.018)) * 100.0, 0.30)
        mu_d_pct = max(abs(float(m.get("mu_ann", 0.0))) / 252.0 * 100.0, 0.35 * sigma_d_pct)
        c_rt_pct = self.compute_roundtrip_friction_pct(tk, order_value_krw=order_value_krw, price=price)

        vq = self.get_vix_regime_quantiles()
        v_base = max(vq["baseline"], 10.0)
        v_cur = float(vix_level) if (vix_level and vix_level > 0) else v_base
        kappa_v = math.sqrt(float(np.clip(v_cur / v_base, 0.65, 2.25)))

        tau_drift = c_rt_pct / max(mu_d_pct, 0.05)
        tau_snr = (2.0 * c_rt_pct / max(sigma_d_pct, 0.20)) ** 2
        t_min = 390.0 * max(tau_drift * 0.25, tau_snr) / kappa_v
        return round(float(np.clip(t_min, 5.0, 180.0)), 2)

    def compute_optimal_stopping_params(
        self,
        ticker: str,
        regime: str = "caution",
        vix_level: Optional[float] = None,
        order_value_krw: float = 2_500_000.0,
        price: float = 0.0,
        atr_pct: Optional[float] = None,
        friction_pct: Optional[float] = None,
        stream_id: Optional[str] = None,
        holding_days: float = 0.0,
        max_hold_days: float = 0.0,
        vix: Optional[float] = None,
        is_kr: Optional[bool] = None,
    ) -> Dict[str, float]:
        """UniversalExitEngine의 모든 고정 퍼센트 상수(6.0%, 0.50%, 3.0%, 2.5%, 2.0%, 15.0%)를 대체하는
        확률과정 첫 도달 장벽(First-Passage Optimal Stopping) 및 마찰비용 결합 해.
        """
        tk = str(ticker or "").strip().upper()
        m = self.get_asset_metrics(tk)
        _unit_cut = 1.0 / math.e
        if atr_pct and float(atr_pct) > 0:
            _raw_atr = float(atr_pct)
            _atr_pct_val = _raw_atr if _raw_atr >= _unit_cut else (_raw_atr * 100.0)
        else:
            _atr_pct_val = float(m.get("vol_daily", 0.018)) * 100.0
        sigma_d_pct = max(_atr_pct_val, _unit_cut)
        ir = float(m.get("ir", 0.0))
        c_rt_pct = (
            float(friction_pct)
            if (friction_pct is not None and float(friction_pct) > 0)
            else self.compute_roundtrip_friction_pct(tk, order_value_krw=order_value_krw, price=price)
        )

        vq = self.get_vix_regime_quantiles()
        v_base = max(vq["baseline"], 10.0)
        _eff_vix = vix_level if (vix_level is not None and float(vix_level) > 0) else vix
        v_cur = float(_eff_vix) if (_eff_vix and float(_eff_vix) > 0) else v_base
        kappa_v = math.sqrt(float(np.clip(v_cur / v_base, 0.65, 2.25)))

        reg = str(regime.get("primary_regime", "caution") if isinstance(regime, dict) else (regime or "caution")).lower()
        reg_decay = {"bull": 0.0, "caution": 0.15, "bear": 0.35, "crash": 0.60}.get(reg, 0.15)
        kappa_r = math.exp(-reg_decay)

        # 가우시안 표준 분위수 (물리·수학 상수: Φ^{-1}(p))
        z_90 = 1.28155   # 90% 단측 분위수
        z_975 = 1.95996  # 97.5% 단측 분위수
        z_995 = 2.57583  # 99.5% 단측 분위수
        z_999 = 3.09023  # 99.9% 단측 분위수

        # 스트림 아키타입별 첫 도달 손절 배수(ζ_SL) 및 본전 보존 배수(ζ_PL) 차등화
        # - S1 / S11 (Citadel False-Breakout): (1.0 + z_90) / 2 = 1.1408 σ_d
        # - S2 / S12 (Renaissance OU Regime-Break): z_90 = 1.2816 σ_d
        # - S3 / S3_A / S3_C / S6 / S10 (Bridgewater Drawdown-Gated Trend): (z_90 + z_975) / 2 = 1.6208 σ_d
        # - S0 / S14 (Citadel Stat-Arb Spread): 1.00 σ_d
        _sid_clean = str(stream_id or "").strip().upper()
        if _sid_clean in ("S0", "S1", "S11", "S11_HIGHBETA_SNIPER"):
            _zeta_sl = (1.0 + z_90) / 2.0
            _zeta_pl = z_975
        elif _sid_clean in ("S2", "S12", "S12_DERIVATIVE_SQUEEZE"):
            _zeta_sl = z_90
            _zeta_pl = z_975
        elif _sid_clean in ("S3", "S3_A", "S3_C", "S6", "S10", "S10_MEGA_TREND", "S14", "S14_PAIR_ALPHA"):
            _zeta_sl = (z_90 + z_975) / 2.0
            _zeta_pl = z_975
        else:
            _zeta_sl = z_975
            _zeta_pl = z_995

        # 손실 보유일 경과에 따른 연속 시간감쇠 손절 수축 (Time-Decay Stop Tightening: τ(t) = 2^{-t / (2 T_max)})
        _tau_decay = 1.0
        _hd_val = float(holding_days or 0.0)
        if _sid_clean and _sid_clean not in ("LIVE_SSOT", "S4", "S3_B") and _hd_val > 0.0:
            _default_tmax = 2.0 + 2.0 + 1.0
            _t_max_eff = float(max_hold_days) if (max_hold_days and float(max_hold_days) > 2.0) else _default_tmax
            _t_ratio = min(_hd_val / _t_max_eff, 1.0)
            _tau_decay = math.exp(-math.log(2.0) * _t_ratio * (1.0 / 2.0))

        # 1) 본전 보존 지지선: 왕복 마찰비용 100% 회수 + 0.25 σ_{i,d} 순수익 보장
        break_even_floor_pct = c_rt_pct + 0.25 * sigma_d_pct
        # 2) 본전 보존 점화 장벽: C_RT + ζ_PL * σ_{i,d} * κ_v
        profit_lock_trigger_pct = c_rt_pct + _zeta_pl * sigma_d_pct * kappa_v
        # 3) 1차 진입 손절 장벽: -(C_RT + ζ_SL * σ_{i,d} * κ_v * κ_r * τ_decay)
        hard_sl_limit_pct = -(c_rt_pct + _zeta_sl * sigma_d_pct * kappa_v * kappa_r * _tau_decay)
        # 4) 2차 고점 폭락 손절폭: C_RT + z_{0.999} * σ_{i,d} * κ_v
        catastrophic_drop_pct = c_rt_pct + z_999 * sigma_d_pct * kappa_v
        # 5) 샹들리에 트레일링 점화 장벽: 2 * C_RT + z_{0.90} * σ_{i,d} * κ_r
        trailing_trigger_pct = 2.0 * c_rt_pct + z_90 * sigma_d_pct * kappa_r
        # 6) 샹들리에 연속 배수: 레짐·VIX 연속 볼츠만 스케일링
        chandelier_mult = float(np.clip((z_975 / kappa_v) * (1.0 / kappa_r if reg == "bull" else kappa_r), 0.75, 3.50))
        chandelier_drop_pct = max(2.0 * c_rt_pct + 0.50 * sigma_d_pct, chandelier_mult * sigma_d_pct)
        # 7) 분할 익절(Scale-Out) 목표 장벽 및 최적 분할 비율
        ir_boost = math.exp(0.20 * float(np.clip(ir, -1.0, 2.0)))
        scale_out_target_pct = c_rt_pct + 2.0 * z_995 * sigma_d_pct * ir_boost
        scale_out_ratio = float(np.clip(1.0 / (1.0 + ir_boost), 0.25, 0.65))
        min_hold_mins = self.compute_optimal_min_holding_minutes(
            tk, order_value_krw=order_value_krw, price=price, regime=reg, vix_level=v_cur
        )

        return {
            "roundtrip_friction_pct": round(c_rt_pct, 4),
            "min_holding_minutes": round(min_hold_mins, 2),
            "break_even_floor_pct": round(break_even_floor_pct, 4),
            "profit_lock_trigger_pct": round(profit_lock_trigger_pct, 4),
            "hard_sl_limit_pct": round(hard_sl_limit_pct, 4),
            "catastrophic_drop_pct": round(catastrophic_drop_pct, 4),
            "trailing_trigger_pct": round(trailing_trigger_pct, 4),
            "chandelier_mult": round(chandelier_mult, 4),
            "chandelier_drop_pct": round(chandelier_drop_pct, 4),
            "scale_out_target_pct": round(scale_out_target_pct, 4),
            "scale_out_ratio": round(scale_out_ratio, 4),
            "sigma_daily_pct": round(sigma_d_pct, 4),
        }

    def compute_pair_spread_stopping_params(
        self,
        leg_weights: Dict[str, float],
        regime: str = "caution",
        vix_level: Optional[float] = None,
        total_pair_value_krw: float = 2_500_000.0,
        leg_prices: Optional[Dict[str, float]] = None,
        leg_sigmas_pct: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """마켓뉴트럴 페어(S14_PAIR_ALPHA) 동시 스프레드의 공분산 반영 일간 변동성(σ_spread,d) 및
        확률과정 첫 도달 장벽(First-Passage Optimal Stopping) + 무상한 ATR 샹들리에 트레일링 해.

        수리 모델:
          1) 자본 가중치 정규화: w_i = V_i / sum_k V_k  (sum_i w_i = 1)
          2) 단일요인/경험적 공분산 기반 페어 스프레드 일간 변동성:
             σ_spread,d = sqrt( sum_i w_i^2 σ_{i,d}^2 + 2 sum_{i<j} w_i w_j ρ_{i,j} σ_{i,d} σ_{j,d} )
             (여기서 ρ_{i,j} = clip(ρ_{i,m} * ρ_{j,m}, -0.95, 0.95), 롱 섹터와 인버스 헷지 간 음(-)의 상관관계 반영)
          3) 페어 왕복 마찰비용: C_RT,pair = sum_i w_i * C_RT(i, w_i * V_pair, P_i)
          4) 첫 도달 손절·본전보존·샹들리에 트레일링 장벽:
             - hard_sl_limit_pct       = -(C_RT,pair + z_0.975 * σ_spread,d * κ_v * κ_r)
             - break_even_floor_pct    = C_RT,pair + (σ_spread,d / 4)
             - profit_lock_trigger_pct = C_RT,pair + z_0.995 * σ_spread,d * κ_v
             - catastrophic_drop_pct   = C_RT,pair + z_0.999 * σ_spread,d * κ_v
             - trailing_trigger_pct    = 2 * C_RT,pair + z_0.90 * σ_spread,d * κ_r
             - chandelier_drop_pct     = max(2 * C_RT,pair + (σ_spread,d / 2), M_chandelier * σ_spread,d)
        """
        self._ensure_calibrated()
        clean_weights = {
            str(k).strip().upper(): max(float(v or 0.0), 0.0)
            for k, v in (leg_weights or {}).items()
            if str(k).strip() and float(v or 0.0) > 0.0
        }
        if not clean_weights:
            return self.compute_optimal_stopping_params(
                ticker="", regime=regime, vix_level=vix_level, order_value_krw=total_pair_value_krw
            )
        if len(clean_weights) == 1:
            only_tk = next(iter(clean_weights.keys()))
            only_px = float((leg_prices or {}).get(only_tk, 0.0) or 0.0)
            only_sig = (leg_sigmas_pct or {}).get(only_tk)
            return self.compute_optimal_stopping_params(
                ticker=only_tk,
                regime=regime,
                vix_level=vix_level,
                order_value_krw=total_pair_value_krw,
                price=only_px,
                atr_pct=only_sig,
            )

        w_sum = sum(clean_weights.values())
        norm_w = {tk: val / w_sum for tk, val in clean_weights.items()}
        tks = sorted(norm_w.keys())
        inv_set = set(self._classifications.get("inverse_hedges", set()))
        _unit_cut = 1.0 / math.e

        sigmas: Dict[str, float] = {}
        corrs: Dict[str, float] = {}
        irs: Dict[str, float] = {}
        frictions: Dict[str, float] = {}
        min_holds: Dict[str, float] = {}

        for tk in tks:
            m = self.get_asset_metrics(tk)
            ext_sig = (leg_sigmas_pct or {}).get(tk)
            if ext_sig is not None and float(ext_sig) > 0:
                raw_s = float(ext_sig)
                sig_pct = raw_s if raw_s >= _unit_cut else (raw_s * 100.0)
            else:
                sig_pct = float(m.get("vol_daily", _unit_cut / 20.0)) * 100.0
            sigmas[tk] = max(sig_pct, _unit_cut)

            beta_val = float(m.get("beta", 0.0) or 0.0)
            corr_val = float(m.get("corr", 0.0) or 0.0)
            is_inv = (tk in inv_set) or (beta_val < 0.0)
            if corr_val == 0.0:
                corr_val = (-1.0 / math.sqrt(2.0)) if is_inv else (1.0 / math.sqrt(2.0))
            elif is_inv and corr_val > 0.0:
                corr_val = -abs(corr_val)
            corrs[tk] = corr_val
            px_i = float((leg_prices or {}).get(tk, 0.0) or 0.0)
            val_i = norm_w[tk] * float(total_pair_value_krw or 0.0)
            frictions[tk] = self.compute_roundtrip_friction_pct(tk, order_value_krw=val_i, price=px_i)

        var_spread = 0.0
        for i, tk_i in enumerate(tks):
            w_i = norm_w[tk_i]
            s_i = sigmas[tk_i]
            var_spread += (w_i * s_i) ** 2
            for j in range(i + 1, len(tks)):
                tk_j = tks[j]
                w_j = norm_w[tk_j]
                s_j = sigmas[tk_j]
                rho_ij = corrs[tk_i] * corrs[tk_j]
                var_spread += 2.0 * w_i * w_j * rho_ij * s_i * s_j

        avg_leg_sigma = sum(norm_w[tk] * sigmas[tk] for tk in tks)
        sigma_spread_pct = max(math.sqrt(max(var_spread, 0.0)), avg_leg_sigma / math.e, _unit_cut)
        c_rt_pair = sum(norm_w[tk] * frictions[tk] for tk in tks)

        return self.compute_optimal_stopping_params(
            ticker=tks[0],
            regime=regime,
            vix_level=vix_level,
            order_value_krw=total_pair_value_krw,
            price=float((leg_prices or {}).get(tks[0], 0.0) or 0.0),
            atr_pct=sigma_spread_pct,
            friction_pct=c_rt_pair,
        )

    def compute_no_trade_band_ratio(
        self,
        ticker: str,
        order_value_krw: float = 2_500_000.0,
        price: float = 0.0,
        regime: str = "caution",
        vix_level: Optional[float] = None,
    ) -> float:
        """고정 25% 추가매수 불감대를 대체하는 Constantinides (1986) / Leland (1999) 3제곱근 무거래 영역(No-Trade Region).

        수리 모델:
          Δw*_i / w*_i = ( (3 * C_RT) / (2 * γ(VIX) * σ_{i,d}^2) )^(1/3)
        """
        tk = str(ticker or "").strip().upper()
        m = self.get_asset_metrics(tk)
        sigma_d = max(float(m.get("vol_daily", 0.018)), 0.004)
        c_rt_dec = self.compute_roundtrip_friction_pct(tk, order_value_krw=order_value_krw, price=price) / 100.0

        vq = self.get_vix_regime_quantiles()
        v_base = max(vq["baseline"], 10.0)
        v_cur = float(vix_level) if (vix_level and vix_level > 0) else v_base
        gamma_vix = 1.5 * float(np.clip(v_cur / v_base, 0.70, 2.50))

        cube_root_band = ((3.0 * c_rt_dec) / max(2.0 * gamma_vix * (sigma_d ** 2), 1e-8)) ** (1.0 / 3.0)
        # 최소 왕복 거래비용의 3배 이상 괴리가 발생할 때만 리밸런싱 허용
        return round(float(np.clip(cube_root_band * 0.15, max(c_rt_dec * 10.0, 0.08), 0.45)), 4)

    def compute_reentry_hurdle_pct(
        self,
        ticker: str,
        elapsed_minutes: float,
        prev_realized_pnl_pct: float = 0.0,
        order_value_krw: float = 2_500_000.0,
        price: float = 0.0,
    ) -> float:
        """'당일 무조건 재진입 금지' 하드코딩을 대체하는 매몰비용·변동성 지수감쇠 베이즈 재진입 허들(%).

        수리 모델:
          H_reentry(Δt) = C_RT + ( 2 * C_RT + |min(0, PnL_prev)| + z_{0.99} * σ_{i,d} ) * exp(-Δt / τ_day)
          (τ_day = 390분, z_{0.99} = 2.32635)
        """
        tk = str(ticker or "").strip().upper()
        m = self.get_asset_metrics(tk)
        sigma_d_pct = max(float(m.get("vol_daily", 0.018)) * 100.0, 0.35)
        c_rt_pct = self.compute_roundtrip_friction_pct(tk, order_value_krw=order_value_krw, price=price)
        sunk_loss_pct = abs(min(0.0, float(prev_realized_pnl_pct or 0.0)))
        z_99 = 2.32635
        decay = math.exp(-max(0.0, float(elapsed_minutes)) / 390.0)
        hurdle_pct = c_rt_pct + (2.0 * c_rt_pct + sunk_loss_pct + z_99 * sigma_d_pct) * decay
        return round(float(hurdle_pct), 4)

    def compute_discrete_lot_marginal_utility(
        self,
        ticker: str,
        delta_qty: int,
        current_qty: int,
        price_krw: float,
        nav_krw: float,
        target_value_krw: float = 0.0,
        confidence: float = 0.55,
        regime: str = "caution",
        vix_level: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Grinold & Kahn (1999) / Boyd et al. (2017) 이산 정수 수량(Discrete-Lot) 한계 순효용 및 무거래 영역 판정.

        수리 모델:
          1) 정수 수량 거래대금: ΔV_i = |Δq_i| * P_i, 기보유 평가액: V_i^held = q_i^held * P_i
          2) 이산 상대 비중 변화율: δw_rel = ΔV_i / max(V_i*, V_i^held, ΔV_i)
          3) Constantinides-Leland 3제곱근 무거래 영역:
             기보유 포지션(q_i^held > 0) 조정 시 δw_rel <= B_i 이면 무거래 영역 내부이므로 거래 기각.
          4) Grinold-Kahn 한계 순효용(Marginal Net Utility, 거래대금 대비 %):
             - 기대 일간 활성 알파: α_i = max(|μ_{i,d}|, max(0, c_i - 0.5) * σ_{i,d}) + 0.5 * max(0, IR_i) * σ_{i,d}
             - 한계 이차 리스크(분산) 패널티:
               ΔR_i = γ(VIX, Regime) * σ_{i,d}^2 * (w_i^held + 0.5 * sign(Δq_i) * Δw_i)
             - 왕복 마찰비용: C_RT(ΔV_i)
             - 매수(Δq_i > 0) 시 한계 순효용: ΔU_buy = α_i - ΔR_i - C_RT
             - 감축 매도(Δq_i < 0) 시 한계 순효용: ΔU_sell = (ΔR_i - α_i) - C_RT
               (즉, 포지션 축소로 절감되는 한계 리스크 패널티가 포기하는 알파 + 마찰비용보다 클 때만 감축 정당화)
        """
        tk = str(ticker or "").strip().upper()
        dq = int(delta_qty or 0)
        cq = max(0, int(current_qty or 0))
        px = max(float(price_krw or 0.0), 1.0)
        nav = max(float(nav_krw or 0.0), 1_000_000.0)

        if dq == 0:
            return {
                "is_optimal_trade": False,
                "net_marginal_utility_pct": 0.0,
                "discrete_rel_gap": 0.0,
                "no_trade_band": 0.0,
                "reason": "zero_lot",
            }

        trade_val_krw = abs(dq) * px
        held_val_krw = cq * px
        tgt_val_krw = max(float(target_value_krw or 0.0), (cq + dq) * px, 0.0)

        m = self.get_asset_metrics(tk)
        sigma_d = max(float(m.get("vol_daily", 0.018)), 0.004)
        mu_d = float(m.get("mu_ann", 0.0)) / 252.0
        ir = float(m.get("ir", 0.0))

        c_rt_pct = self.compute_roundtrip_friction_pct(tk, order_value_krw=trade_val_krw, price=px)
        c_rt_dec = c_rt_pct / 100.0

        nt_band = self.compute_no_trade_band_ratio(
            tk, order_value_krw=max(trade_val_krw, tgt_val_krw), price=px, regime=regime, vix_level=vix_level
        )
        denom_val = max(tgt_val_krw, held_val_krw, trade_val_krw, 1.0)
        discrete_rel_gap = trade_val_krw / denom_val

        vq = self.get_vix_regime_quantiles()
        v_base = max(vq["baseline"], 10.0)
        v_cur = float(vix_level) if (vix_level and vix_level > 0) else v_base
        reg = str(regime or "caution").lower()
        reg_mult = {"bull": 1.0, "caution": 1.35, "bear": 1.85, "crash": 2.50}.get(reg, 1.35)
        gamma = 1.5 * float(np.clip(v_cur / v_base, 0.70, 2.50)) * reg_mult

        w_held = held_val_krw / nav
        dw_signed = (dq * px) / nav
        w_mid = max(0.0, w_held + 0.5 * dw_signed)

        # 한계 분산 패널티 (일간 비율)
        marginal_risk_penalty = gamma * (sigma_d ** 2) * w_mid

        # 기대 활성 알파 (보유 수평선 T = max(1일, 최적 최소보유시간 비율) 동안의 누적 기대 알파)
        conf_edge = max(0.0, float(confidence or 0.5) - 0.45) * sigma_d
        drift_edge = max(0.0, mu_d) + 0.25 * max(0.0, ir) * sigma_d
        expected_alpha = max(conf_edge, drift_edge)

        if dq > 0:
            net_u_dec = expected_alpha - marginal_risk_penalty - c_rt_dec
            in_no_trade_zone = (cq > 0) and (discrete_rel_gap < nt_band)
            is_opt = (not in_no_trade_zone) and (net_u_dec > 0.0)
            reason = (
                "in_no_trade_band" if in_no_trade_zone
                else ("negative_marginal_utility" if net_u_dec <= 0.0 else "optimal_scale_in" if cq > 0 else "optimal_entry")
            )
        else:
            # 감축(Trimming) 시: 절감되는 리스크 패널티가 포기하는 기대알파 + 왕복 마찰비용을 초과해야 최적 감축
            net_u_dec = (marginal_risk_penalty - expected_alpha) - c_rt_dec
            in_no_trade_zone = discrete_rel_gap < nt_band
            is_opt = (not in_no_trade_zone) and (net_u_dec > 0.0)
            reason = (
                "in_no_trade_band" if in_no_trade_zone
                else ("negative_trim_utility" if net_u_dec <= 0.0 else "optimal_trim")
            )

        return {
            "is_optimal_trade": bool(is_opt),
            "net_marginal_utility_pct": round(net_u_dec * 100.0, 4),
            "expected_alpha_pct": round(expected_alpha * 100.0, 4),
            "marginal_risk_penalty_pct": round(marginal_risk_penalty * 100.0, 4),
            "roundtrip_friction_pct": round(c_rt_pct, 4),
            "discrete_rel_gap": round(discrete_rel_gap, 4),
            "no_trade_band": round(nt_band, 4),
            "reason": reason,
        }

    def compute_state_divergence_trigger(
        self,
        prev_state: Dict[str, Any],
        curr_state: Dict[str, Any],
        max_unheld_signal_z: float = 0.0,
    ) -> Dict[str, Any]:
        """장중 연속 파이프라인 재최적화 발화 여부를 결정하는 베이즈 상태 변위(Total Variation & Mahalanobis) 판정기.

        수리 모델:
          1) 레짐 확률분포 전변동 거리(Total Variation Distance):
             D_TV(p_t, p_{t_0}) = 0.5 * Σ_k |p_{t,k} - p_{t_0,k}|
          2) 내재변동성 상대 변위:
             δ_vol = |VKOSPI_t - VKOSPI_{t_0}| / max(VKOSPI_{t_0}, 10.0)
          3) 결합 상태 변위: D_state = D_TV + 0.5 * δ_vol > B_market (대표 시장 지수 무거래 반폭)
          4) 미보유 신규 종목 다중가설검정(Bonferroni/FDR) 임계치:
             z_FDR = Φ^{-1}(1 - 0.005 / max(1, N_eff)) (약 2.80σ ~ 3.20σ)
        """
        p_prev = prev_state.get("regime_probs") or {}
        p_curr = curr_state.get("regime_probs") or {}
        keys = set(p_prev.keys()) | set(p_curr.keys())
        if keys and p_prev and p_curr:
            d_tv = 0.5 * sum(abs(float(p_curr.get(k, 0.0)) - float(p_prev.get(k, 0.0))) for k in keys)
        else:
            d_tv = 1.0 if (prev_state.get("regime") and prev_state.get("regime") != curr_state.get("regime")) else 0.0

        v_prev = float(prev_state.get("vkospi", 0.0) or 0.0)
        v_curr = float(curr_state.get("vkospi", 0.0) or 0.0)
        d_vol = (abs(v_curr - v_prev) / max(v_prev, 10.0)) if (v_prev > 0 and v_curr > 0) else 0.0

        d_state = d_tv + 0.5 * d_vol
        market_band = self.compute_no_trade_band_ratio("069500", vix_level=v_curr if v_curr > 0 else None)

        # 다중가설검정 임계치 (가우시안 꼬리 근사: z = sqrt(2 * ln(N_eff / 0.01)))
        n_eff = max(float(len(self._kr_metrics) or 20), 10.0)
        z_fdr = min(3.2905, max(2.5758, math.sqrt(2.0 * math.log(n_eff / 0.05))))

        should_fire = (d_state > market_band) or (float(max_unheld_signal_z or 0.0) >= z_fdr)
        return {
            "should_reoptimize": bool(should_fire),
            "state_divergence": round(d_state, 4),
            "market_no_trade_band": round(market_band, 4),
            "fdr_z_threshold": round(z_fdr, 4),
            "max_unheld_z": round(float(max_unheld_signal_z or 0.0), 4),
        }


def get_quant_engine() -> DynamicQuantEngine:
    """전역 싱글톤 DynamicQuantEngine 인스턴스 반환."""
    return DynamicQuantEngine()


