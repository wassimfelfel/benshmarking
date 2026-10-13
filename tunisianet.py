#!/usr/bin/env python3
# tunisianet_scraper_final_safe.py
# Scraper Tunisianet : requests fast + selenium fallback per-thread + multithread + safe Ctrl+C

import os
import re
import csv
import time
import atexit
import signal
import threading
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# Selenium imports (used on-demand by workers)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

# -------------------- CONFIG --------------------
BASE_URL = "https://www.tunisianet.com.tn"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
OUTPUT_FILE = "produits_tunisianet_final_safe.csv"

MAX_WORKERS = 12            # recommandé pour i5-10400 (6c/12t)
BATCH_SIZE = 100            # écriture par batch (flush après chaque batch)
REQUEST_TIMEOUT = 10
REQUEST_DELAY = 0.03        # petit delay entre tâches terminées
SITEMAP_MAX = 100

# -------------------- THREAD / DRIVER MANAGEMENT --------------------
file_lock = threading.Lock()
thread_local = threading.local()
selenium_drivers = set()
selenium_drivers_lock = threading.Lock()
stop_event = threading.Event()  # signale arrêt demandé (Ctrl+C)

def init_selenium_driver():
    """
    Initialise un Chrome headless par thread (créé uniquement si nécessaire).
    Retour : webdriver instance (owned by current thread)
    """
    if getattr(thread_local, "driver", None):
        return thread_local.driver

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    # disable images/styles for speed
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
    }
    chrome_options.add_experimental_option("prefs", prefs)

    driver = webdriver.Chrome(options=chrome_options)
    thread_local.driver = driver
    with selenium_drivers_lock:
        selenium_drivers.add(driver)
    return driver

def quit_all_selenium_drivers():
    with selenium_drivers_lock:
        for d in list(selenium_drivers):
            try:
                d.quit()
            except Exception:
                pass
        selenium_drivers.clear()

atexit.register(quit_all_selenium_drivers)

# -------------------- SITEMAP -> product links --------------------
def get_product_links():
    print("📄 Lecture des sitemaps 1_fr_X_sitemap.xml ...")
    all_links = []
    for i in range(SITEMAP_MAX):
        if stop_event.is_set():
            break
        sitemap_url = f"{BASE_URL}/1_fr_{i}_sitemap.xml"
        try:
            r = requests.get(sitemap_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if r.status_code == 404 and i > 1:
                break
            r.raise_for_status()
            # capture CDATA urls
            links = re.findall(r"<!\[CDATA\[\s*(https://www\.tunisianet\.com\.tn/[^]]+?\.html)\s*\]\]>", r.text, re.IGNORECASE)
            if links:
                print(f" → {len(links)} liens trouvés dans 1_fr_{i}_sitemap.xml")
                all_links.extend(links)
        except requests.RequestException:
            pass
        time.sleep(0.25)

    # cleanup + de-duplicate; keep only product-like links (containing /<digits>-)
    cleaned = []
    seen = set()
    for l in all_links:
        if stop_event.is_set():
            break
        l2 = re.sub(r'[\?#].*$', '', l)
        if re.search(r'/\d+-', l2) and l2 not in seen:
            seen.add(l2)
            cleaned.append(l2)
    print(f"✅ {len(cleaned)} liens produits uniques trouvés.")
    return cleaned

# -------------------- HTML helpers --------------------
def normalize_text(s: str):
    return s.replace("\u202f", "").replace("\xa0", "").strip() if s else ""

def extract_price_from_text(txt: str):
    if not txt:
        return ""
    s = txt.replace("\u202f", "").replace("\xa0", "").replace("DT", "").replace("dt", "").strip()
    s = s.replace(" ", "")
    # find number with optional thousands/decimals
    m = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+)", s)
    if not m:
        return ""
    raw = m.group(1)
    # normalize: determine decimal separator
    if raw.count(",") > 0 and raw.count(".") == 0:
        val_str = raw.replace(",", ".")
    elif raw.count(".") > 0 and raw.count(",") == 0:
        val_str = raw
    else:
        # both present: assume rightmost is decimal
        if raw.rfind(",") > raw.rfind("."):
            val_str = raw.replace(".", "").replace(",", ".")
        else:
            val_str = raw.replace(",", "")
    try:
        val = float(val_str)
        return f"{val:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".") + " DT"
    except Exception:
        return raw + " DT"

