import re

with open('scripts/stream_orchestrator.py', 'r') as f:
    content = f.read()

# 1. Modify stream initialization and all_signals logic
# Insert S10 manual execution after S3
loop_str = """        # ── Step 4: 각 스트림 신호 생성 ──
        logger.info("  📋 Step 4: 스트림 신호 생성")
        all_signals = {}
        s3_sector_scores = {}
        for stream in self.streams:
            if not stream.is_active():
                logger.info(f"    ⏸ {stream.stream_id} 비활성")
                continue

            try:
                signals = stream.generate_signals(regime, market_data)
                # Capture S3 sector scores if this is S3
                if stream.stream_id == 'S3_MACRO' and isinstance(signals, dict):
                    s3_sector_scores = signals.get('sector_scores', {})
                    signals = signals.get('orders', signals) # fallback to dict if orders not present, wait S3 usually returns list

                all_signals[stream.stream_id] = signals
                logger.info(
                    f"    ✅ {stream.stream_id}: {len(signals)}개 신호")
            except Exception as e:
                logger.error(f"    ❌ {stream.stream_id} 신호 생성 실패: {e}")
                all_signals[stream.stream_id] = []

        # ── Step 4.0: S10 Mega-Trend 신호 생성 ──
        try:
            s10_out = self.s10.generate_signals(market_data, s3_sector_scores)
            s10_signals = s10_out.get('orders', [])
            all_signals['S10_MEGA_TREND'] = s10_signals
            market_data['s10_status'] = s10_out.get('s10_status', 'neutral')
            logger.info(f"    ✅ S10_MEGA_TREND: {len(s10_signals)}개 신호 (Status: {market_data['s10_status']})")
        except Exception as e:
            logger.error(f"    ❌ S10_MEGA_TREND 신호 생성 실패: {e}")
            all_signals['S10_MEGA_TREND'] = []
            market_data['s10_status'] = 'neutral'
"""

old_loop = """        # ── Step 4: 각 스트림 신호 생성 ──
        logger.info("  📋 Step 4: 스트림 신호 생성")
        all_signals = {}
        for stream in self.streams:
            if not stream.is_active():
                logger.info(f"    ⏸ {stream.stream_id} 비활성")
                continue

            try:
                signals = stream.generate_signals(regime, market_data)
                all_signals[stream.stream_id] = signals
                logger.info(
                    f"    ✅ {stream.stream_id}: {len(signals)}개 신호")
            except Exception as e:
                logger.error(f"    ❌ {stream.stream_id} 신호 생성 실패: {e}")
                all_signals[stream.stream_id] = []"""

content = content.replace(old_loop, loop_str)

with open('scripts/stream_orchestrator.py', 'w') as f:
    f.write(content)
print("Patched!")
