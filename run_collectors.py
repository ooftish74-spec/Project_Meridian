import logging
logging.basicConfig(level=logging.INFO)

from src.data_collection.investor_flow_collector import InvestorFlowCollector
print("Running InvestorFlowCollector...")
flow = InvestorFlowCollector()
flow.collect_daily()
flow.update_short_proxy()

from src.data_collection.naver_news_sentiment import NaverNewsSentiment
print("Running NaverNewsSentiment...")
nns = NaverNewsSentiment()
if nns.is_available:
    nns.collect_all(max_pages=1)
else:
    print("Naver API not available")
