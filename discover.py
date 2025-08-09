import pathlib, json, time, re, unicodedata
import yaml
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "config" / "stores.yml"
CATALOG = ROOT / "config" / "retailers.yml"

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = re.sub(r"[^a-z0-9+ ]+"," ", s)
    return " ".join(s.split())

def score_title(title: str, tokens: list[str]) -> int:
    t = norm(title)
    return sum(1 for tok in tokens if tok.lower() in t)

def extract_price_text(soup, sels):
    for sel in sels.split(","):
        el = soup.select_one(sel.strip())
        if el:
            txt = el.get_text(" ", strip=True)
            if txt: return txt
    return None

def detect_in_stock(soup, stock_sel, needle):
    if not stock_sel: return None
    el = soup.select_one(stock_sel)
    if not el: return None
    text = el.get_text(" ", strip=True).lower()
    if needle: return needle.lower() in text
    return ("in stock" in text) or ("available" in text)

def run():
    cfg = yaml.safe_load(CATALOG.read_text())
    q = cfg["query"]
    tokens = cfg["tokens"]
    must_include = cfg.get("must_include", [])
    retailers = cfg["retailers"]

    found = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari")

        # 1) Force-include fixed entries (MSY etc.)
        for it in must_include:
            found.append({
                "name": it["name"],
                "url": it["url"],
                "price_selector": it.get("price_selector",".price"),
                "stock_selector": it.get("stock_selector",".stock"),
                "in_stock_text": it.get("in_stock_text","in stock")
            })

        # 2) Discover per retailer via site search
        for r in retailers:
            try:
                search_url = r["search_url"].format(q=quote_plus(q))
                page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
                time.sleep(1.0)
                soup = BeautifulSoup(page.content(), "lxml")
                links = []
                for a in soup.select(r["result_item_selector"]):
                    href = a.get("href") or ""
                    if href.startswith("/"):
                        href = urljoin(search_url, href)
                    links.append((a.get_text(" ", strip=True) or "", href))
                if not links:
                    continue

                # Rank links by token overlap in link text
                ranked = sorted(links, key=lambda x: score_title(x[0], tokens), reverse=True)
                best_href = ranked[0][1]

                # Optional: visit product page and double-check title
                page.goto(best_href, wait_until="domcontentloaded", timeout=45000)
                time.sleep(0.8)
                psoup = BeautifulSoup(page.content(), "lxml")
                title_el = None
                for sel in (r.get("product_title_selector") or "h1").split(","):
                    title_el = psoup.select_one(sel.strip())
                    if title_el: break
                title = title_el.get_text(" ", strip=True) if title_el else best_href
                if score_title(title, tokens) < len(tokens) - 3:
                    # If title is very weak match, skip to avoid false positives
                    continue

                found.append({
                    "name": r["name"],
                    "url": best_href,
                    "price_selector": r["price_selector"],
                    "stock_selector": r.get("stock_selector"),
                    "in_stock_text": r.get("in_stock_text")
                })
            except Exception as e:
                # Just skip on errors; discovery is best-effort
                continue

        browser.close()

    # De-dupe by store name
    seen = set()
    stores = []
    for s in found:
        if s["name"] in seen: 
            continue
        seen.add(s["name"])
        stores.append({
            "name": s["name"],
            "url": s["url"],
            "price_selector": s["price_selector"],
            "stock_selector": s.get("stock_selector"),
            "in_stock_text": s.get("in_stock_text")
        })

    # Write config/stores.yml for the scraper
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(yaml.safe_dump({"sku": cfg["query"], "stores": stores}, sort_keys=False))

if __name__ == "__main__":
    run()
