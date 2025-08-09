import json, re, sys, time, pathlib, datetime as dt
from typing import Optional
import yaml
from bs4 import BeautifulSoup
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
OUTDIR = ROOT / "out"
OUTDIR.mkdir(exist_ok=True, parents=True)
DEBUGDIR = OUTDIR / "debug"
DEBUGDIR.mkdir(exist_ok=True, parents=True)
HISTORY = OUTDIR / "history.json"
LATEST  = OUTDIR / "latest.json"
CONFIG  = ROOT / "config" / "stores.yml"

# price patterns
PRICE_RE = re.compile(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
AUD_NEAR_RE = re.compile(r"(?:A?\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?)", re.I)

GENERIC_PRICE_SELECTORS = [
    "[itemprop='price']",
    "meta[itemprop='price']",
    "meta[property='product:price:amount']",
    "[data-price]",
    "[data-testid='product-price']",
    "[data-test='Price']",
    ".price .amount",
    ".price .price",
    ".product-price",
    ".price",
    ".our-price",
    ".final-price",
    ".productView-price",
    ".p-price",
    ".price__current",
    ".price-section .price",
    "span.price",
    "div.price",
]

GENERIC_STOCK_SELECTORS = [
    "[data-testid='stock']",
    ".availability",
    ".stock-status",
    ".stock",
    ".product-availability",
    ".in-stock",
]

def is_plausible_gpu_price(v: Optional[float]) -> bool:
    if v is None:
        return False
    return 500.0 <= v <= 5000.0

class Store(BaseModel):
    name: str
    url: str
    price_selector: Optional[str] = None
    stock_selector: Optional[str] = None
    in_stock_text: Optional[str] = None

class Snapshot(BaseModel):
    ts: str
    sku: str
    lowest: Optional[dict]
    stores: list

def extract_price_number(text: str) -> Optional[float]:
    if not text:
        return None
    m = AUD_NEAR_RE.search(text)
    if not m:
        m = PRICE_RE.search(text)
    if not m:
        return None
    v = m.group(1).replace(",", "")
    try:
        return float(v)
    except:
        return None

def try_css_price(soup, selectors: list[str]) -> Optional[float]:
    for sel in selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        # meta tag?
        if el.name == "meta":
            content = el.get("content") or el.get("value")
            p = extract_price_number(content or "")
            if p is not None and is_plausible_gpu_price(p):
                return p
        txt = el.get_text(" ", strip=True)
        p = extract_price_number(txt)
        if p is not None and is_plausible_gpu_price(p):
            return p
    return None

def try_jsonld_price(soup) -> Optional[float]:
    for s in soup.find_all("script", {"type": "application/ld+json"}):
        raw = s.get_text(strip=True)
        if not raw:
            continue
        # Try JSON first
        try:
            data = json.loads(raw)
        except Exception:
            # crude fallback: look for "price":"1234.56"
            m = re.search(r'"price"\s*:\s*"?(?P<val>[0-9,]+\.\d{2})"?', raw)
            if m:
                p = float(m.group("val").replace(",", ""))
                return p if is_plausible_gpu_price(p) else None
            continue

        def walk(obj):
            if isinstance(obj, dict):
                for k in ("price", "priceAmount", "lowPrice", "highPrice"):
                    if k in obj:
                        p = extract_price_number(str(obj[k]))
                        if p is not None and is_plausible_gpu_price(p):
                            return p
                offers = obj.get("offers")
                if offers:
                    p = walk(offers)
                    if p is not None:
                        return p
                graph = obj.get("@graph")
                if graph:
                    p = walk(graph)
                    if p is not None:
                        return p
                agg = obj.get("aggregateOffer") or obj.get("aggregateRating")
                if agg:
                    p = walk(agg)
                    if p is not None:
                        return p
                for v in obj.values():
                    p = walk(v)
                    if p is not None:
                        return p
            elif isinstance(obj, list):
                for v in obj:
                    p = walk(v)
                    if p is not None:
                        return p
            return None

        p = walk(data)
        if p is not None and is_plausible_gpu_price(p):
            return p
    return None

def try_regex_price(html: str) -> Optional[float]:
    # Only consider currency-tagged values; avoid bare numbers/IDs
    near = re.findall(
        r"(?:price|now|today|was)[^$A]{0,80}(?:A?\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)",
        html, re.I
    )
    candidates = []
    for s in near:
        try:
            candidates.append(float(s.replace(",", "")))
        except:
            pass
    if not candidates:
        m = re.findall(r"(?:A?\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)", html, re.I)
        for s in m:
            try:
                candidates.append(float(s.replace(",", "")))
            except:
                pass
    candidates = [c for c in candidates if is_plausible_gpu_price(c)]
    if candidates:
        # take the largest plausible amount (avoids per-week/fortnight amounts)
        return max(candidates)
    return None

def detect_in_stock(soup, selectors: list[str], needle: Optional[str]) -> Optional[bool]:
    for sel in selectors:
        el = soup.select_one(sel)
        if not el:
            continue
        text = el.get_text(" ", strip=True).lower()
        if needle:
            return needle.lower() in text
        if any(tok in text for tok in ("in stock", "available", "in-store", "ready to ship", "in stock online")):
            return True
        if any(tok in text for tok in ("out of stock", "sold out", "unavailable", "pre-order", "backorder")):
            return False
    return None

def scrape_store(page, store: Store):
    page.goto(store.url, wait_until="networkidle", timeout=60000)
    time.sleep(1.5)
    html = page.content()
    soup = BeautifulSoup(html, "lxml")

    # dump debug snapshot
    (DEBUGDIR / f"{store.name}.html").write_text(html[:300000], errors="ignore")

    # 1) store-provided selectors first
    price = None
    store_price_sels = []
    if store.price_selector:
        store_price_sels = [s.strip() for s in store.price_selector.split(",") if s.strip()]
        price = try_css_price(soup, store_price_sels)

    # 2) generic CSS selectors
    if price is None:
        price = try_css_price(soup, GENERIC_PRICE_SELECTORS)

    # 3) JSON-LD offers
    if price is None:
        price = try_jsonld_price(soup)

    # 4) regex fallback over whole HTML
    if price is None:
        price = try_regex_price(html)

    # stock detection
    stock = detect_in_stock(
        soup,
        ([s.strip() for s in (store.stock_selector or "").split(",") if s.strip()] or GENERIC_STOCK_SELECTORS),
        store.in_stock_text,
    )

    return {
        "store": store.name,
        "price_aud": price,
        "in_stock": stock,
        "url": store.url
    }

def load_history():
    if HISTORY.exists():
        return json.loads(HISTORY.read_text())
    return []

def save_history(hist):
    HISTORY.write_text(json.dumps(hist, indent=2))

def today_iso_date_tz():
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
                r = scrape_store(page, s)
                print(f"[SCRAPE] {s.name}: price={r['price_aud']} stock={r['in_stock']} url={s.url}")
                results.append(r)
            except Exception as e:
                print(f"[SCRAPE] {s.name}: ERROR {e}")
                results.append({"store": s.name, "price_aud": None, "in_stock": None, "url": s.url, "error": str(e)})

        browser.close()

    # pick lowest price with not-null
    valid = [r for r in results if isinstance(r.get("price_aud"), (int, float))]
    lowest = min(valid, key=lambda r: r["price_aud"]) if valid else None

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
    lowest_price = lowest["price_aud"] if lowest else None
    existing = next((h for h in hist if h.get("date")==day), None)
    if existing:
        existing["lowest_price_aud"] = lowest_price
    else:
        hist.append({"date": day, "lowest_price_aud": lowest_price})
    hist = sorted(hist, key=lambda x: x["date"])[-365:]
    save_history(hist)

    # simple index for Pages
    (OUTDIR / "index.html").write_text(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>GPU Price Tracker</title></head>
<body>
<h1>{sku}</h1>
<p>Updated: {snapshot.ts}</p>
<h2>Lowest</h2>
<pre>{json.dumps(lowest, indent=2)}</pre>
<h2>Stores</h2>
<pre>{json.dumps(results, indent=2)}</pre>
<h2>History (last 30)</h2>
<pre>{json.dumps(hist[-30:], indent=2)}</pre>
<p>Debug HTML saved for each store under out/debug/</p>
</body></html>""")

if __name__ == "__main__":
    sys.exit(main())
