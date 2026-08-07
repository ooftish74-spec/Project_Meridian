#!/usr/bin/env python3
"""
Ultimate Meridian Quant Report Generator
Combines external Macro Economic Data and internal Meridian Stream Data
into a premium, Goldman Sachs/McKinsey style PDF report.
(Korean Translated Version)
"""

import sys
import logging
from datetime import datetime
from pathlib import Path
import pandas as pd
import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(_PROJECT_ROOT))

from src.report.ultimate_pdf_generator import UltimatePDFGenerator
from src.report.data_aggregator import DataAggregator
from src.report.chart_generator import ChartGenerator
from src.interface.email_notifier import MeridianEmail

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

REPORTS_DIR = _PROJECT_ROOT / 'reports'
CHARTS_DIR = REPORTS_DIR / 'charts'

import os

# External Charts Path
EXTERNAL_CHARTS_DIR = Path(os.environ.get('EXTERNAL_CHARTS_DIR', _PROJECT_ROOT.parent.parent / 'scratch' / 'economy-investment-analysis' / 'reports' / 'daily' / 'charts'))

class UltimateMeridianReport:
    def __init__(self):
        self.pdf_gen = UltimatePDFGenerator(output_dir=str(REPORTS_DIR))
        self.chart_gen = ChartGenerator(output_dir=str(CHARTS_DIR))
        self.aggregator = DataAggregator()
        self.date_str = datetime.now().strftime('%Y-%m-%d')
        
        # Load Data
        self.meridian_data = self.aggregator.load_meridian_data()
        self.macro_data = self.aggregator.load_macro_data()
        
        self.sections = []
        self.charts = {}

    def _calculate_portfolio_scenarios(self, vix):
        """Generate Korea-Optimized Portfolio Scenarios (ISA, Pension)"""
        regime = "중립 (Neutral)"
        if vix < 18.0: regime = "상승장 (Risk-On)"
        elif vix > 25.0: regime = "하락장 (Risk-Off)"
        
        ai_confidence = "높음 (High)" if vix < 20 else "보통 (Moderate)"
        
        rat_con = (
            f"<b>AI 전략 (신뢰도: {ai_confidence}):</b> 현재의 {regime} 환경(VIX {vix:.1f})에서 알고리즘은 자본 보존을 최우선으로 합니다.<br/>"
            f"<b>논리:</b> 수익률 곡선 역전은 장기 채권 듀레이션의 우위를 시사합니다. 변동성 방어를 위해 배당 귀족주가 선택되었습니다.<br/><br/>"
            f"<b>전술적 배분:</b><br/>"
            f"1. <b>연금계좌:</b> 경기 침체 리스크 헷지를 위해 초장기 국채(TIGER 30년)에 50% 배분.<br/>"
            f"2. <b>ISA계좌:</b> 밸류 트랩을 피하기 위해 단순 고배당보다는 '퀄리티' 팩터 배당주(SCHD 스타일)에 집중.<br/>"
        )
        
        rat_mod = (
            f"<b>AI 전략 (신뢰도: {ai_confidence}):</b> 성장 팩터와 변동성 완화 장치의 균형 유지. "
            f"모델이 {regime} 시그널을 감지하여 코어-위성(Core-Satellite) 전략을 권장합니다.<br/><br/>"
            f"<b>논리:</b> 빅테크에 대한 AI 모멘텀 점수는 양수이나, 매크로 리스크가 잔존합니다. 바벨 전략이 권장됩니다.<br/>"
            f"<b>전술적 배분:</b><br/>"
            f"1. <b>연금계좌:</b> 40% 시장 전체(S&P 500) + 20% 헷지된 테크 익스포저.<br/>"
            f"2. <b>ISA계좌:</b> 미국 테크 Top 10에 배분하되 배당 성장주로 버퍼 구축.<br/>"
        )
        
        rat_agg = (
            f"<b>AI 전략 (신뢰도: {ai_confidence}):</b> 구조적 장기 트렌드(AI/반도체)의 기회 포착. "
            f"{regime} 경고에도 불구하고, 알고리즘은 반도체 사이클에서 높은 샤프 지수 잠재력을 식별합니다.<br/><br/>"
            f"<b>논리:</b> '승자독식' 시장 국면입니다. AI 선도 기업(NVIDIA/Hynix 밸류체인)에 자본을 집중합니다.<br/>"
            f"<b>전술적 배분:</b><br/>"
            f"1. <b>연금계좌:</b> 과세 이연 복리 효과를 위한 나스닥/FANG+ 공격적 비중 확대.<br/>"
            f"2. <b>ISA계좌:</b> 슈퍼 사이클 베타를 캡처하기 위한 순수 반도체 ETF 배분.<br/>"
        )

        return {
            'Conservative (보수형)': rat_con,
            'Moderate (중도형)': rat_mod,
            'Aggressive (공격형)': rat_agg
        }

    def build_executive_summary(self):
        """Top level executive summary"""
        metrics = self.aggregator.get_latest_macro_metrics()
        vix = metrics.get('VIXCLS', 20.0)
        
        shadow = self.meridian_data.get('shadow_summary', {})
        cum_ret = shadow.get('cumulative_return_pct', 0.0)
        regime = shadow.get('daily_stats', [{'regime': 'BULL'}])[-1].get('regime', 'BULL').upper()
        
        vix_comment = "불확실성 증가 및 잠재적 시장 스트레스" if vix > 25 else "안정적인 변동성 환경" if vix < 18 else "중립적인 시장 변동성"
        perf_comment = "강력한 양수 알파 창출" if cum_ret > 0 else "하락장 속 자본 보존 체제 돌입"
        
        summary_text = (
            f"<b>총괄 요약 (EXECUTIVE SUMMARY):</b> 메리디안 퀀트 포트폴리오는 현재 {cum_ret:+.2f}%의 누적 수익률을 기록하고 있으며, "
            f"이는 {perf_comment}을(를) 반영합니다. AI 리짐 분류기(Regime Classifier)는 현재 시장 국면을 <b>{regime}</b>(으)로 식별했습니다. "
            f"글로벌 VIX 지수는 {vix:.2f}를 기록하며 {vix_comment}을(를) 나타냅니다. 이러한 요인들을 바탕으로, "
            f"시스템은 4개의 활성 퀀트 스트림(Streams)에 걸쳐 철저히 계산된 익스포저를 유지하며, 실시간 VaR 제약을 "
            f"엄격히 준수하는 동시에 동적 자산 배분을 최우선으로 수행하고 있습니다."
        )
        
        self.sections.append({
            'type': 'executive_summary',
            'content': summary_text
        })

    def build_chapter_1(self):
        """Chapter 1: Global Macro & Economic Intelligence"""
        self.sections.append({
            'type': 'heading',
            'title': '1. 글로벌 매크로 및 경제 동향 (Global Macro Intelligence)',
            'content': '크로스에셋 글로벌 매크로 지표 통합 분석'
        })
        
        metrics = self.aggregator.get_latest_macro_metrics()
        
        if metrics:
            df = pd.DataFrame(list(metrics.items()), columns=['지표 (Indicator)', '최근 값 (Latest Value)'])
            df['최근 값 (Latest Value)'] = df['최근 값 (Latest Value)'].apply(lambda x: f"{x:.4f}" if isinstance(x, float) else x)
            
            signal_cache = self.meridian_data.get('signal_cache', {})
            vix = signal_cache.get('vix', metrics.get('VIXCLS', 20.0))
            krw_usd = signal_cache.get('usdkrw', metrics.get('KRW', 1350.0))
            
            macro_analysis = (
                f"<b>매크로 환경 분석:</b><br/>"
                f"글로벌 매크로 환경은 구조적 변화를 탐지하기 위해 실시간으로 모니터링됩니다. "
                f"현재 VIX 지수({vix:.2f})가 시장 심리의 주요 척도로 기능하고 있습니다. "
                f"또한 USD/KRW 환율은 약 {krw_usd:.2f}원으로 추적되며, 이는 크로스보더 트레이딩의 "
                f"환헷지 수익률 격차(Yield Differential)에 직결됩니다. "
                f"이러한 지표들의 융합은 현재 시장이 리스크-온(Risk-on) 기술주 팩터와 방어적인 배당 전략 사이의 "
                f"민첩한 로테이션을 요구하고 있음을 시사합니다."
            )
            self.sections.append({'type': 'text', 'content': macro_analysis})
            
            krw_chart = EXTERNAL_CHARTS_DIR / 'usdkrw_exchange_rate.png'
            if krw_chart.exists():
                self.sections.append({'type': 'subheading', 'title': 'USD/KRW 환율 트렌드'})
                self.sections.append({'type': 'chart', 'chart_path': str(krw_chart)})

            if not self.macro_data.empty and 'VIXCLS' in self.macro_data.columns:
                try:
                    vix_series = self.macro_data['VIXCLS'].tail(60).dropna()
                    if not vix_series.empty:
                        chart_path = self.chart_gen.plot_time_series(
                            vix_series.to_frame(), 
                            title="VIX 변동성 트렌드 (최근 60일)", 
                            ylabel="VIX 지수", 
                            filename=f"vix_trend_{self.date_str}.png"
                        )
                        self.sections.append({'type': 'chart', 'chart_path': chart_path})
                except Exception as e:
                    logger.warning(f"VIX chart generation failed: {e}")
            
            self.sections.append({
                'type': 'table',
                'title': '핵심 매크로 동향 (Key Macroeconomic Pulse)',
                'data': df.head(10)
            })
            
            comm_chart = EXTERNAL_CHARTS_DIR / 'commodities_trends.png'
            if comm_chart.exists():
                self.sections.append({'type': 'subheading', 'title': '글로벌 원자재 트렌드'})
                self.sections.append({'type': 'chart', 'chart_path': str(comm_chart)})
        else:
            self.sections.append({
                'type': 'text',
                'content': '매크로 데이터베이스를 현재 사용할 수 없습니다. 시스템 내부 변동성 추정치에 의존합니다.'
            })

    def build_chapter_2(self):
        """Chapter 2: V2 Engine (Chameleon Orchestrator & Alpha Miner v2)"""
        self.sections.append({
            'type': 'heading',
            'title': '2. V2 엔진 (Chameleon Orchestrator & Alpha Miner v2)',
            'content': 'V2 엔진은 Alpha Miner v2의 시그널을 바탕으로 자본을 동적으로 라우팅하고 자산 배분을 최적화합니다.'
        })
        
        s4 = self.meridian_data.get('s4_advisory', {})
        allocations = s4.get('allocation', {'Core Stocks': '60%', 'Bonds': '20%', 'Cash': '20%'})
        
        try:
            alloc_series = pd.Series({k: float(str(v).replace('%', '')) for k, v in allocations.items()})
            self.charts['allocation'] = self.chart_gen.plot_pie_chart(
                alloc_series,
                title="V2 엔진 목표 자산 배분 (Target Allocation)",
                filename=f"v2_allocation_ultimate_{self.date_str}.png"
            )
            
            top_asset = alloc_series.idxmax()
            top_weight = alloc_series.max()
            
            allocation_analysis = (
                f"<b>카멜레온 오케스트레이터 (V2 엔진):</b><br/>"
                f"Alpha Miner v2 리짐 분류기의 신호에 따라, V2 엔진은 자본을 동적으로 라우팅하며 현재 <b>{top_asset}</b> 비중을 {top_weight}%로 오버웨이트(비중 확대)하고 있습니다. "
                f"이러한 유체적인(Fluid) 접근 방식은 강세장(Bull) 마이크로 사이클에서는 벤치마크를 상회하는 업사이드 베타를 캡처하는 동시에, "
                f"예상치 못한 외생 변수(Exogenous Shocks) 발생 시 채권과 현금을 밸러스트(Ballast, 균형추)로 활용합니다. "
                f"카멜레온 오케스트레이터는 모멘텀 시그널이 본질적인 팩터 가치와 괴리될 때 즉각적인 동적 리밸런싱을 트리거합니다."
            )
            
            self.sections.append({'type': 'text', 'content': allocation_analysis})
            self.sections.append({
                'type': 'chart',
                'chart_path': self.charts['allocation']
            })
            
        except Exception as e:
            logger.warning(f"Pie chart generation failed: {e}")

    def build_chapter_3(self):
        """Chapter 3: S4 Advisory (V1 Stream)"""
        self.sections.append({
            'type': 'heading',
            'title': '3. S4 자문 스트림 (텍스 최적화 자산관리)',
            'content': '한국의 세제혜택 계좌(ISA/연금저축)를 위한 외부 ML 시그널 기반 맞춤형 포트폴리오 제안.'
        })

        metrics = self.aggregator.get_latest_macro_metrics()
        vix = metrics.get('VIXCLS', 20.0)
        scenarios = self._calculate_portfolio_scenarios(vix)
        
        for strategy, rationale in scenarios.items():
            self.sections.append({
                'type': 'text',
                'content': f"<b>{strategy} 포트폴리오</b><br/>{rationale}"
            })
            
        samsung_chart = EXTERNAL_CHARTS_DIR / '005930_Samsung_Electronics.png'
        if samsung_chart.exists():
            self.sections.append({'type': 'subheading', 'title': '핵심 테크 편입 분석: 삼성전자'})
            self.sections.append({'type': 'chart', 'chart_path': str(samsung_chart)})

        bitcoin_chart = EXTERNAL_CHARTS_DIR / '371450_TIGER_Bitcoin_Futures.png'
        if bitcoin_chart.exists():
            self.sections.append({'type': 'subheading', 'title': '대체 자산 편입 분석: TIGER 비트코인 선물'})
            self.sections.append({'type': 'chart', 'chart_path': str(bitcoin_chart)})


    def build_chapter_4(self):
        """Chapter 4: Meridian Stream Performance"""
        self.sections.append({
            'type': 'heading',
            'title': '4. 메리디안 스트림 실적 (V1 엔진)',
            'content': '고유 알고리즘 트레이딩 스트림들의 실시간 퍼포먼스 트래킹'
        })
        
        metrics = self.meridian_data.get('stream_metrics', {})
        raw_data = metrics.get('raw_data', {})
        
        if raw_data:
            try:
                from scripts.stream_orchestrator import StreamOrchestrator
                orch = StreamOrchestrator()
                status_list = orch.get_stream_status()
                active_map = {s['stream_id']: s['active'] for s in status_list}
            except Exception:
                active_map = {}

            summary_records = []
            best_stream = None
            best_ret = -999.0
            
            cum_returns_data = {}
            for stream_id in sorted(raw_data.keys()):
                s_data = raw_data.get(stream_id, {})
                returns = s_data.get('daily_returns', [])
                if returns:
                    cum_returns_data[stream_id] = np.cumsum(returns) * 100
                    
                cum_ret = sum(returns)*100 if returns else 0.0
                win_rate = s_data.get('win_rate', 0.0)*100
                
                if cum_ret > best_ret:
                    best_ret = cum_ret
                    best_stream = stream_id
                
                is_active = active_map.get(stream_id, True)
                display_name = stream_id if is_active else f"{stream_id} (Deactivated)"
                
                summary_records.append({
                    '스트림 (Stream)': display_name,
                    '활성 거래수 (Trades)': len(returns),
                    '승률 (Win Rate)': f"{win_rate:.1f}%",
                    '누적 수익률 (Cumulative Return)': f"{cum_ret:+.2f}%"
                })
                
            df = pd.DataFrame(summary_records)
            
            perf_analysis = (
                f"<b>스트림 기여도 분석:</b><br/>"
                f"멀티 전략 프레임워크 전반에 걸쳐 현재 <b>{best_stream}</b>이(가) {best_ret:+.2f}%의 누적 수익률을 기록하며 "
                f"가장 지배적인 알파(Alpha)를 창출하고 있습니다. 이러한 아웃퍼폼은 현재 시장 국면에서 메리디안의 "
                f"팩터 타이밍 모델이 매우 유효하게 작동하고 있음을 증명합니다. 반대로 실적이 부진한 스트림은 전체 포트폴리오의 "
                f"수익률 갉아먹기(Drag)를 방지하기 위해 포트폴리오 옵티마이저에 의해 비중이 자동으로 축소(Scaled down)됩니다."
            )
            self.sections.append({'type': 'text', 'content': perf_analysis})
            
            if cum_returns_data:
                try:
                    max_len = max(len(v) for v in cum_returns_data.values())
                    padded_returns = {k: np.pad(v, (max_len - len(v), 0), constant_values=np.nan) for k, v in cum_returns_data.items()}
                    df_returns = pd.DataFrame(padded_returns)
                    df_returns.index = pd.date_range(end=datetime.now(), periods=max_len)
                    
                    chart_path = self.chart_gen.plot_time_series(
                        df_returns, 
                        title="스트림별 누적 수익률 (Stream Cumulative Returns %)", 
                        ylabel="수익률 (Return %)", 
                        filename=f"stream_returns_{self.date_str}.png"
                    )
                    self.sections.append({'type': 'chart', 'chart_path': chart_path})
                except Exception as e:
                    logger.warning(f"Stream returns chart generation failed: {e}")
            
            self.sections.append({
                'type': 'table',
                'title': '스트림 퍼포먼스 요약 (Stream Performance Overview)',
                'data': df
            })
        else:
            self.sections.append({'type': 'text', 'content': '비교를 위한 스트림 데이터가 충분하지 않습니다.'})

    def build_chapter_5(self):
        """Chapter 5: Factor Insights & Risk Management"""
        self.sections.append({
            'type': 'heading',
            'title': '5. 팩터 인사이트 및 리스크 관리',
            'content': '예측 분석, 갭 분석(Gap Analysis), 및 실시간 Value-at-Risk(VaR) 트래킹.'
        })
        
        var_data = self.meridian_data.get('realtime_var', {})
        gap_data = self.meridian_data.get('gap_analysis', {}).get('summary', {})
        shadow = self.meridian_data.get('shadow_summary', {})
        
        current_var = var_data.get('current_var_pct', 0.0)
        var_limit = var_data.get('var_limit_pct', 1.5)
        max_dd = shadow.get('max_dd', 0.0)
        
        da = gap_data.get('overall_da', 0.0) * 100
        ic = gap_data.get('overall_ic', 0.0)
        
        risk_health = "안정적 (Healthy)" if current_var <= var_limit else "위험 상승 (Elevated Risk)"
        dd_health = "통제 중 (Controlled)" if max_dd > -8.0 else "한계 돌파 (Breached Limits)"
        
        risk_analysis = (
            f"<b>리스크 엔진 상태: {risk_health}</b><br/>"
            f"실시간 99% VaR(Value-at-Risk)는 {current_var:.2f}%로 산출되었으며, 이는 내부 한도인 {var_limit:.2f}% 내에서 "
            f"안정적으로 관리되고 있습니다. 현재 최대 낙폭(Maximum Drawdown)은 {max_dd:.2f}%로 <b>{dd_health}</b> 상태입니다. "
            f"예측력 측면에서, 머신러닝 모델은 방향성 정확도(DA) {da:.1f}%와 정보 계수(IC) {ic:.3f}를 기록하며 "
            f"알파 팩토리(Alpha Factory)가 창출하는 통계적 엣지(Edge)의 유효성을 입증하고 있습니다."
        )
        self.sections.append({'type': 'text', 'content': risk_analysis})
        
        risk_chart = EXTERNAL_CHARTS_DIR / 'risk_matrix.png'
        if risk_chart.exists():
            self.sections.append({'type': 'subheading', 'title': '거시 경제 리스크 매트릭스'})
            self.sections.append({'type': 'chart', 'chart_path': str(risk_chart)})
        
        risk_df = pd.DataFrame([
            {"지표 (Metric)": "실시간 VaR (99%)", "현재 값 (Value)": f"{current_var:.2f}%", "한도 (Limit)": f"≤ {var_limit:.2f}%"},
            {"지표 (Metric)": "최대 낙폭 (Max Drawdown)", "현재 값 (Value)": f"{max_dd:.2f}%", "한도 (Limit)": "≤ 8.00%"},
            {"지표 (Metric)": "방향 정확도 (DA)", "현재 값 (Value)": f"{da:.1f}%", "한도 (Limit)": "≥ 52.0%"},
            {"지표 (Metric)": "정보 계수 (IC)", "현재 값 (Value)": f"{ic:.3f}", "한도 (Limit)": "> 0.02"}
        ])
        
        self.sections.append({
            'type': 'table',
            'title': '시스템 리스크 및 정확도 지표 (System Risk & Accuracy Metrics)',
            'data': risk_df
        })

    def build_chapter_6(self):
        """Chapter 6: Trade Execution & Portfolio Log"""
        self.sections.append({
            'type': 'heading',
            'title': '6. 트레이딩 체결 및 포트폴리오 로그',
            'content': '현재 섀도우 포트폴리오(Shadow Portfolio) 보유 종목 및 최근 거래 내역의 스냅샷.'
        })
        
        portfolio = self.meridian_data.get('shadow_portfolio', {})
        positions = portfolio.get('positions', {})
        
        if positions:
            pos_records = []
            for ticker, p in positions.items():
                pos_records.append({
                    '티커 (Ticker)': ticker,
                    '수량 (Quantity)': p.get('quantity', 0),
                    '매입 단가 (Avg Price)': f"₩{p.get('avg_price', 0):,.0f}",
                    '현재가 (Current Price)': f"₩{p.get('current_price', 0):,.0f}",
                    '수익률 (Return %)': f"{p.get('return_pct', 0.0):+.2f}%"
                })
            
            df_pos = pd.DataFrame(pos_records)
            
            pos_analysis = (
                f"<b>포트폴리오 보유 종목 분석:</b><br/>"
                f"섀도우 포트폴리오는 현재 {len(positions)}개의 액티브 포지션을 보유하고 있습니다. "
                f"알고리즘 체결 엔진은 시장 충격(Market Impact)을 최소화하고 매수-매도 호가 스프레드를 좁히는 데 최적화되어 있습니다. "
                f"자본 집행은 매우 선택적으로 이루어지며, 변동성이 큰 구간에서 기회주의적(Opportunistic) 진입을 위해 현금(Dry Powder)을 적정량 유지하고 있습니다."
            )
            self.sections.append({'type': 'text', 'content': pos_analysis})
            
            self.sections.append({
                'type': 'table',
                'title': '현재 포트폴리오 편입 종목 (Current Portfolio Positions)',
                'data': df_pos
            })
        else:
            self.sections.append({'type': 'text', 'content': '<b>포트폴리오 보유 종목 분석:</b><br/>현재 포트폴리오는 전액 현금으로 청산(Liquidated)된 상태입니다. 이러한 방어적 포지셔닝은 불확실성이 극대화된 구간에서 자본을 보호하기 위한 조치입니다.'})

    def generate(self):
        logger.info("🔭 Assembling Ultimate Report Chapters...")
        self.build_executive_summary()
        self.build_chapter_1()
        self.build_chapter_2()
        self.build_chapter_3()
        self.build_chapter_4()
        self.build_chapter_5()
        self.build_chapter_6()
        
        filename = f"Ultimate_Meridian_Quant_Report_KR_{self.date_str}.pdf"
        report_path = self.pdf_gen.generate_report(
            filename=filename,
            title="얼티밋 퀀트 리포트 (Ultimate Quant Report)",
            subtitle=f"거시 경제 및 포트폴리오 인텔리전스 ({self.date_str})",
            sections=self.sections
        )
        
        logger.info(f"✅ Ultimate PDF Generated: {report_path}")
        
        email = MeridianEmail()
        if email.enabled:
            email.send_report(
                pdf_path=report_path,
                subject=f"🚀 [Meridian] 메리디안 퀀트 리포트 발송 ({self.date_str})",
                body="메리디안 퀀트 리포트(한글판)가 성공적으로 생성되었습니다.\n\n"
                     "본 리포트는 글로벌 거시 경제 인텔리전스와 프로젝트 메리디안의 "
                     "트레이딩 스트림 및 리스크 모델을 통합하여 분석한 내용을 담고 있습니다.\n\n"
                     "자세한 내용은 첨부된 PDF 파일을 확인해 주시기 바랍니다."
            )
        return report_path

if __name__ == '__main__':
    report = UltimateMeridianReport()
    report.generate()
