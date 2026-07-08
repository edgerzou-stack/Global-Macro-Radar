import os
import sys
import re
from dotenv import load_dotenv
import os

env_path = os.environ.get("RADAR_ENV", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "industry-radar", ".env"))
if env_path and os.path.exists(env_path):
    load_dotenv(env_path)
else:
    load_dotenv() # Load from current directory if possible
import glob
import markdown
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

def get_latest_radar_report():
    # P2.13: 移除硬编码路径，使用基于当前文件的相对路径或环境变量
    default_reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "industry-radar", "reports")
    radar_reports_dir = os.environ.get("RADAR_REPORTS_DIR", default_reports_dir)
    reports = glob.glob(os.path.join(radar_reports_dir, "*.md"))
    if not reports:
        return None
    reports.sort(reverse=True)
    return reports[0]

def main():
    radar_report = get_latest_radar_report()
    from config import PROJECT_ROOT
    quant_html = os.path.join(PROJECT_ROOT, "reports", "screening_results.html")
    
    import yaml
    default_radar_config = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "industry-radar", "config.yaml")
    config_path = os.environ.get("RADAR_CONFIG", default_radar_config)
    
    config = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    delivery_cfg = config.get("delivery", {})
    sender = delivery_cfg.get("sender_email")
    recipient = delivery_cfg.get("recipient_email")
    server = delivery_cfg.get("smtp_server", "smtp.mail.me.com")
    port = delivery_cfg.get("smtp_port", 587)
    password = os.getenv("ICLOUD_APP_PASSWORD")
    
    if not password:
        print("ICLOUD_APP_PASSWORD not set. Skipping unified email.")
        return
        
    print("Combining Radar and Quant HTML reports into unified email...")
    
    radar_html = ""
    if radar_report and os.path.exists(radar_report):
        with open(radar_report, 'r', encoding='utf-8') as f:
            radar_md = f.read()
            radar_md = re.sub(r'!\[.*?\]\(.*?\)', '', radar_md)
            radar_html = markdown.markdown(radar_md, extensions=['tables', 'md_in_html'])
            radar_html = f"<h2>🌍 第一部分：全球前沿产业雷达</h2>\n<div style='margin-bottom: 40px;'>{radar_html}</div>\n<hr>\n"
            
    if os.path.exists(quant_html):
        with open(quant_html, 'r', encoding='utf-8') as f:
            html_content = f.read()
            
        if radar_html:
            html_content = html_content.replace("<h1>每日全球策略量化报告</h1>", f"<h1>每日全球策略量化报告</h1>\n{radar_html}")
    else:
        html_content = f"""
        <html><body><div class="container">
        <h1>每日全球策略量化报告</h1>
        {radar_html}
        <p>No quant report found.</p>
        </div></body></html>
        """

    msg = MIMEMultipart('related')
    msg['Subject'] = "🌍 全球前沿产业与量化实盘通讯"
    msg['From'] = sender
    msg['To'] = recipient

    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    try:
        smtp = smtplib.SMTP(server, port)
        smtp.starttls()
        smtp.login(sender, password)
        smtp.send_message(msg)
        smtp.quit()
        print("Successfully sent unified email!")
    except Exception as e:
        print(f"Failed to send email: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
