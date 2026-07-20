import re
with open('dashboard/app.py', 'r') as f:
    code = f.read()

# Remove the duplicated _render_account_tab inside page_s2
bad_block = """    def _render_account_tab(acct_label, acct_desc, risk_limit, tax_benefit, annual_deduction,
                            strategy_notes, live_holdings=None):
        \"\"\"Render a single account tab showing holdings from shadow_portfolio.\"\"\"
        is_actual_portfolio = True
        st.subheader(f"{acct_label} — {acct_desc}")
"""
code = code.replace(bad_block, "")

# Find the real _render_account_tab
real_sig = """def _render_account_tab(acct_label, acct_desc, risk_limit, tax_benefit, annual_deduction,
                        strategy_notes, live_holdings=None):
    \"\"\"Render a single account tab showing holdings from shadow_portfolio.\"\"\"
    st.subheader(f"{acct_label} — {acct_desc}")"""

fixed_sig = """def _render_account_tab(acct_label, acct_desc, risk_limit, tax_benefit, annual_deduction,
                        strategy_notes, live_holdings=None):
    \"\"\"Render a single account tab showing holdings from shadow_portfolio.\"\"\"
    is_actual_portfolio = True
    st.subheader(f"{acct_label} — {acct_desc}")"""

code = code.replace(real_sig, fixed_sig)

with open('dashboard/app.py', 'w') as f:
    f.write(code)
print("Fixed app.py")
