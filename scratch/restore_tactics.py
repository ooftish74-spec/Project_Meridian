import re

with open('src/streams/s1_edge/etf_sniper_stream.py', 'r') as f:
    code = f.read()

# 1. Insert trigger conditions
trigger_code = """        tactic_c_hyn_trigger = (hyn_vol_z > vr_z_threshold) and (hyn_anomaly_z > vr_z_threshold)

        from datetime import datetime
        now = datetime.now()
        is_moc_window = (now.hour == 14 and now.minute >= 50) or (now.hour == 15 and now.minute <= 20)
        
        tactic_d_trigger = False
        tactic_d_direction = 'long'
        tactic_c_global_trigger = False
        
        if is_moc_window:
            if lp_pressure > 200 and vol_z_score > 2.0:
                tactic_d_trigger = True
                tactic_d_direction = 'long'
            elif lp_pressure < -200 and vol_z_score > 2.0:
                tactic_d_trigger = True
                tactic_d_direction = 'short'
                
            us_regime = regime_info.get('us_regime', 'neutral')
            if us_regime in ['bull', 'neutral'] and vix < 18:
                tactic_c_global_trigger = True
"""
code = code.replace("        tactic_c_hyn_trigger = (hyn_vol_z > vr_z_threshold) and (hyn_anomaly_z > vr_z_threshold)", trigger_code)

# 2. Fix the return condition to include new triggers
code = code.replace("if not is_shock and not any([tactic_a_trigger, tactic_b_trigger, tactic_c_sam_trigger, tactic_c_hyn_trigger]):",
                    "if not is_shock and not any([tactic_a_trigger, tactic_b_trigger, tactic_c_sam_trigger, tactic_c_hyn_trigger, tactic_d_trigger, tactic_c_global_trigger]):")

# 3. Insert Signal Generation blocks
signal_code = """        # Tactic D 최우선 판별
        if tactic_d_trigger:
            logger.warning(f"    🎯 [Tactic D] LP MOC 헤징 압박 감지! 선행매매 진입 (방향: {tactic_d_direction})")
            _t_type = 'index_1x' if tactic_d_direction == 'long' else 'index_inv_1x'
            ticker = self._get_ticker_by_type(_t_type)
            price_data = signal_cache.get(ticker)
            price = float(price_data.get('close', 0.0)) if isinstance(price_data, dict) else float(price_data or 0.0)
            if price > 0:
                signals.append({
                    'stream_id': self.stream_id,
                    'ticker': ticker,
                    'name': self.universe[ticker]['name'],
                    'direction': 'long',
                    'size_pct': 1.0,
                    'price': price,
                    'confidence': 1.0,
                    'strategy': 'tactic_d_moc_lp_frontrun',
                    'reason': f"LP MOC Hedging Arb (Pressure: {lp_pressure:.1f})",
                    'tp_pct': tp_pct,
                    'sl_pct': sl_pct,
                    'holding_time': 'MOC',
                    'execution_algo': 'vwap'
                })
        elif tactic_c_global_trigger:
            logger.warning("    🎯 [Tactic C-Global] 글로벌 디커플링 감지! 오버나잇 갭상승 베팅 (2배수 롱).")
            ticker = self._get_ticker_by_type('index_lev_2x')
            price_data = signal_cache.get(ticker)
            price = float(price_data.get('close', 0.0)) if isinstance(price_data, dict) else float(price_data or 0.0)
            if price > 0:
                signals.append({
                    'stream_id': self.stream_id,
                    'ticker': ticker,
                    'name': self.universe[ticker]['name'],
                    'direction': 'long',
                    'size_pct': 1.0,
                    'price': price,
                    'confidence': 1.0,
                    'strategy': 'tactic_c_global_decoupling',
                    'reason': f"US Neutral + KOSPI Drop Decoupling",
                    'tp_pct': tp_pct * tactic_bc_tp_multiplier,
                    'sl_pct': sl_pct,
                    'holding_time': 'OVERNIGHT',
                    'execution_algo': 'vwap'
                })
        elif tactic_c_sam_trigger:"""
code = code.replace("        elif tactic_c_sam_trigger:" if "elif tactic_c_sam_trigger:" in code else "        if tactic_c_sam_trigger:", signal_code)

# 4. Restore 2x Leverage logic for Tactic A, B, and Shock
tactic_a_old = """        elif tactic_a_trigger:
            logger.warning("    🎯 [Tactic A] Wag-the-Dog 감지: LP 기계적 매도압력 폭발. 인버스 진입.") # Tactic A-1: 인버스 저격
            ticker = self._get_ticker_by_type('index_inv')"""
tactic_a_new = """        elif tactic_a_trigger:
            _is_extreme = ofi_prob < 0.05
            _t_type = 'index_inv_2x' if _is_extreme else 'index_inv_1x'
            logger.warning(f"    🎯 [Tactic A] Wag-the-Dog 감지. OFI Prob: {ofi_prob:.4f}. {'2배 곱버스' if _is_extreme else '1배 인버스'} 진입.") 
            ticker = self._get_ticker_by_type(_t_type) or self._get_ticker_by_type('index_inv')"""
code = code.replace(tactic_a_old, tactic_a_new)

tactic_b_old = """        elif tactic_b_trigger:
            logger.warning("    🎯 [Tactic B] Climax 역발상 감지: 비정상 변동성 피크 후 진정세. 레버리지 진입.") # Tactic B-1: 레버리지/롱 저격
            ticker = self._get_ticker_by_type('index')"""
tactic_b_new = """        elif tactic_b_trigger:
            _is_extreme = vol_z_score > 2.5
            _t_type = 'index_lev_2x' if _is_extreme else 'index_1x'
            logger.warning(f"    🎯 [Tactic B] Climax 역발상 감지. Vol Z-Score: {vol_z_score:.2f}. {'2배 레버리지' if _is_extreme else '1배 롱'} 진입.") 
            ticker = self._get_ticker_by_type(_t_type) or self._get_ticker_by_type('index')"""
code = code.replace(tactic_b_old, tactic_b_new)

shock_old = """        elif is_shock:
            logger.info("    🎯 [VIX Shock] 순수 변동성 스파이크 감지. 인버스 스나이핑 진입.")
            ticker = self._get_ticker_by_type('index_inv')"""
shock_new = """        elif is_shock:
            vix_z = (vix - vix_ma20) / max(vix_std20, 1e-9)
            _is_extreme = vix_z > 2.5
            _t_type = 'index_inv_2x' if _is_extreme else 'index_inv_1x'
            logger.info(f"    🎯 [VIX Shock] 순수 변동성 스파이크 감지. VIX Z: {vix_z:.2f}. {'2배 곱버스' if _is_extreme else '1배 인버스'} 진입.")
            ticker = self._get_ticker_by_type(_t_type) or self._get_ticker_by_type('index_inv')"""
code = code.replace(shock_old, shock_new)

with open('src/streams/s1_edge/etf_sniper_stream.py', 'w') as f:
    f.write(code)

print("Restoration successful.")
