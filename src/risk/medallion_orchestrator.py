"""
Medallion Orchestrator — 4원칙 포트폴리오 검증
=================================================
Sleeve A/B 전체 포트폴리오에 Medallion 3+1 원칙 적용.

원칙:
  ① 50.75% — 비용 차감 후 엣지(Kelly EV≥0.30%)가 있을 때만 거래
  ② 리스크 관리 — Dynamic Exposure (F&G/VIX/레짐)
  ③ 대수의 법칙 — 분산 (섹터/종목) + 비상관성
  ④ Never Override — 모델 결정 존중 (Kill Switch만 허용)

Usage:
    from src.risk.medallion_orchestrator import MedallionOrchestrator
    orch = MedallionOrchestrator()
    result = orch.validate_all()
"""
import json, logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional
logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'
try:
    from config.dynamic_config import DynamicConfig
    _cfg = DynamicConfig()
except ImportError as e:
    _cfg = None
try:
    from src.risk.stream_correlation import StreamCorrelationMonitor
    _CORR_AVAILABLE = True
except ImportError as e:
    _CORR_AVAILABLE = False
try:
    from src.measurement.pnl_attribution import PnLAttribution
    _PNL_AVAILABLE = True
except ImportError as e:
    _PNL_AVAILABLE = False
ETF_TICKERS = {'122630', '091160', '396500', '471230', '305080', '132030', '279530', '458730', '458760', '133690', '441640', '289480', '411060', '211900', '211560', '091180', '379800'}

