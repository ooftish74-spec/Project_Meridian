from src.data.market_data_bridge import MarketDataBridge
import logging

logging.basicConfig(level=logging.DEBUG)

bridge = MarketDataBridge()
print("Fetching VKOSPI...")
try:
    vkospi = bridge._get_vkospi()
    print("VKOSPI:", vkospi)
except Exception as e:
    print("VKOSPI Error:", e)

print("\nFetching USDKRW...")
try:
    usdkrw = bridge._get_usdkrw()
    print("USDKRW:", usdkrw)
except Exception as e:
    print("USDKRW Error:", e)

