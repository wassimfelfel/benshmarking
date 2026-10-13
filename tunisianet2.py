#!/usr/bin/env python3
# tunisianet_scraper_fixed.py
# Version corrigée : lecture des sitemaps sans parser XML (regex CDATA), stable et finale

import requests
from bs4 import BeautifulSoup
import csv
import re
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import signal
import sys

# ---------------- CONFIG ----------------
BASE_URL = "https://www.tunisianet.com.tn"
OUTPUT_FILE = "produits_tunisianet_fixed.csv"
MISSED_FILE = "missed_links.txt"
TIMEOUT = 15
MAX_WORKERS = 12            # adapté i5-10400
REQUEST_DELAY = 0.03
SITEMAP_PREFIXES = range(1, 6)  # essais 1_fr .. 5_fr (ajuste si besoin)
SITEMAP_MAX = 100

csv_lock = threading.Lock()
missed_links = []
stop_flag = False

# Ctrl+C handler
def handle_sigint(sig, frame):
    global stop_flag
    print("\n🛑 Ctrl+C détecté — arrêt demandé...")
    stop_flag = True

signal.signal(signal.SIGINT, handle_sigint)
signal.signal(signal.SIGTERM, handle_sigint)

# ---------------- sitemap extraction (regex safe) ----------------
def get_all_product_links():
    """
    Parcourt les sitemaps prefix_fr_i_sitemap.xml (prefix=1..N),
    capture les URLs .html même dans <![CDATA[ ... ]]> et retourne une liste unique.
    """
    print("📄 Lecture des sitemaps (regex CDATA, pas de parser XML requis)...")
    all_links = []
    for prefix in SITEMAP_PREFIXES:
        for i in range(SITEMAP_MAX):
            if stop_flag:
                break
            sitemap_url = f"{BASE_URL}/{prefix}_fr_{i}_sitemap.xml"
            try:
                r = requests.get(sitemap_url, timeout=TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 404:
                    # 404 : ce préfixe n'existe pas (i=0) ou on a dépassé le dernier sitemap
                    break
                r.raise_for_status()
                text = r.text

                # première tentative : CDATA inside <loc>
                links = re.findall(r"<!\[CDATA\[\s*(https://www\.tunisianet\.com\.tn/[^]]+?\.html)\s*\]\]>", text, re.IGNORECASE)
                # fallback : <loc>https://... .html</loc>
                if not links:
                    links = re.findall(r"<loc>\s*(https://www\.tunisianet\.com\.tn/[^<]+?\.html)\s*</loc>", text, re.IGNORECASE)

                if links:
                    print(f" → {len(links)} liens trouvés dans {prefix}_fr_{i}_sitemap.xml")
                    all_links.extend(links)

            except requests.RequestException as e:
                # log et continuer
                print(f" ⚠️ Erreur requête {sitemap_url}: {e}")
                break
            time.sleep(0.2)
        if stop_flag:
            break

    # Nettoyage / dédoublonnage
    cleaned = []
    seen = set()
    for l in all_links:
        if stop_flag:
            break
        l2 = re.sub(r'[\?#].*$', '', l).strip()
        if not l2:
            continue
        # garder les URLs qui semblent être des produits : contiennent '/<digits>-' ou finissent par -nom.html
        # on accepte aussi autres formes pour ne rien manquer
        if l2 not in seen:
            seen.add(l2)
            cleaned.append(l2)

    print(f"✅ Total {len(cleaned)} liens produits uniques trouvés.")
    return cleaned

# ---------------- helpers extraction ----------------
def normalize_text(s):
    if not s:
        return ""
    return s.replace("\u202f", "").replace("\xa0", " ").strip()

def extract_price_from_text(txt):
    if not txt:
        return ""
    s = txt.replace("DT", "").replace("dt", "")
    s = s.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    # capture number with thousands/decimals
    m = re.search(r"(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+)", s)
    if not m:
        return ""
    raw = m.group(1)
    # normalize decimal
    if raw.count(",") > 0 and raw.count(".") == 0:
        val = raw.replace(",", ".")
    elif raw.count(".") > 0 and raw.count(",") == 0:
        val = raw
    else:
        # both present: rightmost is decimal
        if raw.rfind(",") > raw.rfind("."):
            val = raw.replace(".", "").replace(",", ".")
        else:
            val = raw.replace(",", "")
    try:
        f = float(val)
        # format to 3 decimals tunisian style: 1.234,000 -> '1.234,000 DT'
        res = f"{f:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".") + " DT"
        return res
    except Exception:
        return raw + " DT"

# ---------------- parse product ----------------
def parse_product(url):
    if stop_flag:
        return None
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, timeout=TIMEOUT, headers=headers)
    except Exception as e:
        # marquer comme manqué
        missed_links.append(url)
        return None

    if r.status_code != 200:
        missed_links.append(url)
        return None

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # nom
    name_tag = soup.select_one("h1[itemprop='name'], h1.product-title, h1")
    name = normalize_text(name_tag.get_text()) if name_tag else ""

    # catégorie : avant-dernier du breadcrumb schema.org
    cat = "Non spécifié"
    breadcrumb_items = soup.select("ol[itemtype='http://schema.org/BreadcrumbList'] li span[itemprop='name']")
    if breadcrumb_items and len(breadcrumb_items) >= 2:
        cat = normalize_text(breadcrumb_items[-2].get_text())
    else:
        # fallback nav.breadcrumb
        crumbs = soup.select("nav.breadcrumb a span[itemprop='name'], nav.breadcrumb a")
        if crumbs:
            cat = normalize_text(crumbs[-1].get_text())

    # référence
    ref = ""
    t = soup.select_one("span.product-reference")
    if t:
        ref = normalize_text(t.get_text()).strip("[]")
    else:
        t2 = soup.select_one("span[itemprop='sku']")
        if t2:
            ref = normalize_text(t2.get_text()).strip("[]")
        else:
            meta = soup.select_one('meta[itemprop="sku"], meta[property="product:retailer_item_id"]')
            if meta and meta.get("content"):
                ref = meta.get("content").strip()

    # prix
    price = ""
    # vérifier plusieurs sélecteurs possibles
    price_selectors = [
        "div.product-price-and-shipping span.price",
        "span[itemprop='price']",
        "span#our_price_display",
        "span.price"
    ]
    for sel in price_selectors:
        tag = soup.select_one(sel)
        if tag:
            p = extract_price_from_text(tag.get_text())
            if p:
                price = p
                break
    # fallback meta price
    if not price:
        meta_price = soup.select_one('meta[itemprop="price"], meta[property="product:price:amount"], meta[itemprop="price:amount"]')
        if meta_price and meta_price.get("content"):
            try:
                v = float(meta_price.get("content"))
                price = f"{v:,.3f}".replace(",", "X").replace(".", ",").replace("X", ".") + " DT"
            except Exception:
                price = meta_price.get("content") + " DT"

    # disponibilité
    availability = ""
    stock_sel = soup.select_one("#stock_availability span, #stock_availability p, .product-quantities span, .in-stock, .out-of-stock, .later-stock")
    if stock_sel:
        availability = normalize_text(stock_sel.get_text())
    else:
        body = html.lower()
        if "sur commande" in body or "later-stock" in body:
            availability = "Sur commande"
        elif "en stock" in body or "in-stock" in body:
            availability = "En stock"
        elif "rupture" in body or "outofstock" in body or "épuisé" in body:
            availability = "Rupture de stock"
        else:
            availability = "Non précisée"

    # retourner la ligne (même si price vide)
    return {
        "Catégorie": cat,
        "Nom": name,
        "Référence": ref,
        "Prix": price,
        "Disponibilité": availability,
        "Lien": url
    }

