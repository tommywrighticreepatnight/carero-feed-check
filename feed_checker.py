import requests
import re
from datetime import datetime
import os
import sys
import json

# ========== КОНФИГУРАЦИЯ ==========
# Твои SKUs — просто вставь сюда (значения из тега <KOD> фида)
MY_SKUS = [
    "MI06",
    "MR03S",
    "UG70100",
    # Добавляй сюда новые через запятую
    # "SKU1",
    # "SKU2",
]

FEED_URL = "https://b2b.dvedeti.cz/36365?password=36365"
CRITICAL_STOCK = 0
WARNING_STOCK = 3
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
RECIPIENT_EMAILS = [e.strip() for e in os.environ.get("RECIPIENT_EMAILS", "").split(",") if e.strip()]
GOOGLE_SHEETS_CREDENTIALS = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

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

# Парсим фид
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

print(f"Parsed {len(items)} products from feed")

# Фильтруем только твои SKUs
my_skus_set = set([s.upper() for s in MY_SKUS])
current = [i for i in items if i["sku"] in my_skus_set]

if not current:
    print("WARNING: No matching SKUs found")
    print(f"First 3 feed SKUs: {[i['sku'] for i in items[:3]]}")
    print(f"Your SKUs: {MY_SKUS[:3]}")
    sys.exit(1)

print(f"Tracking {len(current)} of your SKUs")

# Загружаем предыдущее состояние из файла
prev_dict = {}
if os.path.exists("inventory_previous.csv"):
    with open("inventory_previous.csv", "r", encoding="utf-8") as f:
        for line in f:
            if "," in line:
                sku, stock = line.strip().split(",", 1)
                try:
                    prev_dict[sku] = int(stock)
                except:
                    pass

# Собираем отчёт
report = []
new_out_of_stock = []

for item in current:
    prev = prev_dict.get(item["sku"], item["stock"])
    change = item["stock"] - prev
    status = "RESTOCKED" if change > 0 else "SOLD" if change < 0 else "UNCHANGED"
    
    if item["stock"] <= CRITICAL_STOCK:
        if prev > CRITICAL_STOCK:
            alert = "NEWLY OUT OF STOCK, CHECK NEEDED"
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
with open("inventory_previous.csv", "w", encoding="utf-8") as f:
    f.write("sku,stock\n")
    for item in current:
        f.write(f"{item['sku']},{item['stock']}\n")

print(f"\n✅ Inventory check complete")
print(f"Total tracked: {len(current)}")
print(f"Out of stock: {len([r for r in report if r['Alert Level'] == 'OUT OF STOCK'])}")
print(f"Newly out of stock: {len(new_out_of_stock)}")
print(f"Dangerous (<=3): {len([r for r in report if r['Alert Level'] == 'DANGEROUS'])}")

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
        
        # Очищаем и пишем заголовки
        sheet.clear()
        headers = ["SKU", "Product", "Current Stock", "Previous Stock", "Change", "Status", "Alert Level", "Last Updated"]
        sheet.append_row(headers)
        
        # Пишем данные
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
        
        if new_out_of_stock:
            print(f"\n⚠️ {len(new_out_of_stock)} NEWLY OUT OF STOCK items:")
            for item in new_out_of_stock:
                print(f"   - {item['sku']}: {item['name']} (stock: {item['stock']})")
            print("\nOpen your sheet → sort by 'Alert Level' to see them at top")
    
    except Exception as e:
        print(f"Google Sheets error: {e}")
else:
    print("WARNING: Google Sheets credentials not set — skipping sheet update")

# ОТПРАВЛЯЕМ ПИСЬМО ПРИ НОВЫХ НУЛЕВЫХ СТОКАХ
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
            print(f"✓ Email sent to {len(RECIPIENT_EMAILS)} recipients")
        else:
            print(f"Brevo failed: {response.status_code}")
    except Exception as e:
        print(f"Email error: {e}")
elif not new_out_of_stock:
    print("No new out-of-stock items — skipping email")