def extract_price(soup: BeautifulSoup):
    # 1) common span price
    selectors = [
        "div.product-price-and-shipping span.price",
        "span[itemprop='price']",
        "span#our_price_display",
        "span.price"
    ]
    for sel in selectors:
        tag = soup.select_one(sel)
        if tag:
            txt = normalize_text(tag.get_text())
            p = extract_price_from_text(txt)
            if p:
                return p
    # 2) meta price
    meta = soup.select_one('meta[itemprop="price"], meta[property="product:price:amount"], meta[itemprop="price:amount"]')
    if meta and meta.get("content"):
        try:
            val = float(meta.get("content"))
            return f"{val:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".") + " DT"
        except Exception:
            return meta.get("content") + " DT"
    return ""

def extract_category(soup: BeautifulSoup):
    items = soup.select("ol[itemtype='http://schema.org/BreadcrumbList'] li span[itemprop='name']")
    if items and len(items) >= 2:
        return normalize_text(items[-2].get_text())
    if items:
        return normalize_text(items[-1].get_text())
    # fallback common breadcrumb nav
    items2 = soup.select("nav.breadcrumb a span[itemprop='name'], nav.breadcrumb a")
    if items2:
        return normalize_text(items2[-1].get_text())
    return "Non spécifié"

def extract_reference(soup: BeautifulSoup):
    # span.product-reference -> [CODE]
    t = soup.select_one("span.product-reference")
    if t:
        return normalize_text(t.get_text()).strip("[]")
    # span[itemprop='sku'] or meta
    t = soup.select_one("span[itemprop='sku']")
    if t:
        return normalize_text(t.get_text()).strip("[]")
    m = soup.select_one('meta[itemprop="sku"], meta[property="product:retailer_item_id"]')
    if m and m.get("content"):
        return m.get("content").strip()
    # fallback search pattern [CODE]
    txt = soup.get_text()
    m2 = re.search(r"\[([A-Z0-9\-]+)\]", txt)
    if m2:
        return m2.group(1)
    return ""

# -------------------- fetch HTML (requests then selenium fallback) --------------------
def fetch_html(url):
    """
    Try requests first. If price/ref not found in raw HTML, fallback to Selenium
    (Selenium driver is created per-thread on demand).
    """
    text = None
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code in (404, 410):
            return None, False  # page disparue : inutile de lancer Chrome
        if r.status_code == 200:
            text = r.text
            # quick check if price or sku exists; if yes return text
            if re.search(r"(itemprop=[\"']price[\"']|product-reference|itemprop=[\"']sku[\"'])", text, re.IGNORECASE):
                return text, False  # False -> selenium not used
            # else fallback to selenium
    except Exception:
        text = None

    # Selenium fallback
    try:
        driver = init_selenium_driver()
        driver.get(url)
        time.sleep(0.5)
        return driver.page_source, True
    except Exception:
        return text or None, False

# -------------------- product parsing --------------------
def parse_product(url):
    if stop_event.is_set():
        return None
    html, used_selenium = fetch_html(url)
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    name_tag = soup.select_one("h1[itemprop='name'], h1.product-title, h1")
    name = normalize_text(name_tag.get_text()) if name_tag else ""

    category = extract_category(soup)
    ref = extract_reference(soup)
    price = extract_price(soup)

    # availability
    stock = soup.select_one("#stock_availability span, #stock_availability p, .in-stock, .out-of-stock, .product-quantities span")
    availability = normalize_text(stock.get_text()) if stock else ""
    if not availability:
        body = soup.get_text().lower()
        if "en stock" in body:
            availability = "En stock"
        elif "rupture" in body or "épuisé" in body or "non disponible" in body:
            availability = "Rupture de stock"
        else:
            availability = "Non précisée"

    return {
        "Catégorie": category,
        "Nom": name,
        "Référence": ref,
        "Prix": price,
        "Disponibilité": availability,
        "Lien": url
    }

