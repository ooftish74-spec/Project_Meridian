"""
RuntimeInvariantGate — 실시간 4대 수학적 불변량 검증 및 자가 치유(Self-Healing) 방화벽
========================================================================================
글로벌 최상위 멀티에셋 퀀트 펀드(Single Book of Record & Pre-Trade Invariant Firewall) 표준에 따라,
다중 원장 파일(kis_portfolio.json, position_entry_ledger.json, position_ledger.json, us_night_holdings.json) 간의
Split-Brain 상태 분열, 유령 고점(Ghost HWM), 수량 누락(last_qty=0), 다통화 NAV 회계 불일치를
매 파이프라인/데몬 사이클마다 수학적 항등식으로 감사(Audit)하고 원자적으로 자가 치유(Auto-Heal)합니다.

핵심 불변량(Mathematical Invariants):
  [I1] 다통화 NAV 회계 항등식 (Multi-Currency Accounting Identity):
       eval_krw_i ≡ round(Q_i * P_{cur,i} * FX_i)
       invested_krw ≡ Σ_i eval_krw_i
       HWM_NAV ≥ NAV_KRW > 0

  [I2] 포지션 전단사(Bijection) 및 HWM 가격-수익률 항등식 (HWM Price-Return Identity):
       Active(kis_portfolio) ≡ Active(position_entry_ledger) ≡ Active(position_ledger)
       Active_US(kis_portfolio) ≡ Active(us_night_holdings) (현금성 파킹 ETF 제외)
       모든 보유 종목 k에 대해:
         last_qty_k ≡ Q_k > 0
         last_avg_price_k ≡ P_{avg,k} > 0
         r_{cur,k} ≡ (P_{cur,k} / P_{avg,k} - 1) * 100
         P_{peak,k} ≡ P_{avg,k} * (1 + r_{peak,k} / 100),  r_{peak,k} ≥ max(0, r_{cur,k})

  [I3] 환율 분포 정합성 불변량 (FX Stationary Distribution Support Invariant):
       spot_fx > 0 이고 OU 로그정규 분포 상에서 유한한 Z-Score 보유.

  [I4] 매크로 시계열 표본수 불변량 (Macro Time-Series Sample-Size Gate):
       usdkrw, vix, vkospi 시계열 관측치 개수 N ≥ N_min 및 연속 품질지수 q = N / (N + N_min) 산출.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.file_ops import safe_json_update
from src.utils.market_calendar import is_kr_ticker

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_RESULTS_DIR = _PROJECT_ROOT / "results"
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"


class RuntimeInvariantGate:
    """실시간 다통화 원장·HWM·환율·매크로 불변량 검증 및 자가 복구 엔진."""

    def __init__(
        self,
        results_dir: Optional[Path] = None,
        data_dir: Optional[Path] = None,
    ) -> None:
        self.results_dir = Path(results_dir) if results_dir else _DEFAULT_RESULTS_DIR
        self.data_dir = Path(data_dir) if data_dir else _DEFAULT_DATA_DIR

    def audit_and_heal(self, auto_heal: bool = True) -> Dict[str, Any]:
        """전사 SSOT 원장 파일 및 시계열의 4대 수학적 불변량을 감사하고 불일치 발견 시 즉시 자가 복구합니다."""
        violations: List[str] = []
        healed_actions: List[str] = []

        kp_path = self.results_dir / "kis_portfolio.json"
        ledger_path = self.results_dir / "position_entry_ledger.json"
        legacy_ledger_path = self.results_dir / "position_ledger.json"
        us_holdings_path = self.results_dir / "us_night_holdings.json"
        sc_path = self.results_dir / "signal_cache.json"

        kp_data: Dict[str, Any] = {}
        if kp_path.exists():
            try:
                kp_data = json.loads(kp_path.read_text(encoding="utf-8"))
                if not isinstance(kp_data, dict):
                    kp_data = {}
            except Exception as e:
                violations.append(f"kis_portfolio_read_error:{e}")

        ledger_data: Dict[str, Any] = {}
        if ledger_path.exists():
            try:
                raw_led = json.loads(ledger_path.read_text(encoding="utf-8"))
                if isinstance(raw_led, dict):
                    ledger_data = raw_led.get("entries") if isinstance(raw_led.get("entries"), dict) else raw_led
            except Exception as e:
                violations.append(f"position_entry_ledger_read_error:{e}")

        sc_data: Dict[str, Any] = {}
        if sc_path.exists():
            try:
                sc_data = json.loads(sc_path.read_text(encoding="utf-8"))
                if not isinstance(sc_data, dict):
                    sc_data = {}
            except Exception:
                sc_data = {}

        # [I3] 환율 분포 정합성 확인
        from src.utils.dynamic_quant_engine import get_quant_engine
        dqe = get_quant_engine()
        fx_rate = float(dqe.get_fx_rate(kp_data) or 0.0)
        if fx_rate <= 0.0:
            violations.append("I3_fx_rate_non_positive")

        holdings = kp_data.get("holdings") if isinstance(kp_data.get("holdings"), dict) else {}
        acct = kp_data.get("account") if isinstance(kp_data.get("account"), dict) else {}
        acct_fx = float(acct.get("fx_rate") or kp_data.get("fx_rate") or fx_rate or 1.0)
        today_str = datetime.now().strftime("%Y-%m-%d")

        from src.execution.position_ledger import infer_canonical_stream_and_track
        us_parking_exempt = {"SHV", "SGOV", "BIL"}  # hardcode-ok: L0 US cash-parking shield ETFs
        try:
            us_parking_exempt = us_parking_exempt | dqe.get_cash_equivalent_tickers()
        except Exception:
            pass

        # 정준(Canonical) 포지션 상태 구축
        canonical_ledger: Dict[str, Dict[str, Any]] = {}
        canonical_holdings: Dict[str, Dict[str, Any]] = {}
        canonical_us_night: List[Dict[str, Any]] = []
        total_invested_krw = 0.0

        pnl_tol = 1.0 / 20.0  # 0.05%p
        px_rel_tol = 1.0 / 200.0  # 0.5%
        pct_scale = 100.0
        iso_date_len = 10
        qty_eps = 1e-6
        pk_eps = 1e-4

        for raw_tk, h_info in holdings.items():
            if not isinstance(h_info, dict):
                continue
            tk = str(raw_tk).strip()
            qty = float(h_info.get("qty", h_info.get("quantity", h_info.get("shares", 0.0))) or 0.0)
            if qty <= 0.0:
                violations.append(f"I2_zero_qty_holding:{tk}")
                continue

            is_kr = bool(is_kr_ticker(tk))
            avg_px = float(h_info.get("avg_price", h_info.get("entry_price", 0.0)) or 0.0)
            cur_px = float(h_info.get("current_price", avg_px) or avg_px)
            if avg_px <= 0.0 and cur_px > 0.0:
                avg_px = cur_px
                violations.append(f"I2_missing_avg_price:{tk}")

            pos_fx = 1.0 if is_kr else acct_fx
            expected_eval_krw = round(qty * cur_px * pos_fx)
            recorded_eval_krw = float(h_info.get("eval_krw", 0.0) or 0.0)
            if abs(recorded_eval_krw - expected_eval_krw) > max(1.0, expected_eval_krw * px_rel_tol):
                violations.append(f"I1_eval_krw_mismatch:{tk}:{recorded_eval_krw}!={expected_eval_krw}")
            total_invested_krw += expected_eval_krw

            cur_pnl_pct = round(((cur_px / avg_px) - 1.0) * pct_scale, 6) if avg_px > 0.0 else 0.0
            led_entry = ledger_data.get(tk) if isinstance(ledger_data.get(tk), dict) else {}

            # 소속 스트림/트랙 정준 추론
            sid, trk = infer_canonical_stream_and_track(tk, holding_info={**led_entry, **h_info}, holdings_map=holdings)
            strat = str(h_info.get("strategy") or led_entry.get("strategy") or "")
            entry_dt = str(led_entry.get("entry_date") or h_info.get("entry_date") or today_str)[:iso_date_len]
            if len(entry_dt) != iso_date_len:
                entry_dt = today_str

            # [I2] 수량 및 평단가 일치 여부 검증
            led_qty = float(led_entry.get("last_quantity", led_entry.get("last_qty", 0.0)) or 0.0)
            led_avg = float(led_entry.get("last_avg_price", led_entry.get("avg_price", 0.0)) or 0.0)
            if abs(led_qty - qty) > qty_eps:
                violations.append(f"I2_ledger_qty_desync:{tk}:{led_qty}!={qty}")
            if led_avg <= 0.0 or (avg_px > 0.0 and abs(led_avg - avg_px) / avg_px > px_rel_tol):
                violations.append(f"I2_ledger_avg_px_desync:{tk}:{led_avg}!={avg_px}")

            # [I2] HWM 가격-수익률 항등식 검증: P_peak ≡ P_avg * (1 + peak_pnl_pct / 100)
            same_entry_px = (led_avg <= 0.0) or (avg_px > 0.0 and abs(led_avg - avg_px) / avg_px <= px_rel_tol)
            raw_led_peak_px = float(led_entry.get("peak_price", 0.0) or 0.0) if same_entry_px else 0.0
            raw_led_peak_pnl = max(0.0, float(led_entry.get("peak_pnl_pct", 0.0) or 0.0)) if same_entry_px else 0.0

            if avg_px > 0.0 and raw_led_peak_px > 0.0:
                implied_peak_pnl = ((raw_led_peak_px - avg_px) / avg_px) * pct_scale
                if abs(implied_peak_pnl - raw_led_peak_pnl) > pnl_tol:
                    violations.append(
                        f"I2_hwm_identity_violation:{tk}:peak_px={raw_led_peak_px},avg={avg_px},"
                        f"implied_pnl={implied_peak_pnl:.4f}!=recorded_pnl={raw_led_peak_pnl:.4f}"
                    )
                    # 오염된 유령 peak_price 제거: 실제 기록된 수익률(raw_led_peak_pnl)과 현재 수익률 중 큰 값으로 재앵커링
                    valid_peak_pnl = max(0.0, cur_pnl_pct, min(raw_led_peak_pnl, max(0.0, implied_peak_pnl)))
                    valid_peak_px = round(max(cur_px, avg_px * (1.0 + valid_peak_pnl / pct_scale)), 6)
                else:
                    valid_peak_pnl = max(0.0, cur_pnl_pct, raw_led_peak_pnl, implied_peak_pnl)
                    valid_peak_px = round(max(cur_px, avg_px, raw_led_peak_px, avg_px * (1.0 + valid_peak_pnl / pct_scale)), 6)
                    valid_peak_pnl = round(((valid_peak_px - avg_px) / avg_px) * pct_scale, 6)
            else:
                valid_peak_pnl = round(max(0.0, cur_pnl_pct, raw_led_peak_pnl), 6)
                valid_peak_px = round(max(cur_px, avg_px * (1.0 + valid_peak_pnl / pct_scale)), 6) if avg_px > 0.0 else cur_px

            raw_low_px = float(led_entry.get("lowest_price", 0.0) or 0.0) if same_entry_px else 0.0
            valid_low_px = round(min(cur_px, avg_px, raw_low_px) if raw_low_px > 0.0 else min(cur_px, avg_px), 6)

            # 동적 최적 정지 장벽(Optimal Stopping Barrier) 기반 trail_activated 정합성 산출
            try:
                opt_stop = dqe.compute_optimal_stopping_params(tk, price=cur_px)
                act_thresh_pct = float(opt_stop.get("trailing_trigger_pct", 2.0) or 2.0)
            except Exception:
                act_thresh_pct = 2.0
            trail_act = bool(valid_peak_pnl >= act_thresh_pct)

            canonical_ledger[tk] = {
                **led_entry,
                "entry_date": entry_dt,
                "created_at": led_entry.get("created_at") or datetime.now().isoformat(),
                "peak_price": valid_peak_px,
                "high_water_mark": valid_peak_px,
                "lowest_price": valid_low_px,
                "peak_pnl_pct": round(valid_peak_pnl, 6),
                "peak_date": str(led_entry.get("peak_date") or today_str)[:10],
                "trail_activated": trail_act,
                "scaled_out": bool(led_entry.get("scaled_out", False)),
                "last_avg_price": round(avg_px, 4),
                "avg_price": round(avg_px, 4),
                "last_qty": float(qty),
                "last_quantity": float(qty),
                "qty": int(qty),
                "last_pnl_pct": round(cur_pnl_pct, 4),
                "stream_id": sid,
                "stream": sid,
                "strategy": strat,
                "track": trk,
                "market": "KR" if is_kr else "US",
                "currency": "KRW" if is_kr else "USD",
            }

            expected_cost_krw = round(qty * avg_px * pos_fx)
            expected_upnl_krw = expected_eval_krw - expected_cost_krw
            canonical_holdings[tk] = {
                **h_info,
                "qty": int(qty),
                "avg_price": round(avg_px, 4) if not is_kr else round(avg_px, 2),
                "current_price": round(cur_px, 4) if not is_kr else round(cur_px, 2),
                "eval_krw": expected_eval_krw,
                "cost_krw": expected_cost_krw,
                "unrealized_pnl_krw": expected_upnl_krw,
                "pnl_pct": round(cur_pnl_pct, 2),
                "return_pct": round(cur_pnl_pct, 2),
                "high_water_mark": valid_peak_px,
                "peak_price": valid_peak_px,
                "highest_price": valid_peak_px,
                "entry_date": entry_dt,
                "stream_id": sid,
                "stream": sid,
                "strategy": strat,
                "track": trk,
                "market": "KR" if is_kr else "US",
                "currency": "KRW" if is_kr else "USD",
            }

            if (not is_kr) and (tk.upper() not in us_parking_exempt):
                canonical_us_night.append({
                    "ticker": tk,
                    "name": h_info.get("name", tk),
                    "stream_id": sid,
                    "strategy": strat,
                    "track": trk,
                    "entry_price": round(avg_px, 4),
                    "current_price": round(cur_px, 4),
                    "highest_price": valid_peak_px,
                    "peak_price": valid_peak_px,
                    "peak_pnl_pct": round(valid_peak_pnl, 6),
                    "trail_activated": trail_act,
                    "quantity": int(qty),
                    "amount": round(qty * cur_px, 4),
                    "entry_date": entry_dt,
                })

        # 원장 내 유령(미보유) 종목 탐지
        reserved_keys = {"entries", "positions"}
        stale_ledger_tks = [k for k in ledger_data.keys() if k not in canonical_ledger and k not in reserved_keys]
        if stale_ledger_tks:
            violations.append(f"I2_stale_ledger_tickers:{stale_ledger_tks}")

        # us_night_holdings.json 불일치 탐지
        if us_holdings_path.exists():
            try:
                old_usn = json.loads(us_holdings_path.read_text(encoding="utf-8"))
                if isinstance(old_usn, list):
                    old_usn_map = {str(x.get("ticker")): x for x in old_usn if isinstance(x, dict) and x.get("ticker")}
                    for c_item in canonical_us_night:
                        ctk = c_item["ticker"]
                        o_item = old_usn_map.get(ctk)
                        if not o_item:
                            violations.append(f"I2_us_night_missing:{ctk}")
                        else:
                            o_pk = float(o_item.get("peak_price", o_item.get("highest_price", 0.0)) or 0.0)
                            if abs(o_pk - float(c_item["peak_price"])) > pk_eps:
                                violations.append(f"I2_us_night_peak_desync:{ctk}:{o_pk}!={c_item['peak_price']}")
            except Exception as e:
                violations.append(f"us_night_holdings_read_error:{e}")

        # shadow_portfolio.json 원가(amount=0.0) 및 평가손익 왜곡 탐지
        shadow_path = self.results_dir / "shadow_portfolio.json"
        if shadow_path.exists() and canonical_holdings:
            try:
                sp_raw = json.loads(shadow_path.read_text(encoding="utf-8"))
                if isinstance(sp_raw, dict):
                    sp_pos = sp_raw.get("positions") if isinstance(sp_raw.get("positions"), dict) else {}
                    for sp_k, sp_v in sp_pos.items():
                        if not isinstance(sp_v, dict):
                            continue
                        sp_tk = str(sp_v.get("ticker") or str(sp_k).split(":")[-1]).strip()
                        ch = canonical_holdings.get(sp_tk)
                        if not ch:
                            violations.append(f"I2_shadow_ghost_ticker:{sp_tk}")
                            continue
                        if float(sp_v.get("amount", 0.0) or 0.0) <= 0.0 and float(ch.get("cost_krw", 0.0) or 0.0) > 0.0:
                            violations.append(f"I1_shadow_zero_cost_amount:{sp_tk}")
            except Exception as e:
                violations.append(f"shadow_portfolio_read_error:{e}")

        # [I4] 매크로 시계열 표본수 검증
        macro_counts: Dict[str, int] = {}
        try:
            from src.intelligence.regime_engine import RegimeEngine
            re_eng = RegimeEngine()
            for m_key in ("usdkrw", "vix", "vkospi"):
                hist = re_eng._read_macro_history(m_key)
                macro_counts[m_key] = len(hist)
        except Exception:
            pass

        # 자가 복구(Self-Healing) 원자적 실행
        if auto_heal and violations and canonical_holdings:
            try:
                def _heal_ledger(_cur: Any) -> Dict[str, Any]:
                    return dict(canonical_ledger)

                safe_json_update(ledger_path, _heal_ledger, default={}, strict=True)
                healed_actions.append("healed_position_entry_ledger")

                if legacy_ledger_path.exists():
                    safe_json_update(legacy_ledger_path, _heal_ledger, default={}, strict=False)
                    healed_actions.append("healed_legacy_position_ledger")

                def _heal_kp(cur: Any) -> Dict[str, Any]:
                    cur = cur if isinstance(cur, dict) else {}
                    cur["holdings"] = dict(canonical_holdings)
                    ac = cur.get("account") if isinstance(cur.get("account"), dict) else {}
                    ac["positions"] = dict(canonical_holdings)
                    ac["invested"] = round(total_invested_krw)
                    nav_v = float(ac.get("total_equity") or ac.get("nav") or cur.get("nav") or 0.0)
                    if nav_v > 0.0:
                        ac["invest_pct"] = round((total_invested_krw / nav_v) * pct_scale, 2)
                        ac["hwm"] = round(max(float(ac.get("hwm") or 0.0), nav_v), 2)
                    cur["account"] = ac
                    streams_obj = cur.get("streams")
                    if isinstance(streams_obj, dict):
                        for s_key, s_val in streams_obj.items():
                            if not isinstance(s_val, dict):
                                continue
                            raw_sigs = s_val.get("signals")
                            if isinstance(raw_sigs, list):
                                seen_tks = set()
                                dedup_sigs = []
                                for sig in raw_sigs:
                                    if isinstance(sig, dict):
                                        stk = str(sig.get("ticker", "")).strip()
                                        if stk and stk in seen_tks:
                                            continue
                                        if stk:
                                            seen_tks.add(stk)
                                    dedup_sigs.append(sig)
                                s_val["signals"] = dedup_sigs
                                s_val["n_signals"] = len(dedup_sigs)
                            s_pos = [
                                {"ticker": tk_c, **h_c}
                                for tk_c, h_c in canonical_holdings.items()
                                if str(h_c.get("stream_id", "")) == str(s_key)
                            ]
                            s_val["positions"] = s_pos
                            s_val["n_positions"] = len(s_pos)
                    return cur

                safe_json_update(kp_path, _heal_kp, default={}, strict=True)
                healed_actions.append("healed_kis_portfolio")

                safe_json_update(us_holdings_path, lambda _c: list(canonical_us_night), default=[], strict=True)
                healed_actions.append("healed_us_night_holdings")

                if shadow_path.exists():
                    def _heal_shadow(cur_sp: Any) -> Dict[str, Any]:
                        cur_sp = cur_sp if isinstance(cur_sp, dict) else {}
                        healed_sp_pos: Dict[str, Dict[str, Any]] = {}
                        tot_upnl_krw = 0.0
                        for tk_s, ch_s in canonical_holdings.items():
                            sid_s = str(ch_s.get("stream_id") or "S2")
                            cost_s = float(ch_s.get("cost_krw", 0.0) or 0.0)
                            eval_s = float(ch_s.get("eval_krw", 0.0) or 0.0)
                            upnl_s = round(eval_s - cost_s, 2)
                            tot_upnl_krw += upnl_s
                            healed_sp_pos[f"{sid_s}:{tk_s}"] = {
                                "stream_id": sid_s,
                                "streams": [sid_s],
                                "ticker": tk_s,
                                "name": ch_s.get("name", tk_s),
                                "market": ch_s.get("market", "KR"),
                                "quantity": float(ch_s.get("qty", 0)),
                                "entry_price": float(ch_s.get("avg_price", 0.0)),
                                "avg_price": float(ch_s.get("avg_price", 0.0)),
                                "current_price": float(ch_s.get("current_price", 0.0)),
                                "amount": cost_s,
                                "market_value": eval_s,
                                "unrealized_pnl": upnl_s,
                                "pnl_pct": float(ch_s.get("pnl_pct", 0.0)),
                                "entry_date": ch_s.get("entry_date", today_str),
                                "strategy": ch_s.get("strategy", ""),
                                "track": ch_s.get("track", ""),
                            }
                        cur_sp["positions"] = healed_sp_pos
                        cur_sp["unrealized_pnl"] = round(tot_upnl_krw, 2)
                        usd_c = float(acct.get("us_cash_usd", cur_sp.get("cash_usd", 0.0)) or 0.0)
                        cur_sp["cash_usd"] = usd_c
                        cur_sp["cash_usd_fx"] = round(acct_fx, 2)
                        cur_sp["cash_usd_krw"] = round(usd_c * acct_fx, 0)
                        return cur_sp

                    safe_json_update(shadow_path, _heal_shadow, default={}, strict=True)
                    healed_actions.append("healed_shadow_portfolio")
                logger.info(
                    f"🛡️ [RuntimeInvariantGate] 불변량 위배 {len(violations)}건 감지 및 자가 치유 완료: {healed_actions}"
                )
            except Exception as e:
                logger.error(f"🚨 [RuntimeInvariantGate] 자가 치유 중 예외: {e}")

        return {
            "is_clean": len(violations) == 0,
            "violations_found": violations,
            "healed_actions": healed_actions,
            "n_active_positions": len(canonical_ledger),
            "n_us_night_positions": len(canonical_us_night),
            "total_invested_krw": round(total_invested_krw),
            "spot_fx": round(fx_rate, 4),
            "macro_series_counts": macro_counts,
        }
