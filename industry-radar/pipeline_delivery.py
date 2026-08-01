import logging
import os
import smtplib
from email.message import EmailMessage

import markdown

from provider_errors import log_provider_error
from run_date import logical_date_text


logger = logging.getLogger(__name__)


def send_email(report_path, config):
    delivery_cfg = config.get("delivery", {})
    if not delivery_cfg.get("enabled"):
        return

    sender = delivery_cfg.get("sender_email")
    recipient = delivery_cfg.get("recipient_email")
    server = delivery_cfg.get("smtp_server", "smtp.mail.me.com")
    port = delivery_cfg.get("smtp_port", 587)

    password = os.getenv("ICLOUD_APP_PASSWORD")
    if not sender or not recipient or not password:
        print("Email configuration or ICLOUD_APP_PASSWORD missing. Skipping email delivery.")
        return

    print(f"Sending report via email to {recipient}...")

    with open(report_path, 'r', encoding='utf-8') as f:
        md_content = f.read()

    # Convert markdown to HTML
    html_body = markdown.markdown(md_content, extensions=['tables', 'md_in_html'])

    # CSS styling for a premium newsletter look
    html_content = f"""
    <html>
    <head>
    <style>
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            line-height: 1.6;
            color: #1f2937;
            background-color: #f3f4f6;
            margin: 0;
            padding: 20px;
        }}
        .container {{
            max-width: 650px;
            margin: 0 auto;
            background: #ffffff;
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.05);
        }}
        h1 {{
            color: #111827;
            font-size: 26px;
            border-bottom: 3px solid #3b82f6;
            padding-bottom: 12px;
            margin-bottom: 25px;
            font-weight: 800;
        }}
        h2 {{
            color: #2563eb;
            font-size: 22px;
            margin-top: 35px;
            border-bottom: 1px solid #e5e7eb;
            padding-bottom: 10px;
            font-weight: 700;
        }}
        h3 {{
            color: #111827;
            font-size: 18px;
            margin-top: 25px;
            line-height: 1.4;
        }}
        p {{
            margin-bottom: 15px;
            color: #4b5563;
            font-size: 15px;
        }}
        a {{
            color: #3b82f6;
            text-decoration: none;
            font-weight: 500;
        }}
        a:hover {{
            text-decoration: underline;
        }}
        em {{
            color: #6b7280;
            font-style: italic;
            font-size: 14px;
        }}
        hr {{
            border: 0;
            height: 1px;
            background: #e5e7eb;
            margin: 25px 0;
        }}
        strong {{
            color: #111827;
            font-weight: 600;
        }}
    </style>
    </head>
    <body>
        <div class="container">
            {html_body}
        </div>
    </body>
    </html>
    """

    date_str = logical_date_text()
    msg = EmailMessage()
    msg['Subject'] = f"🚀 科技产业情报雷达 - {date_str}"
    msg['From'] = sender
    msg['To'] = recipient

    msg.set_content(md_content) # Plain text fallback
    msg.add_alternative(html_content, subtype='html') # Rich HTML version

    try:
        with smtplib.SMTP(server, port) as smtp:
            smtp.starttls()
            smtp.login(sender, password)
            smtp.send_message(msg)
        print("Email sent successfully!")
        return {
            "state": "accepted_by_smtp",
            "recipient": recipient,
            "sender": sender,
        }
    except Exception as e:
        log_provider_error(
            logger,
            e,
            provider=f"smtp:{server}",
            operation="send_email",
            retryable=False,
            degraded_allowed=False,
        )
        raise
