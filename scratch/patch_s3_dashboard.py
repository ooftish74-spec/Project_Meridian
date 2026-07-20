import sys

target = "    st.dataframe(pd.DataFrame(factor_map), hide_index=True, width=\"stretch\")"
replacement = """    st.dataframe(pd.DataFrame(factor_map), hide_index=True, width="stretch")

    st.markdown("---")
    st.subheader("💼 S3 Holdings (Track A & Track B)")
    
    # Render S3 holdings directly from shadow_portfolio
    shadow = load_json('shadow_portfolio.json')
    s3_pos = [p for p in shadow.get('positions', {}).values() if p.get('stream_id') == 'S3']
    
    if not s3_pos:
        st.info("현재 보유 중인 S3 포지션이 없습니다.")
    else:
        df_s3 = pd.DataFrame(s3_pos)
        
        # Track A vs Track B 분리
        track_b_qvm = df_s3[df_s3['strategy'] == 'qvm_value']
        track_a_etf = df_s3[df_s3['strategy'] != 'qvm_value']
        
        st.markdown("#### 🎯 Track B: QVM Value (개별주)")
        if not track_b_qvm.empty:
            disp_qvm = track_b_qvm[['ticker', 'name', 'quantity', 'avg_price', 'current_price', 'pnl_pct', 'amount']].copy()
            disp_qvm['pnl_pct'] = disp_qvm['pnl_pct'].apply(lambda x: f"{x:.2f}%")
            disp_qvm['amount'] = disp_qvm['amount'].apply(lambda x: f"₩{x:,.0f}")
            disp_qvm['avg_price'] = disp_qvm['avg_price'].apply(lambda x: f"₩{x:,.0f}")
            disp_qvm['current_price'] = disp_qvm['current_price'].apply(lambda x: f"₩{x:,.0f}")
            st.dataframe(disp_qvm, hide_index=True, width="stretch")
        else:
            st.caption("현재 QVM 편입 종목이 없습니다.")
            
        st.markdown("#### 📈 Track A: Factor ETF")
        if not track_a_etf.empty:
            disp_etf = track_a_etf[['ticker', 'name', 'quantity', 'avg_price', 'current_price', 'pnl_pct', 'amount']].copy()
            disp_etf['pnl_pct'] = disp_etf['pnl_pct'].apply(lambda x: f"{x:.2f}%")
            disp_etf['amount'] = disp_etf['amount'].apply(lambda x: f"₩{x:,.0f}")
            disp_etf['avg_price'] = disp_etf['avg_price'].apply(lambda x: f"₩{x:,.0f}")
            disp_etf['current_price'] = disp_etf['current_price'].apply(lambda x: f"₩{x:,.0f}")
            st.dataframe(disp_etf, hide_index=True, width="stretch")
        else:
            st.caption("현재 ETF 편입 종목이 없습니다.")
"""

with open("dashboard/app.py", "r") as f:
    content = f.read()

if target in content:
    content = content.replace(target, replacement)
    with open("dashboard/app.py", "w") as f:
        f.write(content)
    print("S3 Dashboard patched.")
else:
    print("Target string not found in app.py!")

