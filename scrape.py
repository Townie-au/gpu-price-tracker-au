import json, re, sys, time, pathlib, datetime as dt
from typing import Optional, List, Tuple
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

# ---------- heuristics ----------
# Accept realistic GPU prices only; widen if you want.
MIN_PRICE = 1100.0
MAX_PRICE = 3000.0

JUNK_WORDS = [
    "per week", "per fortnight", "per month", "/week", "/wk", "/fortnight",
    "afterpay", "zip", "klarna", "interest-free", "interest free", "laybuy",
    "deposit", "from ", "as low as", "finance", "credit", "rent", "*$", "/mo", "/month"
]
GOOD_WORDS = [
    "add to cart", "in stock", "buy now", "final price", "our price", "inc gst", "incl. gst",
    "price", "member price", "special", "deal", "cart"
]

PRICE_RE = re.compile(r"(?:A?\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)", re.I)

GENERIC_PRICE_SELECTORS = [
    "[itemprop='price']",
    "meta[itemprop='price']",
    "meta[property='product:price:amount']",
    "[data-price]",
    "[data-testid='product-price']",
    "[data-test='Price']",
    "span[itemprop='price'][content]",
    "meta[property='og:price:amount']"
    ".product-price",
    ".price",
    ".final-price",
    ".price .amount",
    ".price .price",
    ".price__current",
    ".productView-price",
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

def plausible(v: Optional[float]) -> bool:
    return v is not None and (MIN_PRICE <= v <= MAX_PRICE)

def to_float(s: str) -> Optional[float]:
    m = PRICE_RE.search(s or "")
    if not m: return None
    try: return float(m.group(1).replace(",", ""))
    except: return None

def text_near(el) -> str:
    # element text + a bit of parent text for context
    t = (el.get_text(" ", strip=True) if hasattr(el, "get_text") else "") or ""
    p = el.parent.get_text(" ", strip=True) if getattr(el, "parent", None) else ""
    s = (t + " " + p).lower()
    return re.sub(r"\s+", " ", s)[:400]

def score_context(txt: str) -> int:
    s = 0
    for w in GOOD_WORDS:
        if w in txt: s += 2
    for w in JUNK_WORDS:
        if w in txt: s -= 6
    return s

def collect_css_candidates(soup: BeautifulSoup, selectors: List[str]) -> List[Tuple[float,int,str]]:
    cands = []
    for sel in selectors:
        for el in soup.select(sel):
            # meta price in content/value
            if getattr(el, "name", "") == "meta":
                val = el.get("content") or el.get("value") or ""
                v = to_float(val)
                if plausible(v):
                    cands.append((v, 15, f"meta:{sel}"))
                continue
            # text price
            txt = el.get_text(" ", strip=True)
            v = to_float(txt)
            if plausible(v):
                ctx = text_near(el)
                cands.append((v, 10 + score_context(ctx), f"css:{sel} ctx"))
    return cands

def collect_jsonld_candidates(soup: BeautifulSoup) -> List[Tuple[float,int,str]]:
    out = []
    for s in soup.find_all("script", {"type":"application/ld+json"}):
        raw = s.get_text(strip=True) or ""
        # quick wins
        for m in re.finditer(r'"price"\s*:\s*"?(?P<val>[0-9]{2,5}(?:\.[0-9]{2})?)"?', raw):
            v = to_float("A$"+m.group("val"))
            if plausible(v): out.append((v, 18, "jsonld:price"))
        # low/high price fields
        for key in ("lowPrice","highPrice","priceAmount"):
            for m in re.finditer(rf'"{key}"\s*:\s*"?(?P<val>[0-9]{{2,5}}(?:\.[0-9]{{2}})?)"?', raw):
                v = to_float("A$"+m.group("val"))
                if plausible(v): out.append((v, 16, f"jsonld:{key}"))
    return out

def collect_regex_candidates(html: str) -> List[Tuple[float,int,str]]:
    cands = []
    # require currency symbol; avoid bare numbers
    for m in re.finditer(r"(?:A?\$)\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{2})?)", html, re.I):
        v = to_float("A$"+m.group(1))
        if plausible(v):
            # peek around the match for bad words
            start = max(0, m.start()-80); end = min(len(html), m.end()+80)
            ctx = html[start:end].lower()
            pen = -8 if any(w in ctx for w in JUNK_WORDS) else 0
            cands.append((v, 5 + pen, "regex"))
    return cands

def choose_best(cands: List[Tuple[float,int,str]]) -> Optional[float]:
    if not cands: return None
    # pick by score then value (prefer higher if same score to avoid per-week small numbers)
    cands.sort(key=lambda x: (x[1], x[0]), reverse=True)
    return cands[0][0]

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

def detect_in_stock(soup, selectors: list[str], needle: Optional[str]) -> Optional[bool]:
    for sel in selectors:
        el = soup.select_one(sel)
        if not el: continue
        text = el.get_text(" ", strip=True).lower()
        if needle:
            return needle.lower() in text
        if any(tok in text for tok in ("in stock", "available", "ready to ship", "in stock online")):
            return True
        if any(tok in text for tok in ("out of stock", "sold out", "unavailable", "pre-order", "backorder")):
            return False
    return None

def scrape_store(page, store: Store):
    page.goto(store.url, wait_until="networkidle", timeout=60000)
    time.sleep(1.5)
    html = page.content()
    (DEBUGDIR / f"{store.name}.html").write_text(html[:300000], errors="ignore")
    soup = BeautifulSoup(html, "lxml")

    cands: List[Tuple[float,int,str]] = []

    # Store-specific selectors first
    store_sels = [s.strip() for s in (store.price_selector or "").split(",") if s.strip()]
    if store_sels:
        cands += collect_css_candidates(soup, store_sels)

    # Generic CSS
    cands += collect_css_candidates(soup, GENERIC_PRICE_SELECTORS)

    # JSON-LD
    cands += collect_jsonld_candidates(soup)

    # Regex fallback
    cands += collect_regex_candidates(html)

    price = choose_best(cands)

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

    valid = [r for r in results if isinstance(r.get("price_aud"), (int, float))]
    lowest = min(valid, key=lambda r: r["price_aud"]) if valid else None

    snapshot = Snapshot(
        ts=dt.datetime.now(dt.timezone(dt.timedelta(hours=10))).isoformat(),
        sku=sku,
        lowest=lowest,
        stores=results
    )
    LATEST.write_text(json.dumps(snapshot.model_dump(), indent=2))

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
