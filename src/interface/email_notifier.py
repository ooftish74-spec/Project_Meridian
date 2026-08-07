"""
Project Meridian — Email Notifier
=======================================
PDF 레포트를 이메일로 자동 전송하는 모듈입니다.

Usage:
    from src.interface.email_notifier import MeridianEmail
    email = MeridianEmail()
    email.send_report('report.pdf', 'Meridian Daily Report')
"""
import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from pathlib import Path
logger = logging.getLogger(__name__)

class MeridianEmail:
    """Meridian 이메일 알림 클래스.
    DynamicConfig 우선, .env fallback으로 설정 로드.
    """

    def __init__(self):
        self._cfg = None
        try:
            from config.dynamic_config import DynamicConfig
            self._cfg = DynamicConfig()
        except (FileNotFoundError, ValueError, KeyError, TypeError, ImportError) as e:
            import logging
            logging.getLogger(__name__).debug(f'Targeted fallback: {e}')
            pass
        cm = None
        try:
            from src.utils.credential_manager import CredentialManager
            cm = CredentialManager()
        except ImportError as e:
            from src.utils.error_logger import log_error_rate_limited
            log_error_rate_limited(__name__, f'🚨 [Silent Bypass 감지] 치명적 예외 발생: {e}', exc_info=True)

        def get_env_or_keychain(key: str, default: str='') -> str:
            cfg_val = self._cfg.get(f'email.{key.lower()}', '') if self._cfg else ''
            if cfg_val:
                return cfg_val
            if cm:
                kc_val = cm.read_from_env(key)
                if kc_val:
                    return kc_val
            return default
        self.smtp_server = get_env_or_keychain('SMTP_SERVER', 'smtp.gmail.com')
        self.smtp_port = int(get_env_or_keychain('SMTP_PORT', '587'))
        self.sender_email = get_env_or_keychain('EMAIL_SENDER', '')
        self.sender_password = get_env_or_keychain('EMAIL_PASSWORD', '')
        self.receiver_email = get_env_or_keychain('REPORT_RECIPIENT', '')
        self.enabled = bool(self.sender_email and self.sender_password and self.receiver_email)

    def send_report(self, pdf_path: str, subject: str='Project Meridian Report', body: str='첨부된 리포트를 확인해 주세요.'):
        """PDF 파일을 이메일로 전송합니다."""
        if not self.enabled:
            logger.warning('이메일 설정이 누락되어 전송할 수 없습니다 (.env의 SENDER_EMAIL 등 확인).')
            return False
        if not os.path.exists(pdf_path):
            logger.error(f'첨부할 PDF 파일을 찾을 수 없습니다: {pdf_path}')
            return False
        msg = MIMEMultipart()
        msg['From'] = self.sender_email
        msg['To'] = self.receiver_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        try:
            with open(pdf_path, 'rb') as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename= {os.path.basename(pdf_path)}')
                msg.attach(part)
        except Exception as e:
            logger.error(f'파일 첨부 중 오류 발생: {e}')
            return False
        try:
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.sender_email, self.sender_password)
            server.send_message(msg)
            server.quit()
            logger.info(f'✅ 이메일 전송 성공: {self.receiver_email} (파일: {os.path.basename(pdf_path)})')
            return True
        except Exception as e:
            logger.error(f'이메일 전송 실패: {e}')
            return False

    def send_html_report(self, markdown_content: str, subject: str='Project Meridian Daily Report'):
        """Markdown을 HTML로 변환하여 이메일 본문으로 전송합니다."""
        if not self.enabled:
            logger.warning('이메일 설정이 누락되어 전송할 수 없습니다 (.env의 SENDER_EMAIL 등 확인).')
            return False
        try:
            import markdown
            html_body = markdown.markdown(markdown_content, extensions=['tables', 'fenced_code'])
        except ImportError as e:
            logger.error('markdown 패키지가 설치되지 않았습니다. pip install Markdown', exc_info=True)
            return False
        html_content = f'\n        <html>\n        <head>\n        <style>\n            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; padding: 20px; }}\n            table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; font-size: 14px; }}\n            th, td {{ padding: 8px 12px; border: 1px solid #ddd; text-align: left; }}\n            th {{ background-color: #f8f9fa; font-weight: bold; }}\n            h1, h2, h3 {{ color: #2c3e50; }}\n            hr {{ border: 0; border-top: 1px solid #eee; margin: 20px 0; }}\n        </style>\n        </head>\n        <body>\n        {html_body}\n        </body>\n        </html>\n        '
        msg = MIMEMultipart('alternative')
        msg['From'] = self.sender_email
        msg['To'] = self.receiver_email
        msg['Subject'] = subject
        msg.attach(MIMEText(html_content, 'html'))
        try:
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.sender_email, self.sender_password)
            server.send_message(msg)
            server.quit()
            logger.info(f'✅ HTML 이메일 전송 성공: {self.receiver_email}')
            return True
        except Exception as e:
            logger.error(f'이메일 전송 실패: {e}')
            return False