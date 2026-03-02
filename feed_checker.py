import requests
import pandas as pd
import re
from datetime import datetime
import os
import sys

# CONFIG
FEED_URL = "https://b2b.dvedeti.cz/36365?password=36365"
MY_SKUS_FILE = "my_skus.xlsx"
CRITICAL_STOCK = 0
WARNING_STOCK = 3
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")
RECIPIENT_EMAILS = [e.strip() for e in os.environ.get("RECIPIENT_EMAILS", "").split(",") if e.strip()]

print("Fetching DveDeti feed...")
print(f"URL: {FEED_URL}")

# Загрузка фида
max_retries = 3
timeout = 120
xml = None

for attempt in range(max_retries):
    try:
        print(f"Attempt {attempt + 1}/{max_retries}...")
        r = requests.get(FEED_URL, timeout=timeout)
        r.raise_for_status()
        xml = r.content.decode('utf-8', errors='ignore')
        print(f"✓ Feed loaded ({len(xml) // 1024 // 1024} MB)")
        break
    except Exception as e:
        print(f"Error on attempt {attempt + 1}: {e}")
        if attempt == max_retries - 1:
            print(f"FAILED to fetch feed after {max_retries} attempts")
            sys.exit(1)

# ОТЛАДКА: проверяем наличие твоих SKUs в сыром фиде
print("\n🔍 DEBUG: Checking if your SKUs exist in feed...")
sample_skus = ['MI06', 'MR03S', 'UG70100']
for sku in sample_skus:
    if sku in xml:
        print(f"  ✓ SKU '{sku}' FOUND in feed")
    else:
        print(f"  ✗ SKU '{sku}' NOT FOUND in feed")

# ОТЛАДКА: ищем реальные теги в фиде
print("\n🔍 DEBUG: Looking for product tags in first 10KB of feed...")
sample = xml[:10000].lower()
tags_found = []
for tag in ['produkt', 'zbozi', 'item', 'product', 'kod', 'pocetnasklade']:
    if tag in sample:
        tags_found.append(tag)
print(f"  Tags detected: {tags_found}")

# ПАРСИМ БОЛЕЕ ГИБКО: ищем <KOD> напрямую внутри блоков
items = []
# Ищем все блоки между <KOD> и </KOD> с захватом соседних тегов
for match in re.finditer(r'<KOD>([^<]+)</KOD>.*?<POCETNASKLADE>([^<]+)</POCETNASKLADE>.*?<NAZEV>([^<]+)</NAZEV>', xml, re.DOTALL | re.IGNORECASE):
    try:
        sku = match.group(1).strip().upper()
        stock = int(match.group(2).strip())
        name = match.group(3).strip()
        items.append({"sku": sku, "stock": stock, "name": name})
    except:
        continue

# Если не нашли — пробуем альтернативный паттерн
if len(items) == 0:
    print("Trying fallback parser...")
    for match in re.finditer(r'<PRODUKT[^>]*>(.*?)</PRODUKT>', xml, re.DOTALL | re.IGNORECASE):
        block = match.group(1)
        kod = re.search(r'<KOD>([^<]+)</KOD>', block, re.IGNORECASE)
        stock_txt = re.search(r'<POCETNASKLADE>([^<]+)</POCETNASKLADE>', block, re.IGNORECASE)
        name = re.search(r'<NAZEV>([^<]+)</NAZEV>', block, re.IGNORECASE)
        
        if kod and stock_txt:
            try:
                stock = int(stock_txt.group(1).strip())
                sku = kod.group(1).strip().upper()
                items.append({
                    "sku": sku,
                    "stock": stock,
                    "name": name.group(1).strip() if name else "Unknown"
                })
            except:
                continue

print(f"\nParsed {len(items)} products from feed")

if len(items) == 0:
    print("ERROR: No products parsed. Showing first 500 chars of feed for debugging:")
    print(xml[:500])
    sys.exit(1)

