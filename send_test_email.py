#!/usr/bin/env python
"""Send a test email to celestiatechco@gmail.com using Brevo."""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Credentials
SMTP_HOST = os.getenv("SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_ADDRESS", "a1aa4e001@smtp-brevo.com")
EMAIL_PASS = os.getenv("EMAIL_PASSWORD", "")
FROM_EMAIL = "anirudhpathania384@gmail.com"
FROM_NAME = "Career Vault Platform"
TO_EMAIL = "celestiatechco@gmail.com"

def send_email():
    """Send test email to celestiatechco@gmail.com."""
    print(f"Sending email to {TO_EMAIL}...")
    
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        print(f"✓ Authenticated")
        
        # Create email
        msg = MIMEMultipart()
        msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg["To"] = TO_EMAIL
        msg["Subject"] = "Quick question from Career Vault"
        
        body = """Hi there,

I came across your website and thought you might be interested in our web development and optimization services.

We specialize in:
- Website speed optimization (often 50%+ improvement)
- SSL & security upgrades
- Conversion rate optimization
- SEO improvements

Would you be open to a brief 15-minute call this week to discuss?

Best,
Career Vault Platform
        """.strip()
        
        msg.attach(MIMEText(body, "plain"))
        
        server.send_message(msg)
        print(f"✓ Email sent successfully to {TO_EMAIL}!")
        
        server.quit()
        
    except Exception as e:
        print(f"❌ Error: {e}")
        return False
    
    return True

if __name__ == "__main__":
    if not os.getenv("EMAIL_PASSWORD"):
        print("❌ EMAIL_PASSWORD not set!")
        exit(1)
    
    send_email()
