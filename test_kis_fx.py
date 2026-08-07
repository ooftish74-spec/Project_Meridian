import sys
from pathlib import Path
sys.path.append('/home/ubuntu/Project_Meridian')
from src.data_collection.kis_data_collector import KISDataCollector

kis = KISDataCollector()

try:
    # Try CTCA0903R
    res = kis._call(
        url=f"{kis._base_url}/uapi/overseas-stock/v1/trading/inquire-present-balance",
        tr_id="CTCA0903R",
        params={
            "CANO": kis.cano,
            "ACNT_PRDT_CD": kis.acnt_prdt_cd,
            "WCRC_FRCR_DVSN_CD": "01",
            "NATN_CD": "840", # USD
            "TR_MKTC_CD": "00",
            "INQR_DVSN_CD": "00"
        }
    )
    print("CTCA0903R:", res)
except Exception as e:
    print(e)
