import sys
import argparse
import logging
from pathlib import Path
from datetime import datetime
import pandas as pd
import smtplib
from email.message import EmailMessage
import os

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.portfolio.corp_ledger import CorpLedgerManager
from src.utils.credential_manager import CredentialManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

def send_email(subject, body, attachment_paths):
    cm = CredentialManager()
    email_user = cm.read_from_env('EMAIL_USER') or os.environ.get('EMAIL_USER')
    email_pass = cm.read_from_env('EMAIL_PASSWORD') or os.environ.get('EMAIL_PASSWORD')
    
    if not email_user or not email_pass:
        logging.warning("⚠️ 이메일 인증 정보(EMAIL_USER/EMAIL_PASSWORD)가 없어 메일 발송을 생략합니다.")
        return
        
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = email_user
    msg['To'] = email_user # 자신(대표님)에게 보냄
    msg.set_content(body)
    
    for path in attachment_paths:
        if path and Path(path).exists():
            with open(path, 'rb') as f:
                msg.add_attachment(f.read(), maintype='application', subtype='octet-stream', filename=Path(path).name)
                
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(email_user, email_pass)
            smtp.send_message(msg)
        logging.info("✅ 장부 엑셀 이메일 발송 완료!")
    except Exception as e:
        logging.error(f"❌ 이메일 발송 실패: {e}")

def main():
    parser = argparse.ArgumentParser(description="법인 장부(원장 및 일계표) 자동 생성 스크립트")
    parser.add_argument('--month', type=str, help="생성할 대상 월 (형식: YYYY-MM). 미지정시 현재 월.", default=None)
    args = parser.parse_args()

    target_month = args.month
    if not target_month:
        target_month = datetime.now().strftime("%Y-%m")

    logging.info(f"🚀 법인 장부 생성 시작: 대상 월 {target_month}")
    
    manager = CorpLedgerManager()
    
    # 1. 거래 원장 생성
    trades_csv = manager.generate_trade_journal(target_month)
    
    # 2. 일계표 생성
    summary_csv = manager.generate_daily_summary(target_month)
    
    attachments = []
    
    # CSV를 엑셀로 병합 (openpyxl 필요)
    excel_path = manager.output_dir / f"Meridian_Ledger_{target_month.replace('-', '')}.xlsx"
    try:
        with pd.ExcelWriter(excel_path) as writer:
            if trades_csv and Path(trades_csv).exists():
                pd.read_csv(trades_csv).to_excel(writer, sheet_name='거래원장', index=False)
            if summary_csv and Path(summary_csv).exists():
                pd.read_csv(summary_csv).to_excel(writer, sheet_name='일계표', index=False)
        attachments.append(excel_path)
        logging.info(f"✔️ 엑셀 장부 완료: {excel_path}")
    except Exception as e:
        logging.warning(f"⚠️ 엑셀 변환 실패 (openpyxl 미설치 가능성): {e}")
        if trades_csv: attachments.append(trades_csv)
        if summary_csv: attachments.append(summary_csv)
        
    if attachments:
        subject = f"[Project Meridian] {target_month} 주간 거래 원장 및 일계표"
        body = f"안녕하세요.\n\nProject Meridian {target_month} 주간 결산 엑셀 파일입니다.\n첨부파일을 확인해주세요.\n\n감사합니다."
        send_email(subject, body, attachments)

if __name__ == "__main__":
    main()

