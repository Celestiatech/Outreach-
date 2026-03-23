#!/usr/bin/env python
"""Quick SMTP connection test using Brevo credentials."""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# Credentials (set via environment variables or directly)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_ADDRESS", "a1aa4e001@smtp-brevo.com")
EMAIL_PASS = os.getenv("EMAIL_PASSWORD", "")
FROM_EMAIL = "anirudhpathania384@gmail.com"
FROM_NAME = "Career Vault Platform"

def test_smtp():
    """Test SMTP connection and send a test email."""
    print(f"Testing SMTP connection to {SMTP_HOST}:{SMTP_PORT}...")
    print(f"Using account: {EMAIL_USER}")
    
    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        print("✓ Connected to SMTP server")
        
        server.starttls()
        print("✓ TLS enabled")
        
        server.login(EMAIL_USER, EMAIL_PASS)
        print(f"✓ Authenticated as {EMAIL_USER}")
        
        # Create a test email
        msg = MIMEMultipart()
        msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg["To"] = "anirudhpathania384@gmail.com"  # Send to yourself for testing
        msg["Subject"] = "✓ Brevo SMTP Test"
        
        body = """
This is a test email from the Outreach platform.

If you received this, the SMTP connection is working correctly!

Ready to start sending outreach campaigns.
        """.strip()
        
        msg.attach(MIMEText(body, "plain"))
        
        print("Sending test email to anirudhpathania384@gmail.com...")
        server.send_message(msg)
        print("✓ Test email sent successfully!")
        
        server.quit()
        print("\n✅ All checks passed. You're ready to send emails.")
        
    except smtplib.SMTPAuthenticationError as e:
        print(f"❌ Authentication failed: {e}")
        print("   Check your email/password credentials")
    except smtplib.SMTPException as e:
        print(f"❌ SMTP error: {e}")
    except Exception as e:
        print(f"❌ Connection error: {e}")

if __name__ == "__main__":
    # Ensure credentials are set
    if not os.getenv("EMAIL_PASSWORD"):
        print("❌ EMAIL_PASSWORD environment variable not set!")
        print("   Set it before running this test.")
        exit(1)
    
    test_smtp()
