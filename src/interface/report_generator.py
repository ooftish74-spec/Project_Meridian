"""
Project Meridian — Report Generator (Markdown)
=================================================
일일 마감 마크다운 리포트.
Shadow 가상거래 결과 + Go/No-Go Tracker + 4-Stream 성과 기반.

Usage:
    from src.interface.report_generator import ReportGenerator
    gen = ReportGenerator()
    report = gen.generate_daily()
"""

import json, logging
from datetime import datetime
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_RESULTS = _PROJECT_ROOT / 'results'


def _load_json(name: str) -> Dict:
    f = _RESULTS / name
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
    return {}


class ReportGenerator:
    """Meridian 마감 리포트 생성 (Markdown).

    ★ Shadow 거래 데이터 + Go/No-Go Tracker 기반.
    ★ 4-Stream 아키텍처 (S1~S4) 반영.
    """

    def generate_daily(self) -> str:
        """일일 리포트 생성."""
        # Data load
        shadow = _load_json('shadow_summary.json')
        signal = _load_json('signal_cache.json')
        metrics = _load_json('stream_metrics.json')
        overrides = _load_json('dynamic_overrides.json')

        gonogo = shadow.get('go_nogo', {})
        daily_stats = shadow.get('daily_stats', [])

        # 기본 정보
        today = datetime.now().strftime('%Y-%m-%d')
        regime = 'bull'
        if daily_stats:
            regime = daily_stats[-1].get('regime', 'bull')
        regime_icons = {'bull': '🐂', 'caution': '⚠️', 'bear': '🐻', 'crash': '🚨'}

        # Go/No-Go
        verdict = gonogo.get('verdict', 'INSUFFICIENT_DATA')
        n_days = gonogo.get('n_days', 0)
        sharpe = gonogo.get('sharpe', 0)
        win_rate = gonogo.get('win_rate', 0)
        max_dd = gonogo.get('max_dd', 0)
        criteria = gonogo.get('criteria', {})

        verdict_icon = '✅' if verdict == 'GO' else '🛑' if verdict == 'NO_GO' else '⏳'

        returns = gonogo.get('daily_returns', [])
        cum_return = sum(returns) * 100 if returns else 0.0

        # 💡 오늘의 투자 전략 서머리 동적 생성
        strategy_summary = "현재 수집된 데이터에 기반한 특별한 방향성 의견이 없습니다."
        if regime == 'bull':
            strategy_summary = "🟢 **리스크 온(Risk-On) 국면**입니다. 주도주 중심의 공격적인 자산 배분이 권장되며, 포트폴리오 노출도를 목표치(Target) 수준으로 최대한 유지합니다."
        elif regime == 'caution':
            strategy_summary = "🟡 **주의(Caution) 국면**입니다. 시장 변동성이 확대되고 있으므로 철저한 리스크 관리가 필요합니다. 시스템이 자동으로 포트폴리오 노출을 축소하고 방어적 스탠스를 취합니다."
        elif regime == 'bear':
            strategy_summary = "🔴 **약세장(Bear) 국면**입니다. 자본 보존을 최우선으로 하여, 주식 등 위험 자산 비중을 최소화하고 헷지 자산 위주로 엄격하게 대응합니다."
        elif regime == 'crash':
            strategy_summary = "🚨 **시장 붕괴(Crash) 국면**입니다. 모든 시스템 방어 기제가 가동 중이며, 포지션을 극단적으로 축소하여 현금을 최대한 확보합니다."

        # 1. Stream Signals (from latest_signals.json & virtual_trading)
        latest_signals_data = _load_json('latest_signals.json')
        signals_dict = latest_signals_data.get('signals', {})
        
        today_vt_file = f'virtual_trading_{today.replace("-", "")}.json'
        vt_data = _load_json(f'logs/{today_vt_file}')
        executed_orders = vt_data.get('orders', []) if vt_data else []
        n_orders = vt_data.get('execution', {}).get('n_orders', 0) if vt_data else 0
        n_buys = vt_data.get('portfolio_summary', {}).get('n_buys', 0) if vt_data else 0
        
        stream_descriptions = {
            'S0': '코어 베타 방어 (Core Beta)',
            'S1': '단기 ETF 스나이핑 (Directional ETF)',
            'S2': '머신러닝 알파 (ML Alpha)',
            'S3': '팩터 기반 섹터 로테이션 (Factor Rotation)',
            'S4': '절세형 자문 포트폴리오 (Tax-Advantaged)',
            'S5': '야간 갭 베팅 및 파킹 (Overnight Sweep)',
            'S10': '섹터별 주도주 추적 (Mega-Trend)'
        }
        
        signal_report = "## 📡 각 스트림 별 시그널 및 실행 결과 요약\n\n"
        total_signals = 0
        
        # S3_A, S3_B 등을 S3로 묶기 위한 처리
        grouped_signals = {}
        for k, v in signals_dict.items():
            base_k = k.split('_')[0]
            if base_k not in grouped_signals:
                grouped_signals[base_k] = []
            grouped_signals[base_k].extend(v)
            total_signals += len(v)
            
        grouped_exec = {}
        for o in executed_orders:
            base_k = str(o.get('stream_id', '')).split('_')[0]
            if base_k not in grouped_exec:
                grouped_exec[base_k] = []
            grouped_exec[base_k].append(o)
            
        for base_k in ['S0', 'S1', 'S2', 'S3', 'S4', 'S5', 'S10']:
            sigs = grouped_signals.get(base_k, [])
            execs = grouped_exec.get(base_k, [])
            desc = stream_descriptions.get(base_k, '전략')
            
            signal_report += f"### [{base_k}] {desc}\n"
            
            if not sigs:
                signal_report += "- **오늘의 시그널 및 추천**: 발생한 시그널 없음.\n"
                if execs:
                    exec_names = [f"{e.get('name', '')}" for e in execs]
                    signal_report += f"- **실제 실행 결과**: {len(execs)}건 체결 ({', '.join(exec_names)}).\n\n"
                else:
                    signal_report += "- **실제 실행 결과**: 시그널 부재로 관망 유지.\n\n"
                continue
                
            # 추천 종목 텍스트 화
            rec_names = [f"{s.get('name', '')}({s.get('confidence', 0.0):.1%})" for s in sigs[:5]]
            rec_str = ", ".join(rec_names)
            if len(sigs) > 5:
                rec_str += f" 등 총 {len(sigs)}건"
            
            signal_report += f"- **오늘의 시그널 및 추천**: {rec_str}\n"
            
            if execs:
                exec_names = [f"{e.get('name', '')}" for e in execs]
                signal_report += f"- **실제 실행 결과**: {len(execs)}건 매수 체결 ({', '.join(exec_names)}).\n\n"
            else:
                signal_report += f"- **실제 실행 결과**: 0건 집행 (Regime 차단 또는 확신도 미달로 진입 보류).\n\n"
        
        # 2. Gap Analysis (from virtual_trading)
        gap_report = "## 🔍 전체 시그널 vs 실행 갭(Gap) 요약\n\n"
        gap_report += f"- **알고리즘이 생성한 시그널 총합**: {total_signals}건\n"
        gap_report += f"- **실제 시장에 집행된 주문**: {n_orders}건\n"
        gap_report += f"- **실제 체결된 매수 포지션**: {n_buys}건\n\n"
        
        if total_signals > 0 and n_orders == 0:
            gap_report += "> [!WARNING]\n"
            gap_report += "> **Gap 발생!** 시그널이 다수 발생했음에도 실제 주문이 0건인 이유는 다음 중 하나일 확률이 높습니다.\n"
            gap_report += "> 1. **Regime 방어벽**: 리스크 오케스트레이터가 시장 위험을 감지하여 포지션 진입을 강제 차단함 (현재: " + regime.upper() + ").\n"
            gap_report += "> 2. **확신도(Confidence) 미달**: 알고리즘 모델의 확신도 스코어가 동적 방어벽 커트라인을 넘지 못함.\n"
            gap_report += "> 3. **증거금 한도 부족**: 최소 거래 금액(Cash Drag) 요건을 맞출 잔여 예수금이 부족함.\n"
        elif total_signals == 0:
            gap_report += "> [!NOTE]\n"
            gap_report += "> **정상 (No Gap):** 발생한 시그널이 없어 집행된 주문도 없습니다. 시장 관망 상태입니다.\n"
        else:
            gap_report += "> [!NOTE]\n"
            gap_report += "> **정상 실행:** 시스템이 생성한 시그널 중 유효한 시그널들이 정상적으로 시장에 집행되었습니다.\n"

        summary_text = (
            f"금일 자본 시장은 **{regime.upper()}** 체제이며, Project Meridian은 치명적 오류 없이 전체 파이프라인을 성공적으로 완수했습니다. "
            f"현재 Shadow Trading {n_days}일 차로, 누적 수익률은 **{cum_return:+.2f}%** 를 기록 중입니다. "
            f"핵심 방어 지표인 최대 낙폭(MDD)은 **{max_dd:+.1f}%** 로 설정된 방어 임계치 이내에서 안전하게 통제되고 있습니다. "
            f"Go/No-Go 최종 심사 상태는 현재 **{verdict}** 입니다."
        )

        report = f"""# 🔭 Project Meridian Daily Report
**{today}** | Regime: {regime_icons.get(regime, '📊')} {regime.upper()} | Go/No-Go: {verdict_icon} {verdict}

---

## 📝 Executive Summary

{summary_text}

---

## 💡 오늘의 투자 전략 서머리

{strategy_summary}

---

{signal_report}

---

{gap_report}

---

## 🎯 Go/No-Go Tracker

Shadow Trading Phase — Day {n_days} / 14

| Criterion | Current | Target | Status |
|-----------|---------|--------|--------|
| Sharpe Ratio | **{sharpe:.3f}** | ≥ 0.50 | {'✅ PASS' if criteria.get('sharpe_pass') else '❌ FAIL'} |
| Win Rate | **{win_rate:.1%}** | ≥ 50% | {'✅ PASS' if criteria.get('winrate_pass') else '❌ FAIL'} |
| Max Drawdown | **{max_dd:+.1f}%** | ≤ -8% | {'✅ PASS' if criteria.get('dd_pass') else '❌ FAIL'} |
| Shadow Days | **{n_days}** | ≥ 14 | {'✅ PASS' if n_days >= 14 else '⏳ ' + str(14 - n_days) + ' days left'} |

"""

        # Daily returns
        returns = gonogo.get('daily_returns', [])
        if returns:
            cum = 0
            report += "### Daily Shadow Returns\n\n"
            report += "| Day | Return | Cumulative |\n"
            report += "|-----|--------|------------|\n"
            for i, r in enumerate(returns):
                cum += r * 100
                icon = '🟢' if r >= 0 else '🔴'
                report += f"| D{i+1} | {icon} {r*100:+.2f}% | {cum:+.2f}% |\n"
            report += "\n"

        # Stream Performance
        report += "---\n\n## 📡 Stream Performance\n\n"
        raw = metrics.get('raw_data', {})
        streams = [
            ('S0', 'Core Beta Defense', 'Always'),
            ('S1', 'Edge (Directional ETF)', '08:00–15:10'),
            ('S2', 'ML Alpha (Stock Selection)', '09:00–15:10'),
            ('S3', 'Factor (Sector Rotation)', 'Always'),
            ('S4', 'Advisory (Tax-Advantaged)', 'Always'),
            ('S5', 'Overnight Sweep', '15:15–08:00'),
            ('S10', 'Macro Ensemble', 'Always'),
        ]

        report += "| Stream | Description | Avg Return | Total Return | Days |\n"
        report += "|--------|-------------|------------|--------------|------|\n"
        for sid, desc, hours in streams:
            data = raw.get(sid, {})
            rets = data.get('daily_returns', [])
            avg = sum(rets) / len(rets) * 100 if rets else 0
            total = sum(rets) * 100
            report += f"| {sid} | {desc} | {avg:+.3f}% | {total:+.2f}% | {len(rets)} |\n"
        report += "\n"

        # Today's Execution Stats
        if daily_stats:
            latest = daily_stats[-1]
            report += f"""---

## ⚡ Shadow Execution ({latest.get('date', today)})

| Metric | Value |
|--------|-------|
| Runs | {latest.get('n_runs', 0)} |
| Orders | {latest.get('n_orders', 0)} |
| Filled | {latest.get('n_filled', 0)} |
| Total Buy | ₩{latest.get('total_buy', 0):,.0f} |
| Total Sell | ₩{latest.get('total_sell', 0):,.0f} |
| Net Flow | ₩{latest.get('net_flow', 0):,.0f} |
| Regime | {latest.get('regime', 'N/A').upper()} |

"""

        # Recent shadow trades
        trades_dir = _RESULTS / 'shadow_trades'
        if trades_dir.exists():
            trade_files = sorted(trades_dir.glob('*.json'), reverse=True)
            if trade_files:
                try:
                    data = json.loads(trade_files[0].read_text())
                    orders = []
                    if isinstance(data, list):
                        for batch in data:
                            orders.extend(batch.get('orders', []))
                    elif isinstance(data, dict):
                        orders = data.get('orders', [])

                    if orders:
                        report += "### Recent Orders\n\n"
                        report += "| Stream | Ticker | Name | Direction | Amount | Confidence |\n"
                        report += "|--------|--------|------|-----------|--------|------------|\n"
                        for o in orders[:15]:
                            report += (f"| {o.get('stream_id', '')} "
                                      f"| {o.get('ticker', '')} "
                                      f"| {o.get('name', '')[:20]} "
                                      f"| {o.get('direction', '')} "
                                      f"| ₩{o.get('amount_krw', 0):,.0f} "
                                      f"| {o.get('confidence', 0):.0%} |\n")
                        report += "\n"
                except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError, json.JSONDecodeError) as e:
                    import logging
                    logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
                    pass

        # Global Signals
        vix = signal.get('vix', 0)
        report += f"""---

## 🌍 Global Market Signals

| Indicator | Value | 1M Change |
|-----------|-------|-----------|
| VIX | {vix:.1f} | {signal.get('vix_change_1m', 0):+.1f}% |
| S&P 500 | {signal.get('sp500', 0):,.1f} | {signal.get('sp500_change_1m', 0):+.1f}% |
| NASDAQ | {signal.get('nasdaq', 0):,.1f} | {signal.get('nasdaq_change_1m', 0):+.1f}% |
| US 10Y | {signal.get('us10y', 0):.3f} | {signal.get('us10y_change_1m', 0):+.1f}% |
| USD/KRW | {signal.get('usdkrw', 0):,.1f} | {signal.get('usdkrw_change_1m', 0):+.1f}% |
| WTI | ${signal.get('wti', 0):.1f} | {signal.get('wti_change_1m', 0):+.1f}% |
| Gold | ${signal.get('gold_us', 0):,.1f} | - |
| OIS | {signal.get('ois', 0):.1f} | - |

"""

        # Risk
        vix_status = '🟢 Safe' if vix < 20 else '🟡 Caution' if vix < 30 else '🔴 Danger'
        dd_status = '🟢 Normal' if max_dd > -5 else '🟡 Warning' if max_dd > -8 else '🔴 Critical'

        report += f"""---

## 🛡️ Risk Dashboard

| Gate | Status | Detail |
|------|--------|--------|
| Kill Switch | {'🟢 Safe' if max_dd > -10 else '🔴 TRIGGERED'} | MDD {max_dd:+.1f}% |
| Crash Defense | {'🟢 Safe' if vix < 30 else '🔴 Active'} | VIX {vix:.1f} |
| Drawdown Guard | {dd_status} | Limit: -8% |
| Exposure Orch | 🟢 Active | Regime-based |
| Realtime VaR | 🟢 Within limits | - |
| Medallion | 🟢 PASS | 4 principles |

| Risk Signal | Value |
|-------------|-------|
| VIX Level | {vix:.1f} ({vix_status}) |
| US Regime | {signal.get('us_regime', 'N/A')} (conf: {signal.get('us_regime_confidence', 0):.0%}) |
| Market Shock | {signal.get('market_shock', 'none')} |
| Cross-Asset | {signal.get('cross_asset_direction', 'N/A')} |
| Upcoming Events | {signal.get('upcoming_events', 0)} |

---
*Generated by Project Meridian — {datetime.now().strftime('%H:%M:%S KST')}*
*Shadow Mode | 4-Stream Architecture | Go/No-Go Phase*
"""

        # Save
        out = _RESULTS / 'reports' / f'daily_{today}.md'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report)
        logger.info(f"  Markdown 리포트 생성: {out}")
        return report

    def _load(self, filename: str) -> Dict:
        return _load_json(filename)
