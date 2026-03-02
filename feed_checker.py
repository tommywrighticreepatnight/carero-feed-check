import requests
import pandas as pd
import re
from datetime import datetime
import os
import sys
import json

# CONFIG
FEED_URL = "https://b2b.dvedeti.cz/36365?password=36365"
MY_SKUS_FILE = "my_skus.csv"
CRITICAL_STOCK = 0
WARNING_STOCK = 3
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
RECIPIENT_EMAILS = [e.strip() for e in os.environ.get("RECIPIENT_EMAILS", "").split(",") if e.strip()]
GOOGLE_SHEETS_CREDENTIALS = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "YOUR_SHEET_ID_HERE")  # ← ЗАМЕНИ НА СВОЙ ID

print("Fetching DveDeti feed...")
try:
    r = requests.get(FEED_URL, timeout=120)
    r.raise_for_status()
    xml_str = r.content.decode('utf-8', errors='ignore')
    xml_str = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', xml_str)
    print(f"✓ Feed loaded ({len(xml_str) // 1024 // 1024} MB)")
except Exception as e:
    print(f"FAILED to fetch feed: {e}")
    sys.exit(1)

# Парсим SHOPITEM
print("Parsing SHOPITEM elements...")
items = []
for match in re.finditer(r'<SHOPITEM>(.*?)</SHOPITEM>', xml_str, re.DOTALL):
    block = match.group(1)
    kod = re.search(r'<KOD>([^<]+)</KOD>', block)
    stock = re.search(r'<POCETNASKLADE>([^<]+)</POCETNASKLADE>', block)
    name = re.search(r'<PRODUCT>([^<]+)</PRODUCT>', block)
    
    if kod and stock:
        try:
            sku = kod.group(1).strip().upper()
            stock_val = int(stock.group(1).strip())
            product_name = name.group(1).strip() if name else "Unknown"
            items.append({"sku": sku, "stock": stock_val, "name": product_name})
        except:
            continue

print(f"Parsed {len(items)} products")

# Загружаем твои SKU
if not os.path.exists(MY_SKUS_FILE):
    print(f"MISSING: {MY_SKUS_FILE} not found")
    sys.exit(1)

my_skus = pd.read_csv(MY_SKUS_FILE, encoding='cp1252')["SKU"].astype(str).str.strip().str.upper().tolist()
current = [i for i in items if i["sku"] in my_skus]

if not current:
    print("WARNING: No matching SKUs found")
    print(f"First 3 feed SKUs: {[i['sku'] for i in items[:3]]}")
    print(f"Your SKUs: {my_skus[:3]}")
    sys.exit(1)

print(f"Tracking {len(current)} of your SKUs")

# Загружаем предыдущее состояние из файла
prev_dict = {}
if os.path.exists("inventory_previous.csv"):
    prev_df = pd.read_csv("inventory_previous.csv")
    prev_dict = dict(zip(prev_df["sku"], prev_df["stock"]))

# Собираем отчёт + помечаем НОВЫЕ нулевые стоки
report = []
new_out_of_stock = []

for item in current:
    prev = prev_dict.get(item["sku"], item["stock"])
    change = item["stock"] - prev
    status = "RESTOCKED" if change > 0 else "SOLD" if change < 0 else "UNCHANGED"
    
    if item["stock"] <= CRITICAL_STOCK:
        if prev > CRITICAL_STOCK:
            alert = "NEWLY OUT OF STOCK, CHECK NEEDED"  # ← КЛЮЧЕВАЯ СТРОКА
            new_out_of_stock.append(item)
        else:
            alert = "OUT OF STOCK"
    elif item["stock"] <= WARNING_STOCK:
        alert = "DANGEROUS"
    else:
        alert = "OK"
    
    report.append({
        "SKU": item["sku"],
        "Product": item["name"],
        "Current Stock": item["stock"],
        "Previous Stock": prev,
        "Change": change,
        "Status": status,
        "Alert Level": alert,
        "Last Updated": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

# Сохраняем состояние для завтра
pd.DataFrame(current)[["sku", "stock"]].to_csv("inventory_previous.csv", index=False)

# ОБНОВЛЯЕМ GOOGLE SHEETS
if GOOGLE_SHEETS_CREDENTIALS and GOOGLE_SHEET_ID:
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
        
        print("Updating Google Sheets...")
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Очищаем таблицу (кроме заголовков)
        sheet.clear()
        
        # Записываем заголовки
        headers = ["SKU", "Product", "Current Stock", "Previous Stock", "Change", "Status", "Alert Level", "Last Updated"]
        sheet.append_row(headers)
        
        # Записываем данные
        for row in report:
            sheet.append_row([
                row["SKU"],
                row["Product"],
                row["Current Stock"],
                row["Previous Stock"],
                row["Change"],
                row["Status"],
                row["Alert Level"],
                row["Last Updated"]
            ])
        
        print(f"✓ Google Sheets updated ({len(report)} rows)")
        
        # Выделяем красным строки с "NEWLY OUT OF STOCK"
        if new_out_of_stock:
            print(f"⚠️ {len(new_out_of_stock)} items marked as 'NEWLY OUT OF STOCK, CHECK NEEDED'")
            print("Open your sheet → sort by 'Alert Level' column to see them at top")
    
    except Exception as e:
        print(f"Google Sheets error: {e}")
else:
    print("WARNING: GOOGLE_SHEETS_CREDENTIALS or GOOGLE_SHEET_ID not set — skipping sheet update")

# Отправка письма при новых нулевых стоках
if new_out_of_stock and BREVO_API_KEY and RECIPIENT_EMAILS:
    subject = f"OUT OF STOCK ALERT - {len(new_out_of_stock)} products"
    body = f"Products that just ran out of stock ({datetime.now().strftime('%Y-%m-%d')}):\n\n"
    
    for item in new_out_of_stock:
        body += f"- SKU: {item['sku']} | {item['name']} | Stock: {item['stock']}\n"
    
    body += "\n⚠️ These items are marked as 'NEWLY OUT OF STOCK, CHECK NEEDED' in Google Sheets"
    
    try:
        import requests as req
        response = req.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json={
                "sender": {"email": "alerts@bk-feed-check.com", "name": "BK Inventory Bot"},
                "to": [{"email": email} for email in RECIPIENT_EMAILS],
                "subject": subject,
                "textContent": body
            }
        )
        if response.status_code in [200, 201]:
            print(f"Email sent to {len(RECIPIENT_EMAILS)} recipients")
        else:
            print(f"Brevo failed: {response.status_code}")
    except Exception as e:
        print(f"Email error: {e}")
