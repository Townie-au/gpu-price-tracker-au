import pathlib, time, re, unicodedata
import yaml
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

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

def run():
    cfg = yaml.safe_load(CATALOG.read_text())
    q = cfg["query"]
    tokens = cfg["tokens"]
    must_include = cfg.get("must_include", [])
    retailers = cfg["retailers"]

    found = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1366, "height": 768}
        )
        page = context.new_page()
        stealth_sync(page)

        # 1) Force-include fixed entries (e.g., MSY)
        for it in must_include:
            print(f"[DISCOVER] force-include: {it['name']} -> {it['url']}")
            found.append({
                "name": it["name"],
                "url": it["url"],
                "price_selector": it.get("price_selector", ".price"),
                "stock_selector": it.get("stock_selector", ".stock"),
                "in_stock_text": it.get("in_stock_text", "in stock")
            })

        # 2) Discover via each retailer's search (kept simple; we mainly rely on must_include)
        for r in retailers:
            try:
                search_url = r["search_url"].format(q=quote_plus(q))
                print(f"[DISCOVER] Retailer={r['name']}  URL={search_url}")
                page.goto(search_url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(6000)  # give CF time if present
                soup = BeautifulSoup(page.content(), "lxml")

                # Try site-provided selector first
                links = []
                sel = r.get("result_item_selector")
                if sel:
                    for a in soup.select(sel):
                        href = a.get("href") or ""
                        if href.startswith("/"):
                            href = urljoin(search_url, href)
                        txt = a.get_text(" ", strip=True) or ""
                        if href:
                            links.append((txt, href))

                # Fallback: all anchors on same host
                if not links:
                    host = search_url.split("/")[2].lower()
                    seen = set()
                    for a in soup.select("a[href]"):
                        href = a.get("href") or ""
                        if href.startswith("/"):
                            href = urljoin(search_url, href)
                        if not href.lower().startswith(("http://", "https://")):
                            continue
                        if host not in href.lower():
                            continue
                        txt = a.get_text(" ", strip=True) or ""
                        if href in seen:
                            continue
                        seen.add(href)
                        links.append((txt, href))

                print(f"[DISCOVER] {r['name']}: found {len(links)} candidate links (with fallback)")
                if not links:
                    continue

                # Score by tokens in text + href
                def href_tokenscore(href: str, toks: list[str]) -> int:
                    h = href.lower().replace("-", " ").replace("_", " ").replace("/", " ")
                    return sum(1 for t in toks if t.lower() in h)

                def productish(href: str) -> int:
                    h = href.lower()
                    keys = ("/product", "/products", "/p/", "/item", "/buy", "/detail")
                    return sum(k in h for k in keys)

                gpu_keywords = ["msi", "geforce", "rtx", "5070", "ti", "inspire", "3x", "oc"]

                def total_score(txt: str, href: str) -> int:
                    s = score_title(txt, tokens) + href_tokenscore(href, tokens)
                    s += href_tokenscore(href, gpu_keywords)
                    s += productish(href) * 2
                    return s

                ranked = sorted(links, key=lambda x: total_score(x[0], x[1]), reverse=True)
                best_txt, best_href = ranked[0]
                best_score = total_score(best_txt, best_href)
                print(f"[DISCOVER] {r['name']}: chosen href = {best_href}  score={best_score}")

                if best_score < 4:
                    print(f"[DISCOVER] {r['name']}: skipped (low combined score)")
                    continue

                # Visit product page and check title loosely
                page.goto(best_href, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(1500)
                psoup = BeautifulSoup(page.content(), "lxml")
                title_el = None
                for sel in (r.get("product_title_selector") or "h1").split(","):
                    sel = sel.strip()
                    if not sel:
                        continue
                    title_el = psoup.select_one(sel)
                    if title_el:
                        break
                title = title_el.get_text(" ", strip=True) if title_el else ""
                if not title:
                    ttag = psoup.select_one("title")
                    title = ttag.get_text(" ", strip=True) if ttag else best_href

                sc = score_title(title, tokens)
                print(f"[DISCOVER] {r['name']}: product title='{title[:120]}'  score={sc}")
                if sc < max(4, len(tokens) - 5):
                    print(f"[DISCOVER] {r['name']}: skipped (weak match)")
                    continue

                found.append({
                    "name": r["name"],
                    "url": best_href,
                    "price_selector": r["price_selector"],
                    "stock_selector": r.get("stock_selector"),
                    "in_stock_text": r.get("in_stock_text")
                })
            except Exception as e:
                print(f"[DISCOVER] {r.get('name','?')}: error: {e}")
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

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(yaml.safe_dump({"sku": cfg["query"], "stores": stores}, sort_keys=False))
    print(f"[DISCOVER] wrote {OUT} with {len(stores)} stores")

if __name__ == "__main__":
    run()
