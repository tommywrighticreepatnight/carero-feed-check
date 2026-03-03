import requests
import pandas as pd
import re
from datetime import datetime
import os
import sys
import json

# ========== КОНФИГУРАЦИЯ ==========
FEED_URL = "https://b2b.dvedeti.cz/36365?password=36365"
MY_SKUS_FILE = "my_skus.xlsx"
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

# Читаем SKUs из Excel
if not os.path.exists(MY_SKUS_FILE):
    print(f"MISSING: {MY_SKUS_FILE} not found")
    sys.exit(1)

try:
    my_skus_df = pd.read_excel(MY_SKUS_FILE)
    MY_SKUS = my_skus_df["SKU"].astype(str).str.strip().str.upper().tolist()
except Exception as e:
    print(f"Error reading {MY_SKUS_FILE}: {e}")
    sys.exit(1)

print(f"Loaded {len(MY_SKUS)} SKUs from {MY_SKUS_FILE}")

# Фильтруем только твои SKUs
my_skus_set = set(MY_SKUS)
current = [i for i in items if i["sku"] in my_skus_set]

if not current:
    print("WARNING: No matching SKUs found in feed")
    print(f"First 3 feed SKUs: {[i['sku'] for i in items[:3]]}")
    print(f"Your SKUs (first 3): {MY_SKUS[:3]}")
    sys.exit(1)

print(f"Tracking {len(current)} of your SKUs")

# Загружаем предыдущее состояние
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

# Загружаем список активных действий (что ещё не сделано)
pending_actions = {}
if os.path.exists("pending_actions.csv"):
    with open("pending_actions.csv", "r", encoding="utf-8") as f:
        for line in f:
            if "," in line:
                parts = line.strip().split(",", 2)
                if len(parts) == 3:
                    sku, action_type, timestamp = parts
                    pending_actions[sku] = {"action": action_type, "since": timestamp}

# Собираем отчёт + определяем новые события
report = []
new_out_of_stock = []
new_restocked = []

for item in current:
    sku = item["sku"]
    current_stock = item["stock"]
    prev_stock = prev_dict.get(sku, current_stock)
    change = current_stock - prev_stock
    status = "RESTOCKED" if change > 0 else "SOLD" if change < 0 else "UNCHANGED"
    
    # Определяем текущий алерт
    if current_stock <= CRITICAL_STOCK:
        alert = "OUT OF STOCK"
        # НОВОЕ условие: был сток >0, стал 0
        if prev_stock > CRITICAL_STOCK and current_stock == CRITICAL_STOCK:
            alert = "NEWLY OUT OF STOCK"
            new_out_of_stock.append(item)
    elif current_stock <= WARNING_STOCK:
        alert = "LOW STOCK"
    else:
        alert = "OK"
        # Пополнение после нуля: был 0, стал >0
        if prev_stock == CRITICAL_STOCK and current_stock > CRITICAL_STOCK:
            alert = "RESTOCKED"
            new_restocked.append(item)
    
    # Определяем действие (персистентное)
    action = "NO ACTION"
    
    # Если есть активное действие из предыдущего запуска - сохраняем его
    if sku in pending_actions:
        action = pending_actions[sku]["action"]
    else:
        # Новое действие только в двух случаях
        if alert == "NEWLY OUT OF STOCK":
            action = "REMOVE FROM STORE"  # Нужно убрать товар из магазина
            pending_actions[sku] = {
                "action": "REMOVE FROM STORE",
                "since": datetime.now().strftime("%Y-%m-%d %H:%M")
            }
        elif alert == "RESTOCKED":
            action = "ADD TO STORE"  # Нужно добавить товар обратно в магазин
            pending_actions[sku] = {
                "action": "ADD TO STORE",
                "since": datetime.now().strftime("%Y-%m-%d %H:%M")
            }
        # LOW STOCK и OK → NO ACTION
    
    report.append({
        "SKU": sku,
        "Product": item["name"],
        "Current Stock": current_stock,
        "Previous Stock": prev_stock,
        "Change": change,
        "Status": status,
        "Alert Level": alert,
        "Action Required": action,
        "Action Since": pending_actions[sku]["since"] if sku in pending_actions else "-",
        "Last Updated": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

# Сохраняем состояние для завтра
with open("inventory_previous.csv", "w", encoding="utf-8") as f:
    f.write("sku,stock\n")
    for item in current:
        f.write(f"{item['sku']},{item['stock']}\n")

# Сохраняем активные действия (только те, что ещё не выполнены)
with open("pending_actions.csv", "w", encoding="utf-8") as f:
    f.write("sku,action,timestamp\n")
    for sku, data in pending_actions.items():
        f.write(f"{sku},{data['action']},{data['since']}\n")

print(f"\n✅ Inventory check complete")
print(f"Total tracked: {len(current)}")
print(f"Out of stock: {len([r for r in report if r['Alert Level'] == 'OUT OF STOCK'])}")
print(f"Newly out of stock: {len(new_out_of_stock)}")
print(f"Restocked (back from 0): {len(new_restocked)}")
print(f"Low stock (<=3): {len([r for r in report if r['Alert Level'] == 'LOW STOCK'])}")

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
        headers = ["SKU", "Product", "Current Stock", "Previous Stock", "Change", "Status", 
                   "Alert Level", "Action Required", "Action Since", "Last Updated"]
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
                row["Action Required"],
                row["Action Since"],
                row["Last Updated"]
            ])
        
        print(f"✓ Google Sheets updated ({len(report)} rows)")
        
        # Показываем новые критические события
        if new_out_of_stock:
            print(f"\n⚠️ {len(new_out_of_stock)} NEWLY OUT OF STOCK items:")
            for item in new_out_of_stock:
                print(f"   - {item['sku']}: {item['name']} (stock: {item['stock']})")
        
        if new_restocked:
            print(f"\n✅ {len(new_restocked)} RESTOCKED items (back from 0):")
            for item in new_restocked:
                print(f"   - {item['sku']}: {item['name']} (stock: {item['stock']})")
        
        print("\nOpen your sheet → sort by 'Action Required' to see pending actions at top")
    
    except Exception as e:
        print(f"Google Sheets error: {e}")
else:
    print("WARNING: Google Sheets credentials not set — skipping sheet update")

# ОТПРАВЛЯЕМ ПИСЬМО ПРИ НОВЫХ СОБЫТИЯХ
if (new_out_of_stock or new_restocked) and BREVO_API_KEY and RECIPIENT_EMAILS:
    subject = f"Inventory Alert - {len(new_out_of_stock)} out of stock, {len(new_restocked)} restocked"
    body = ""
    
    if new_out_of_stock:
        body += f"Products that just ran out of stock:\n\n"
        for item in new_out_of_stock:
            body += f"- SKU: {item['sku']} | {item['name']} | Stock: {item['stock']}\n"
        body += "\n⚠️ Action required: REMOVE FROM STORE\n\n"
    
    if new_restocked:
        body += f"Products that were restocked (back from 0):\n\n"
        for item in new_restocked:
            body += f"- SKU: {item['sku']} | {item['name']} | Stock: {item['stock']}\n"
        body += "\n✅ Action required: ADD TO STORE\n\n"
    
    body += "See Google Sheets for details and pending actions"
    
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
elif not new_out_of_stock and not new_restocked:
    print("No new critical events — skipping email")