# ---------------- write row (thread-safe) ----------------
def write_row(writer, f, data):
    with csv_lock:
        writer.writerow(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass

# ---------------- main ----------------
def main():
    start = time.time()
    print("🤖 Démarrage Tunisianet scraper (fixed)...")

    links = get_all_product_links()
    if not links:
        print("Aucun lien trouvé, sortie.")
        return

    # resume support: skip already present links
    done = set()
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as ff:
                for line in ff:
                    parts = line.rstrip("\n").split(",")
                    if parts:
                        link = parts[-1].strip()
                        if link.startswith("http"):
                            done.add(link)
        except Exception:
            done = set()
    links_to_do = [l for l in links if l not in done]
    print(f"→ {len(links_to_do)} liens à traiter (déjà {len(done)} présents)")

    # prepare csv writer
    write_header = not os.path.exists(OUTPUT_FILE) or os.stat(OUTPUT_FILE).st_size == 0
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        fieldnames = ["Catégorie", "Nom", "Référence", "Prix", "Disponibilité", "Lien"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
            f.flush()

        processed = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exe:
            futures = {exe.submit(parse_product, url): url for url in links_to_do}
            try:
                for fut in as_completed(futures):
                    if stop_flag:
                        break
                    url = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        print(f"⚠️ Erreur product {url}: {e}")
                        missed_links.append(url)
                        continue
                    if res:
                        write_row(writer, f, res)
                        processed += 1
                        if processed % 100 == 0:
                            elapsed = time.time() - start
                            avg = elapsed / processed
                            remaining = (len(links_to_do) - processed) * avg / 60
                            print(f"→ {processed}/{len(links_to_do)} produits enregistrés - ~{remaining:.1f} min restants")
                    time.sleep(REQUEST_DELAY)
            except KeyboardInterrupt:
                print("\n🛑 Interruption clavier ! Arrêt demandé.")
                # stop_flag already set by signal handler
            finally:
                exe.shutdown(wait=False, cancel_futures=True)

    # write missed links
    if missed_links:
        try:
            with open(MISSED_FILE, "w", encoding="utf-8") as mf:
                for l in set(missed_links):
                    mf.write(l + "\n")
            print(f"⚠️ {len(set(missed_links))} liens échoués enregistrés dans {MISSED_FILE}")
        except Exception:
            pass

    elapsed = time.time() - start
    print(f"\n✅ Terminé : {processed}/{len(links_to_do)} produits enregistrés en {elapsed/60:.2f} min")

if __name__ == "__main__":
    main()
