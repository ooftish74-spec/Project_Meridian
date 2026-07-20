#!/usr/bin/env python3
"""
Supply Chain Knowledge Graph (Moonshot 4)
=========================================

DART 공시 및 뉴스 데이터로부터 기업 간 공급망 관계(고객사, 납품사, 경쟁사)망을 구축하고, 
테마/섹터 모멘텀 발생 시 '숨겨진 수혜주'를 찾기 위한 중심성(PageRank) 피처를 생성합니다.

사전 요구사항: `pip install networkx`
"""

import logging
import networkx as nx
import pandas as pd
from typing import Dict, List, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)

class SupplyChainGraph:
    def __init__(self):
        self.graph = nx.DiGraph()
        
    def build_from_dummy(self):
        """
        테스트용 공급망 지식 그래프를 구성합니다.
        실제 환경에서는 DART NLP 파싱 결과에서 엣지를 추출합니다.
        """
        # (Supplier, Customer, Weight/Importance)
        edges = [
            # IT / Semiconductor
            ('000660', 'NVDA', 0.9),  # SK하이닉스 -> 엔비디아 (HBM)
            ('042700', '000660', 0.8), # 한미반도체 -> SK하이닉스 (TC 본더)
            ('058470', '005930', 0.7), # 리노공업 -> 삼성전자
            ('357780', '005930', 0.6), # 솔브레인 -> 삼성전자
            ('005930', 'AAPL', 0.5),   # 삼성전자 -> 애플
            
            # Auto
            ('012330', '005380', 0.9), # 현대모비스 -> 현대자동차
            ('012330', '000270', 0.8), # 현대모비스 -> 기아
            ('006400', 'TSLA', 0.4),   # 삼성SDI -> 테슬라
        ]
        
        for src, dst, weight in edges:
            self.graph.add_edge(src, dst, weight=weight)
            
        logger.info(f"지식 그래프 생성 완료: 노드 {self.graph.number_of_nodes()}개, 엣지 {self.graph.number_of_edges()}개")

    def calculate_centrality_features(self) -> pd.DataFrame:
        """
        Graph Neural Network/네트워크 분석을 통한 노드 중심성 피처 계산.
        """
        if self.graph.number_of_nodes() == 0:
            return pd.DataFrame()
            
        # 1. PageRank (수혜 강도)
        pagerank = nx.pagerank(self.graph, weight='weight')
        
        # 2. In-Degree Centrality (얼마나 많은 벤더를 거느리고 있는가 / 고객사로서의 힘)
        in_degree = nx.in_degree_centrality(self.graph)
        
        # 3. Out-Degree Centrality (얼마나 많은 고객사에 납품하는가 / 공급망 병목 지점)
        out_degree = nx.out_degree_centrality(self.graph)
        
        df = pd.DataFrame({
            'pagerank': pd.Series(pagerank),
            'in_degree': pd.Series(in_degree),
            'out_degree': pd.Series(out_degree),
        })
        
        return df

    def get_ripple_effect(self, shocked_ticker: str, depth: int = 2) -> Dict[str, float]:
        """
        특정 종목(예: 엔비디아)에 호재/악재가 발생했을 때 파급되는 종목과 강도를 계산.
        
        Args:
            shocked_ticker: 쇼크가 발생한 티커 (예: 'NVDA')
            depth: 파급을 추적할 그래프 깊이
            
        Returns:
            수혜/피해 강도를 갖는 딕셔너리 {ticker: impact_score}
        """
        if shocked_ticker not in self.graph:
            return {}
            
        impacts = {}
        # shocked_ticker로 들어오는(In) 엣지들은 납품사들이므로 긍정적 파급 효과를 받음
        # BFS를 통해 전파
        queue = [(shocked_ticker, 1.0, 0)]
        
        while queue:
            current, current_impact, current_depth = queue.pop(0)
            
            if current_depth >= depth:
                continue
                
            for predecessor in self.graph.predecessors(current):
                edge_weight = self.graph[predecessor][current].get('weight', 0.5)
                # 파급 강도 감쇠 (Attenuation)
                transferred_impact = current_impact * edge_weight * 0.8 
                
                if predecessor not in impacts or transferred_impact > impacts[predecessor]:
                    impacts[predecessor] = round(transferred_impact, 4)
                    queue.append((predecessor, transferred_impact, current_depth + 1))
                    
        return impacts