# -------------------- safe multi-threaded processing + batch writing --------------------
def safe_worker_write(urls, output_file, start_index=0):
    """
    Orchestrates thread pool, writes in batches, and supports graceful shutdown.
    """
    total_urls = len(urls)
    processed = 0
    buffer = []

    # Prepare CSV file and writer
    write_header = not os.path.exists(output_file) or os.stat(output_file).st_size == 0
    f = open(output_file, "a", newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=["Catégorie", "Nom", "Référence", "Prix", "Disponibilité", "Lien"])
    if write_header:
        writer.writeheader()
        f.flush()

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    futures = {executor.submit(parse_product, url): url for url in urls}

    try:
        for i, fut in enumerate(as_completed(futures), 1):
            if stop_event.is_set():
                break
            url = futures[fut]
            try:
                data = fut.result()
            except Exception as e:
                data = None
                # non-blocking error log (print) — could be replaced by logging to file
                print(f"⚠️ Erreur parse {url}: {e}")

            if data:
                buffer.append(data)

            # flush in batches
            if len(buffer) >= BATCH_SIZE or (i == total_urls and buffer):
                with file_lock:
                    writer.writerows(buffer)
                    f.flush()
                processed += len(buffer)
                buffer.clear()
                print(f"→ {processed}/{total_urls} produits enregistrés")

            # subtle throttle to avoid hitting remote too hard
            time.sleep(REQUEST_DELAY)

    except KeyboardInterrupt:
        print("\n🛑 Ctrl+C détecté — arrêt demandé. Shutting down workers...")
        stop_event.set()
        # Cancel pending futures
        executor.shutdown(wait=False, cancel_futures=True)
    finally:
        # ensure remaining buffer is written
        if buffer:
            with file_lock:
                writer.writerows(buffer)
                f.flush()
            processed += len(buffer)
            buffer.clear()
        # close file
        try:
            f.close()
        except Exception:
            pass
        # shutdown executor if not already
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    return processed

# -------------------- resume support: skip already scraped links --------------------
def load_already_done_links(output_file):
    done = set()
    if os.path.exists(output_file):
        try:
            with open(output_file, "r", encoding="utf-8") as f:
                # skip header, read last column = link
                for line in f:
                    parts = line.rstrip("\n").split(",")
                    if parts:
                        link = parts[-1].strip()
                        if link.startswith("http"):
                            done.add(link)
        except Exception:
            pass
    return done

# -------------------- signal handler to ensure immediate stop --------------------
def handle_signal(sig, frame):
    print("\n🔔 Signal reçu — arrêt demandé.")
    stop_event.set()

signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)

# -------------------- MAIN --------------------
def main():
    start_time = time.time()
    print("🤖 Démarrage Tunisianet scraper (safe, multithreaded)...")

    product_links = get_product_links()
    if not product_links:
        print("Aucun lien trouvé, sortie.")
        return

    # resume: skip already scraped
    already = load_already_done_links(OUTPUT_FILE)
    links_to_process = [l for l in product_links if l not in already]
    print(f"→ {len(links_to_process)} liens à traiter (déjà {len(already)} présents dans {OUTPUT_FILE})")

    if not links_to_process:
        print("Rien à faire — tout est déjà scrapé.")
        return

    processed = safe_worker_write(links_to_process, OUTPUT_FILE)
    elapsed = (time.time() - start_time) / 60
    print(f"\n✅ Fini : {processed} produits enregistrés en {elapsed:.2f} min")
    # cleanup drivers
    quit_all_selenium_drivers()

if __name__ == "__main__":
    main()
