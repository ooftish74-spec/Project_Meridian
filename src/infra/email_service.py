import os
import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path

from src.utils.credential_manager import CredentialManager

logger = logging.getLogger(__name__)

class EmailService:
    """Service to send automated reports via Email (SMTP)."""
    
    def __init__(self):
        cm = CredentialManager()
        
        self.sender_email = cm.read_from_keychain('EMAIL_SENDER')
        self.sender_password = cm.read_from_keychain('EMAIL_PASSWORD')
        self.recipient_email = cm.read_from_keychain('REPORT_RECIPIENT')
        self.smtp_server = cm.read_from_keychain('SMTP_SERVER') or 'smtp.gmail.com'
        self.smtp_port = int(cm.read_from_keychain('SMTP_PORT') or 587)
        
        self.is_configured = all([self.sender_email, self.sender_password, self.recipient_email])
        
    def send_report(self, subject: str, body_text: str, pdf_path: str = None) -> bool:
        """
        Sends an email with an optional PDF attachment.
        """
        if not self.is_configured:
            logger.error("❌ EmailService is not fully configured. Please set EMAIL_SENDER, EMAIL_PASSWORD, and REPORT_RECIPIENT in .env")
            return False
            
        try:
            msg = MIMEMultipart()
            msg['From'] = self.sender_email
            msg['To'] = self.recipient_email
            msg['Subject'] = subject
            
            # Attach body text
            msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
            
            # Attach PDF if provided
            if pdf_path:
                pdf_file = Path(pdf_path)
                if pdf_file.exists():
                    with open(pdf_file, 'rb') as f:
                        part = MIMEApplication(f.read(), Name=pdf_file.name)
                        part['Content-Disposition'] = f'attachment; filename="{pdf_file.name}"'
                        msg.attach(part)
                else:
                    logger.warning(f"⚠️ PDF attachment not found at: {pdf_path}")
            
            # Send Email
            server = smtplib.SMTP(self.smtp_server, self.smtp_port)
            server.starttls()
            server.login(self.sender_email, self.sender_password)
            server.send_message(msg)
            server.quit()
            
            logger.info(f"✅ Email successfully sent to {self.recipient_email}: {subject}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to send email: {e}")
            return False
