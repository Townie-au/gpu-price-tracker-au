import json, re, sys, time, pathlib, datetime as dt
from typing import Optional
import yaml
from bs4 import BeautifulSoup
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
OUTDIR = ROOT / "out"
OUTDIR.mkdir(exist_ok=True, parents=True)
HISTORY = OUTDIR / "history.json"
LATEST  = OUTDIR / "latest.json"
CONFIG  = ROOT / "config" / "stores.yml"

PRICE_RE = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")

class Store(BaseModel):
    name: str
    url: str
    price_selector: str
    stock_selector: Optional[str] = None
    in_stock_text: Optional[str] = None

class Snapshot(BaseModel):
    ts: str
    sku: str
    lowest: Optional[dict]
    stores: list

def extract_price(text: str) -> Optional[float]:
    m = PRICE_RE.search(text or "")
    if not m: return None
    v = m.group(1).replace(",", "")
    try:
        return float(v)
    except:
        return None

def scrape_store(page, store: Store):
    page.goto(store.url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(1.0)  # small settle
    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    # price
    price_text = None
    for sel in store.price_selector.split(","):
        el = soup.select_one(sel.strip())
        if el and el.get_text(strip=True):
            price_text = el.get_text(" ", strip=True)
            break
    price = extract_price(price_text or "")

    # stock
    in_stock = None
    if store.stock_selector:
        el = soup.select_one(store.stock_selector)
        if el:
            text = el.get_text(" ", strip=True).lower()
            if store.in_stock_text:
                in_stock = store.in_stock_text.lower() in text
            else:
                in_stock = ("in stock" in text) or ("available" in text)

    return {
        "store": store.name,
        "price_aud": price,
        "in_stock": in_stock,
        "url": store.url
    }

def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return []

def save_history(hist):
    HISTORY.write_text(json.dumps(hist, indent=2))

def today_iso_date_tz():
    # Sydney time assumed by Actions runner via tz setting in workflow
    return dt.datetime.now().strftime("%Y-%m-%d")

def main():
    cfg = yaml.safe_load(CONFIG.read_text())
    sku = cfg["sku"]
    stores = [Store(**s) for s in cfg["stores"]]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari")
        results = []
        for s in stores:
            try:
                results.append(scrape_store(page, s))
            except Exception as e:
                results.append({"store": s.name, "price_aud": None, "in_stock": None, "url": s.url, "error": str(e)})
        browser.close()

    # pick lowest price with not-null
    valid = [r for r in results if isinstance(r.get("price_aud"), (int, float))]
    lowest = None
    if valid:
        lowest = min(valid, key=lambda r: r["price_aud"])

    snapshot = Snapshot(
        ts=dt.datetime.now(dt.timezone(dt.timedelta(hours=10))).isoformat(),
        sku=sku,
        lowest=lowest,
        stores=results
    )
    LATEST.write_text(json.dumps(snapshot.model_dump(), indent=2))

    # history append
    hist = load_history()
    day = today_iso_date_tz()
    # upsert by date
    entry = next((h for h in hist if h.get("date")==day), None)
    lowest_price = lowest["price_aud"] if lowest else None
    if entry:
        entry["lowest_price_aud"] = lowest_price
    else:
        hist.append({"date": day, "lowest_price_aud": lowest_price})
    # keep last 365 days
    hist = sorted(hist, key=lambda x: x["date"])[-365:]
    save_history(hist)

    # make a tiny index for GitHub Pages sanity check
    (OUTDIR / "index.html").write_text(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>GPU Price Tracker</title></head>
<body>
<h1>{sku}</h1>
<p>Updated: {snapshot.ts}</p>
<h2>Lowest</h2>
<pre>{json.dumps(lowest, indent=2)}</pre>
<h2>Stores</h2>
<pre>{json.dumps(results, indent=2)}</pre>
<h2>History</h2>
<pre>{json.dumps(hist[-30:], indent=2)}</pre>
</body></html>""")

if __name__ == "__main__":
    sys.exit(main())