# Загружаем твои SKU
if not os.path.exists(MY_SKUS_FILE):
    print(f"MISSING: {MY_SKUS_FILE} not found")
    sys.exit(1)

my_skus = pd.read_excel(MY_SKUS_FILE)["SKU"].astype(str).str.strip().str.upper().tolist()
current = [i for i in items if i["sku"] in my_skus]

if not current:
    print("WARNING: No matching SKUs found")
    print(f"First 3 feed SKUs: {[i['sku'] for i in items[:3]]}")
    print(f"Your SKUs: {my_skus[:3]}")
    print("\n💡 TIP: Check if your SKUs in my_skus.xlsx match the <KOD> values EXACTLY (case/spaces)")
    sys.exit(1)

print(f"Tracking {len(current)} of your SKUs")

# Загружаем предыдущее состояние
prev_dict = {}
if os.path.exists("inventory_previous.csv"):
    prev_df = pd.read_csv("inventory_previous.csv")
    prev_dict = dict(zip(prev_df["sku"], prev_df["stock"]))

# Собираем отчёт
report = []
new_out_of_stock = []
new_warning = []

for item in current:
    prev = prev_dict.get(item["sku"], item["stock"])
    change = item["stock"] - prev
    status = "RESTOCKED" if change > 0 else "SOLD" if change < 0 else "UNCHANGED"
    
    if item["stock"] <= CRITICAL_STOCK:
        alert = "OUT OF STOCK"
        if prev > CRITICAL_STOCK:
            new_out_of_stock.append(item)
    elif item["stock"] <= WARNING_STOCK:
        alert = "DANGEROUS"
        if prev > WARNING_STOCK:
            new_warning.append(item)
    else:
        alert = "OK"
    
    report.append({
        "SKU": item["sku"],
        "Product": item["name"],
        "Current Stock": item["stock"],
        "Previous Stock": prev,
        "Change": change,
        "Status": status,
        "Alert Level": alert
    })

# Сохраняем состояние
pd.DataFrame(current)[["sku", "stock"]].to_csv("inventory_previous.csv", index=False)

# Генерируем Excel
df = pd.DataFrame(report)
df = df.sort_values("Alert Level", key=lambda x: x.map({
    "OUT OF STOCK": 0, "DANGEROUS": 1, "OK": 2, "UNCHANGED": 3
}))
today = datetime.now().strftime("%Y%m%d")
report_file = f"DVEDETI_INVENTORY_{today}.xlsx"
df.to_excel(report_file, index=False)

print(f"\n✅ DONE. Report: {report_file}")
print(f"Out of stock: {len([r for r in report if r['Alert Level'] == 'OUT OF STOCK'])}")
print(f"Dangerous (<=3): {len([r for r in report if r['Alert Level'] == 'DANGEROUS'])}")

# Отправка письма через Brevo
if new_out_of_stock and BREVO_API_KEY and RECIPIENT_EMAILS:
    subject = f"OUT OF STOCK ALERT - {len(new_out_of_stock)} products"
    body = f"Products that just ran out of stock ({today}):\n\n"
    
    for item in new_out_of_stock:
        body += f"- SKU: {item['sku']} | {item['name']} | Stock: {item['stock']}\n"
    
    body += f"\nFull report attached: {report_file}"
    
    try:
        import requests as req
        response = req.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "sender": {"email": "alerts@bk-feed-check.com", "name": "BK Inventory Bot"},
                "to": [{"email": email} for email in RECIPIENT_EMAILS],
                "subject": subject,
                "textContent": body
            }
        )
        
        if response.status_code in [200, 201]:
            print(f"Email sent to {len(RECIPIENT_EMAILS)} recipients via Brevo")
        else:
            print(f"Brevo failed: {response.status_code} {response.text}")
    except Exception as e:
        print(f"Email error: {e}")
elif not new_out_of_stock:
    print("No new out-of-stock items — skipping email")
