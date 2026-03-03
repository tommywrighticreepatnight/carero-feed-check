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

# Загружаем предыдущее состояние ИЗ ТАБЛИЦЫ (не из файла!)
prev_dict = {}
pending_actions_from_sheet = {}

if GOOGLE_SHEETS_CREDENTIALS and GOOGLE_SHEET_ID:
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
        
        creds_dict = json.loads(GOOGLE_SHEETS_CREDENTIALS)
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
        
        # Читаем предыдущие данные из таблицы
        try:
            all_values = sheet.get_all_values()
            if len(all_values) > 1:  # Есть данные (не только заголовки)
                headers = all_values[0]
                sku_idx = headers.index("SKU")
                stock_idx = headers.index("Current Stock")
                action_idx = headers.index("Action Required")
                status_idx = headers.index("Action Status")
                
                for row in all_values[1:]:  # Пропускаем заголовки
                    if len(row) > max(sku_idx, stock_idx, action_idx, status_idx):
                        sku = row[sku_idx].strip().upper()
                        try:
                            stock = int(row[stock_idx])
                            prev_dict[sku] = stock
                        except:
                            pass
                        
                        # Сохраняем pending действия (где статус не DONE)
                        action = row[action_idx]
                        status = row[status_idx] if len(row) > status_idx else "PENDING"
                        
                        if action in ["REMOVE FROM STORE", "ADD TO STORE"] and status != "DONE":
                            pending_actions_from_sheet[sku] = action
        except Exception as e:
            print(f"Note: Could not read previous state from sheet ({e})")
    except:
        pass

# Собираем отчёт
report = []
new_out_of_stock = []
new_restocked = []

for item in current:
    sku = item["sku"]
    current_stock = item["stock"]
    prev_stock = prev_dict.get(sku, current_stock)
    change = current_stock - prev_stock
    status = "RESTOCKED" if change > 0 else "SOLD" if change < 0 else "UNCHANGED"
    
    # Определяем алерт
    if current_stock <= CRITICAL_STOCK:
        alert = "OUT OF STOCK"
        if prev_stock > CRITICAL_STOCK and current_stock == CRITICAL_STOCK:
            alert = "NEWLY OUT OF STOCK"
            new_out_of_stock.append(item)
    elif current_stock <= WARNING_STOCK:
        alert = "LOW STOCK"
    else:
        alert = "OK"
        if prev_stock == CRITICAL_STOCK and current_stock > CRITICAL_STOCK:
            alert = "RESTOCKED"
            new_restocked.append(item)
    
    # Определяем действие
    action = "NO ACTION"
    action_status = "DONE"
    
    # Если уже есть активное действие в таблице - сохраняем его
    if sku in pending_actions_from_sheet:
        action = pending_actions_from_sheet[sku]
        action_status = "PENDING"
    else:
        # Новое действие
        if alert == "NEWLY OUT OF STOCK":
            action = "REMOVE FROM STORE"
            action_status = "PENDING"
        elif alert == "RESTOCKED":
            action = "ADD TO STORE"
            action_status = "PENDING"
    
    report.append({
        "SKU": sku,
        "Product": item["name"],
        "Current Stock": current_stock,
        "Previous Stock": prev_stock,
        "Change": change,
        "Status": status,
        "Alert Level": alert,
        "Action Required": action,
        "Action Status": action_status,
        "Last Updated": datetime.now().strftime("%Y-%m-%d %H:%M")
    })

# Сохраняем состояние в файл (для резервной копии)
with open("inventory_previous.csv", "w", encoding="utf-8") as f:
    f.write("sku,stock\n")
    for item in current:
        f.write(f"{item['sku']},{item['stock']}\n")

print(f"\n✅ Inventory check complete")
print(f"Total tracked: {len(current)}")
print(f"Newly out of stock: {len(new_out_of_stock)}")
print(f"Restocked: {len(new_restocked)}")

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
        
        # Полная очистка + сброс размера
        sheet.clear()
        sheet.resize(rows=1)
        
        # Заголовки (включая "Last Updated")
        headers = ["SKU", "Product", "Current Stock", "Previous Stock", "Change", "Status", 
                   "Alert Level", "Action Required", "Action Status", "Last Updated"]
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
                row["Action Status"],
                row["Last Updated"]
            ])
        
        print(f"✓ Google Sheets updated ({len(report)} rows)")
        
        # Показываем новые события
        if new_out_of_stock:
            print(f"\n⚠️ {len(new_out_of_stock)} NEWLY OUT OF STOCK:")
            for item in new_out_of_stock:
                print(f"   - {item['sku']}: {item['name']} (stock: {item['stock']})")
        
        if new_restocked:
            print(f"\n✅ {len(new_restocked)} RESTOCKED:")
            for item in new_restocked:
                print(f"   - {item['sku']}: {item['name']} (stock: {item['stock']})")
        
        print("\n💡 HOW TO USE:")
        print("1. Open your sheet → sort by 'Action Status' to see PENDING actions at top")
        print("2. Do the action (remove/add product in your store)")
        print("3. Change 'Action Status' from PENDING to DONE in the sheet")
        print("4. Action will disappear from PENDING list on next run")
    
    except Exception as e:
        print(f"Google Sheets error: {e}")
else:
    print("WARNING: Google Sheets credentials not set")
