import requests
import pandas as pd
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
import os
import sys

# CONFIG - CHANGE THESE
FEED_URL = "https://velkoobchod.carero.cz/feed/Eshop-rychle_cz.aspx"
MY_SKUS_FILE = "my_skus.xlsx"  # Must be in repo root
CRITICAL_STOCK = 10
WARNING_STOCK = 20
EMAIL_ADDRESS = os.environ.get("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
RECIPIENT_EMAILS = [e.strip() for e in os.environ.get("RECIPIENT_EMAILS", "").split(",") if e.strip()]

print("Fetching Carero feed...")
try:
    r = requests.get(FEED_URL, timeout=30)
    r.raise_for_status()
    xml = r.content.decode('utf-8')
except Exception as e:
    print(f"FAILED to fetch feed: {e}")
    sys.exit(1)

# Parse feed
items = []
for match in re.finditer(r'<SHOPITEM>(.*?)</SHOPITEM>', xml, re.DOTALL):
    item_xml = match.group(1)
    sku = re.search(r'<ID_PRODUCT>(.*?)</ID_PRODUCT>', item_xml)
    stock_txt = re.search(r'<SKLADOVOST>(.*?)</SKLADOVOST>', item_xml)
    name = re.search(r'<PRODUCT>(.*?)</PRODUCT>', item_xml)
    group = re.search(r'<SKUPINA>(.*?)</SKUPINA>', item_xml)
    
    if sku and stock_txt:
        try:
            stock = int(stock_txt.group(1))
        except:
            stock = 0
        items.append({
            "sku": sku.group(1).strip().upper(),
            "stock": stock,
            "name": name.group(1).strip() if name else "Unknown",
            "group_id": group.group(1).strip() if group else ""
        })

print(f"Parsed {len(items)} products")

# Load your SKUs
if not os.path.exists(MY_SKUS_FILE):
    print(f"MISSING: {MY_SKUS_FILE} not found in repo root")
    sys.exit(1)

my_skus = pd.read_excel(MY_SKUS_FILE)["SKU"].astype(str).str.strip().str.upper().tolist()
current = [i for i in items if i["sku"] in my_skus]

if not current:
    print("WARNING: No matching SKUs found")
    sys.exit(1)

# Load previous state
prev_dict = {}
if os.path.exists("inventory_previous.csv"):
    prev_df = pd.read_csv("inventory_previous.csv")
    prev_dict = dict(zip(prev_df["sku"], prev_df["stock"]))

# Build report + detect NEW alerts
report = []
new_critical = []
new_warning = []

for item in current:
    prev = prev_dict.get(item["sku"], item["stock"])
    change = item["stock"] - prev
    status = "RESTOCKED" if change > 0 else "SOLD" if change < 0 else "UNCHANGED"
    current_alert = "CRITICAL" if item["stock"] <= CRITICAL_STOCK else "WARNING" if item["stock"] <= WARNING_STOCK else "OK"
    prev_alert = "CRITICAL" if prev <= CRITICAL_STOCK else "WARNING" if prev <= WARNING_STOCK else "OK"
    
    # Detect NEW critical/warning states
    if current_alert == "CRITICAL" and prev_alert != "CRITICAL":
        new_critical.append(item)
    elif current_alert == "WARNING" and prev_alert not in ["WARNING", "CRITICAL"]:
        new_warning.append(item)
    
    report.append({
        "SKU": item["sku"],
        "Product": item["name"],
        "Group ID": item["group_id"],
        "Current Stock": item["stock"],
        "Previous Stock": prev,
        "Change": change,
        "Status": status,
        "Alert Level": current_alert
    })

# Save state for next run
pd.DataFrame(current)[["sku", "stock"]].to_csv("inventory_previous.csv", index=False)

# Generate Excel report
df = pd.DataFrame(report)
df = df.sort_values("Alert Level", key=lambda x: x.map({"CRITICAL":0, "WARNING":1, "OK":2, "UNCHANGED":3}))
today = datetime.now().strftime("%Y%m%d")
report_file = f"CARERO_INVENTORY_{today}.xlsx"
df.to_excel(report_file, index=False)

# SEND EMAIL IF NEW ALERTS EXIST
if (new_critical or new_warning) and EMAIL_ADDRESS and EMAIL_PASSWORD and RECIPIENT_EMAILS:
    subject = f"LOW STOCK ALERT - {len(new_critical)} CRITICAL, {len(new_warning)} WARNING"
    body = f"NEW low stock items detected on {today}:\n\n"
    
    if new_critical:
        body += f"CRITICAL (<= {CRITICAL_STOCK}):\n"
        for item in new_critical:
            body += f"- SKU: {item['sku']} | {item['name']} | Stock: {item['stock']}\n"
        body += "\n"
    
    if new_warning:
        body += f"WARNING (<= {WARNING_STOCK}):\n"
        for item in new_warning:
            body += f"- SKU: {item['sku']} | {item['name']} | Stock: {item['stock']}\n"
    
    body += f"\nFull report attached: {report_file}"
    
    msg = MIMEMultipart()
    msg['From'] = EMAIL_ADDRESS
    msg['To'] = ", ".join(RECIPIENT_EMAILS)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'plain'))
    
    # Attach Excel
    with open(report_file, "rb") as f:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header('Content-Disposition', f'attachment; filename={report_file}')
        msg.attach(part)
    
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"Email sent to {len(RECIPIENT_EMAILS)} recipients")
    except Exception as e:
        print(f"Email failed: {e}")

print(f"DONE. Report: {report_file}")
print(f"New critical: {len(new_critical)}, New warning: {len(new_warning)}")