class MedallionOrchestrator:
    """Sleeve A/B 전체 포트폴리오 Medallion 검증."""

    def __init__(self):
        self.max_sector_per_stream = 2
        self.max_sector_global = 4
        self.max_single_pct = 0.15
        self.min_sectors = 3
        self.fg_greed_threshold = 70
        self.fg_extreme_greed = 85
        if _cfg:
            self.max_sector_per_stream = _cfg.get('medallion.max_sector_per_stream', 2)
            self.max_sector_global = _cfg.get('medallion.max_sector_global', 4)
            self.max_single_pct = _cfg.get('medallion.max_single_pct', 0.15)
            self.min_sectors = _cfg.get('medallion.min_sectors', 3)
            self.fg_greed_threshold = _cfg.get('medallion.fg_greed_threshold', 70)
            self.fg_extreme_greed = _cfg.get('medallion.fg_extreme_greed', 85)
        self._corr_monitor = StreamCorrelationMonitor() if _CORR_AVAILABLE else None
        self._pnl_engine = PnLAttribution() if _PNL_AVAILABLE else None

    def validate_all(self) -> Dict:
        """4원칙 전체 검증."""
        logger.info('═' * 60)
        logger.info('🏛️ Medallion Orchestrator — 포트폴리오 4원칙 검증')
        positions = self._collect_positions()
        sentiment = self._get_sentiment()
        v1 = self._validate_edge(positions)
        v2 = self._validate_risk(positions, sentiment)
        v3 = self._validate_diversification(positions)
        v4 = self._validate_no_override()
        total_issues = sum((v['n_issues'] for v in [v1, v2, v3, v4]))
        critical = sum((1 for v in [v1, v2, v3, v4] for i in v.get('issues', []) if i.get('severity') == 'CRITICAL'))
        overall = 'FAIL' if critical > 0 else 'WARN' if total_issues > 0 else 'PASS'
        result = {'timestamp': datetime.now().isoformat(), 'overall': overall, 'total_issues': total_issues, 'critical': critical, 'sentiment': sentiment, 'validations': {'edge': v1, 'risk': v2, 'diversification': v3, 'no_override': v4}}
        for name, v in [('①엣지', v1), ('②리스크', v2), ('③분산', v3), ('④비개입', v4)]:
            icon = '✅' if v['status'] == 'PASS' else '🟡' if v['status'] == 'WARN' else '🔴'
            logger.info(f'  {icon} {name}: {v['status']} ({v['n_issues']}건)')
        logger.info(f'  ═══ 종합: {overall} (이슈 {total_issues}, Critical {critical}) ═══')
        corr_check = None
        if self._corr_monitor:
            try:
                corr_check = self._corr_monitor.measure()
                if corr_check.get('max_correlation', 0) > 0.8:
                    logger.warning(f'  ⚠️ 스트림 간 상관관계 높음: {corr_check.get('max_pair', '?')} = {corr_check['max_correlation']:.2f}')
            except Exception as e:
                logger.critical(f'  Correlation check skipped: {e}', exc_info=True)
        pnl_summary = None
        if self._pnl_engine:
            try:
                pnl_summary = self._pnl_engine.generate_report()
            except Exception as e:
                logger.critical(f'  PnL attribution skipped: {e}', exc_info=True)
        out = _RESULTS / 'medallion_validation.json'
        result['correlation'] = corr_check
        result['pnl_attribution'] = pnl_summary
        out.write_text(json.dumps(result, indent=2, default=str))
        return result

    def compute_exposure(self, sentiment: Optional[Dict]=None) -> Dict:
        """F&G/VIX 기반 목표 노출도."""
        if not sentiment:
            sentiment = self._get_sentiment()
        fg = sentiment.get('fear_greed', 50)
        vix = sentiment.get('vix', 20)
        regime = sentiment.get('regime', 'caution')
        exposure = 1.0
        reasons = []
        if fg >= self.fg_extreme_greed:
            exposure *= 0.4
            reasons.append(f'F&G={fg} (Extreme Greed)')
        elif fg >= self.fg_greed_threshold:
            exposure *= 0.6
            reasons.append(f'F&G={fg} (Greed)')
        vix_ext = _cfg.get('medallion.vix_extreme', 50) if _cfg else 50
        vix_high = _cfg.get('medallion.vix_high', 35) if _cfg else 35
        if vix >= vix_ext:
            exposure = 0.0
            reasons.append(f'VIX={vix} (Extreme >= {vix_ext})')
        elif vix >= vix_high:
            exposure *= 0.5
            reasons.append(f'VIX={vix} (High >= {vix_high})')
        if regime == 'crash':
            exposure = 0.0
            reasons.append('Regime=CRASH')
        elif regime == 'bear':
            exposure *= 0.5
            reasons.append('Regime=BEAR')
        return {'target_exposure': round(max(0, min(1, exposure)), 2), 'reason': ' + '.join(reasons) or 'Normal'}

    def _validate_edge(self, positions: Dict) -> Dict:
        """① 모든 포지션에 양의 엣지 확인."""
        issues = []
        for layer, pos_dict in positions.items():
            for ticker, pos in pos_dict.items():
                prob = pos.get('up_probability', 0.5)
                ev = pos.get('kelly_ev', None)
                if ev is not None and ev < 0.003:
                    issues.append({'layer': layer, 'ticker': ticker, 'issue': f'EV={ev:.4f} < 0.30% (음의 엣지)', 'severity': 'WARNING'})
                elif prob < 0.5:
                    issues.append({'layer': layer, 'ticker': ticker, 'issue': f'P(UP)={prob:.3f} < 0.50', 'severity': 'CRITICAL'})
        return {'principle': '① Edge', 'status': 'PASS' if not issues else 'WARN', 'issues': issues, 'n_issues': len(issues)}

    def _validate_risk(self, positions: Dict, sentiment: Dict) -> Dict:
        """② Dynamic Exposure + Stop-Loss 위반."""
        issues = []
        fg = sentiment.get('fear_greed', 50)
        if fg >= self.fg_extreme_greed:
            issues.append({'layer': 'portfolio', 'issue': f'F&G={fg} ≥ {self.fg_extreme_greed}', 'severity': 'CRITICAL'})
        for layer, pos_dict in positions.items():
            for ticker, pos in pos_dict.items():
                pnl = pos.get('unrealized_pnl_pct', 0)
                base_sl = _cfg.get('medallion.stop_loss_pct', -7) if _cfg else -7
                atr_pct = pos.get('atr_pct', 0)
                if atr_pct > 0:
                    sl_mult = _cfg.get('risk.atr_sl_multiplier', 2.0) if _cfg else 2.0
                    dynamic_sl = -1 * (atr_pct * 100 * sl_mult)
                    stop_loss = min(base_sl, dynamic_sl)
                else:
                    stop_loss = base_sl
                if isinstance(pnl, (int, float)) and pnl < stop_loss:
                    issues.append({'layer': layer, 'ticker': ticker, 'issue': f'PnL={pnl:.1f}% < {stop_loss:.1f}% (Dynamic SL 위반)', 'severity': 'CRITICAL'})
        has_critical = any((i['severity'] == 'CRITICAL' for i in issues))
        return {'principle': '② Risk', 'status': 'FAIL' if has_critical else 'PASS', 'issues': issues, 'n_issues': len(issues)}

    def _validate_diversification(self, positions: Dict) -> Dict:
        """③ 섹터 집중도 + 종목 중복 (스트림 인식).

        검증 로직:
          - ETF 티커는 섹터 집중도에서 제외 (자체적으로 분산됨)
          - 스트림별 같은 섹터 max_sector_per_stream 초과 → WARNING
            (S4 Advisory는 장기 투자 특성상 한도 완화: 3종목)
          - 글로벌 같은 섹터 max_sector_global 초과 → WARNING
          - 총 섹터 수 min_sectors 미만 → WARNING
        """
        issues = []
        global_sector_counts = {}
        stream_sector_counts = {}
        all_tickers = set()
        stream_sector_limits = {'S1': self.max_sector_per_stream, 'S2': self.max_sector_per_stream, 'S3': self.max_sector_per_stream, 'S4': self.max_sector_per_stream + 1}
        for layer, pos_dict in positions.items():
            for ticker, pos in pos_dict.items():
                parts = ticker.split(':')
                clean = parts[-1]
                stream_id = parts[0] if len(parts) > 1 else 'unknown'
                all_tickers.add(clean)
                if clean in ETF_TICKERS:
                    continue
                sector = pos.get('sector', 'Unknown')
                if sector == 'Unknown':
                    try:
                        from pykrx import stock as krx_stock
                        pass
                    except ImportError as e:
                        logger.critical('[SILENT_BYPASS] Suppressed exception at medallion_orchestrator.py:290', exc_info=True)
                global_sector_counts[sector] = global_sector_counts.get(sector, 0) + 1
                if stream_id not in stream_sector_counts:
                    stream_sector_counts[stream_id] = {}
                sc = stream_sector_counts[stream_id]
                sc[sector] = sc.get(sector, 0) + 1
        for sid, sc in stream_sector_counts.items():
            limit = stream_sector_limits.get(sid, self.max_sector_per_stream)
            for sec, cnt in sc.items():
                if sec != 'Unknown' and cnt > limit:
                    issues.append({'layer': 'stream', 'issue': f'[{sid}] 섹터 [{sec}] {cnt}종목 > {limit}', 'severity': 'WARNING'})
        for sec, cnt in global_sector_counts.items():
            if sec != 'Unknown' and cnt > self.max_sector_global:
                issues.append({'layer': 'aggregate', 'issue': f'섹터 [{sec}] 전체 {cnt}종목 > {self.max_sector_global}', 'severity': 'WARNING'})
        n_sectors = len(set(global_sector_counts.keys()) - {'Unknown'})
        n_individual = sum(global_sector_counts.values())
        if 0 < n_sectors < self.min_sectors and n_individual >= self.min_sectors:
            issues.append({'layer': 'portfolio', 'issue': f'{n_sectors}섹터 < {self.min_sectors} (분산 부족)', 'severity': 'WARNING'})
        has_warn = any((i['severity'] in ('CRITICAL', 'WARNING') for i in issues))
        return {'principle': '③ Diversification', 'status': 'WARN' if has_warn else 'PASS', 'sectors': global_sector_counts, 'stream_sectors': stream_sector_counts, 'total_sectors': n_sectors, 'issues': issues, 'n_issues': len(issues)}

    def _validate_no_override(self) -> Dict:
        """④ 인간 오버라이드 없음 — 동적 검증.

        Kill Switch가 트리거된 상태에서 can_buy=False인 경우만 WARNING.
        단순 active 상태(정상 안전장치 동작)는 alert에서 제외.
        """
        issues = []
        ks_path = _RESULTS / 'kill_switch.json'
        if ks_path.exists():
            try:
                ks = json.loads(ks_path.read_text())
                ks_triggered = ks.get('triggered', False)
                ks_can_buy = ks.get('can_buy', True)
                ks_reason = ks.get('reason', '')
                ks_scale = ks.get('position_scale', 1.0)
                if ks_triggered and (not ks_can_buy):
                    severity = 'CRITICAL' if ks_scale <= 0.0 else 'WARNING'
                    issues.append({'layer': 'system', 'issue': f'Kill Switch 발동: {ks_reason} (scale={ks_scale:.0%}, can_buy={ks_can_buy})', 'severity': severity})
                elif ks_triggered and ks_can_buy:
                    fo = ks.get('forward_override', {})
                    fo_reason = fo.get('reason', '')
                    issues.append({'layer': 'system', 'issue': f'Kill Switch 트리거 but Override 적용: {fo_reason}', 'severity': 'INFO'})
            except Exception as _e_mo1:
                logger.critical(f'  [medallion_orchestrator] 메달리온 계산 실패: {_e_mo1}', exc_info=True)
        meaningful_issues = [i for i in issues if i.get('severity') != 'INFO']
        has_critical = any((i['severity'] == 'CRITICAL' for i in meaningful_issues))
        has_warn = any((i['severity'] == 'WARNING' for i in meaningful_issues))
        status = 'FAIL' if has_critical else 'WARN' if has_warn else 'PASS'
        return {'principle': '④ No Override', 'status': status, 'issues': meaningful_issues, 'n_issues': len(meaningful_issues)}

    def _collect_positions(self) -> Dict:
        positions = {}
        try:
            sp = json.loads((_RESULTS / 'shadow_portfolio.json').read_text())
            positions['a3_shadow'] = sp.get('positions', {})
        except (FileNotFoundError, json.JSONDecodeError):
            positions['a3_shadow'] = {}
        except Exception as e:
            logger.critical(f'A3 shadow 포지션 로드 중 예상치 못한 에러: {e}', exc_info=True)
            raise
            positions['a3_shadow'] = {}
        try:
            adv = json.loads((_RESULTS / 'sleeve_b_advisory.json').read_text())
            positions['sleeve_b'] = adv.get('positions', {})
        except (FileNotFoundError, json.JSONDecodeError):
            positions['sleeve_b'] = {}
        except Exception as e:
            logger.critical(f'Sleeve B 포지션 로드 중 예상치 못한 에러: {e}', exc_info=True)
            raise
            positions['sleeve_b'] = {}
        return positions

    def _get_sentiment(self) -> Dict:
        sentiment = {'fear_greed': 50, 'vix': 20, 'regime': 'caution'}
        try:
            cache = json.loads((_RESULTS / 'signal_cache.json').read_text())
            fg = cache.get('FnG', {}).get('value')
            if fg:
                sentiment['fear_greed'] = float(fg)
            vix = cache.get('VIX', {}).get('value')
            if vix:
                sentiment['vix'] = float(vix)
        except Exception as _e_mo2:
            logger.critical(f'  [medallion_orchestrator] 오케스트레이터 실패: {_e_mo2}', exc_info=True)
        try:
            state = json.loads((_RESULTS / 'pipeline_state.json').read_text())
            sentiment['regime'] = state.get('regime', 'caution')
        except Exception as _e_mo3:
            logger.critical(f'  [medallion_orchestrator] pipeline_state 로드 실패: {_e_mo3}', exc_info=True)
        return sentiment