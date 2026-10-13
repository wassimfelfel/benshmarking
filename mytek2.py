#!/usr/bin/env python3
# mytek_pool.py - Version Pool Selenium partagé (Option A)
# Conserve la logique d'extraction / regex d'origine, optimisation temps d'exec.

import requests
from bs4 import BeautifulSoup
import csv
import re
import os
import time
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor
import threading
import queue
import signal
import sys

# Selenium imports (même que l'original)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# --- CONFIGURATION GLOBALE (tu peux ajuster) ---
BASE_URL = "https://www.mytek.tn"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}
OUTPUT_FILE = "produits_hybrides_pool.txt"
SITEMAP = f"{BASE_URL}/media/sitemap/sitemap.xml"
REQUEST_DELAY = 0.25          # délai entre requêtes catégories (requests)
MAX_WORKERS = 4              # nombre de drivers Selenium dans le pool (ajuste selon ta machine)
QUEUE_MAXSIZE = 0            # 0 => taille illimitée
CATEGORY_WORKERS = 4         # nombre de threads qui vont parser les catégories (requests) en parallèle (optionnel)

# lock pour écrire dans le fichier
file_lock = threading.Lock()

# Dédoublonnage par lien (une même fiche apparaît dans plusieurs catégories parentes/enfants)
DEDUPE_BY_LINK = True

# queue des produits à traiter dynamiquement
product_queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

# event d'arrêt propre
stop_event = threading.Event()

# --- Configuration du WebDriver (même options que ton script) ---
def get_chrome_options():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    return options

# ----------------------------------------------------------------------
# FONCTIONS (IDENTIQUES) - je laisse EXACTEMENT ton extract_price
# ----------------------------------------------------------------------
def extract_price(text):
    """
    Nettoie un texte de prix, en gérant le format tunisien (X,YYY DT ou X.YYY DT).
    Corrige la confusion entre séparateurs de milliers et décimaux.
    """
    if not text:
        return ""
    
    # Nettoyage de base
    text = text.replace("\u202f", "").replace("\xa0", "").strip()
    text = text.replace("DT", "").replace("dt", "").strip()
    text = text.replace(' ', '')
    
    # Chercher la partie numérique principale
    match = re.search(r'(\d+[.,]?\d*)', text)
    if not match:
        return ""
    
    raw_price = match.group(1)
    
    # Cas 1 : virgule comme décimale (ex: 8,900 -> 8.900)
    if ',' in raw_price and '.' not in raw_price:
        price_for_calc = raw_price.replace(',', '.')
    
    # Cas 2 : point comme décimale (ex: 8.900 -> 8.900)
    elif '.' in raw_price and ',' not in raw_price:
        price_for_calc = raw_price
    
    # Cas 3 : présence des deux (ex: 1.149,000 ou 1,149.000)
    else:
        # On suppose que le séparateur le plus à droite est la décimale
        if raw_price.rfind(',') > raw_price.rfind('.'):
            # virgule = décimale → on enlève les points des milliers
            price_for_calc = raw_price.replace('.', '').replace(',', '.')
        else:
            # point = décimale → on enlève les virgules des milliers
            price_for_calc = raw_price.replace(',', '')
    
    # Conversion en float
    try:
        price_float = float(price_for_calc)
    except ValueError:
        return raw_price + " DT"
    
    # Formatage à la tunisienne (virgule décimale, 3 chiffres millimes)
    formatted_price = f"{price_float:,.3f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    
    return f"{formatted_price} DT"

# ----------------------------------------------------------------------
# fetch_dynamic_details (identique au tien, légèrement protégée)
# ----------------------------------------------------------------------
def fetch_dynamic_details(product_url, driver):
    """
    Charge la page produit avec Selenium pour extraire UNIQUEMENT le prix et la disponibilité.
    Utilise le driver fourni (appartenant au pool).
    """
    try:
        driver.get(product_url)
    except WebDriverException:
        return {"price": "", "availability": ""}

    # Attendre courts instants — si timeout, on continue (fallback = page source)
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-info-price, div.stock"))
        )
    except TimeoutException:
        # on ne lève pas d'erreur: on continue avec le HTML obtenu
        pass
    except Exception:
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")

    # --- Extraction du Prix ---
    price_txt = ""
    meta_price = soup.select_one('meta[property="product:price:amount"]')
    if meta_price and meta_price.get("content"):
        price_txt = meta_price.get("content", "")
    
    if not price_txt:
        price_tag = soup.select_one("div.price-box span.final-price, div.price-box span.original-price, div.price-box span.price")
        if price_tag:
            price_txt = price_tag.get_text(strip=True)
            
    price = extract_price(price_txt)

    # --- Extraction de la Disponibilité ---
    availability = ""
    stock = soup.select_one("div.stock")
    if stock:
        availability = stock.get_text(strip=True) or stock.get("title", "")
    
    if isinstance(availability, str):
        if "instock" in availability.lower():
            availability = "En stock"
        elif "outofstock" in availability.lower():
            availability = "Épuisé"

    return {"price": price, "availability": availability}

# ----------------------------------------------------------------------
# Récupération des catégories (identique au tien)
# ----------------------------------------------------------------------
def get_categories():
    print(f"📄 Lecture du sitemap : {SITEMAP}")
    try:
        r = requests.get(SITEMAP, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"❌ Erreur lors de la récupération du sitemap: {e}")
        return []
    
    links = re.findall(r"<loc>(https://www\.mytek\.tn/[^<]+)</loc>", r.text)
    cats = [
        link for link in links
        if all(x not in link for x in ["product", "blog", "privacy", "catalog", "cms", "search"])
        and link.count("/") > 3
        and link.endswith(".html")
    ]
    seen = set()
    out = []
    for c in cats:
        clean_c = c.split("?")[0].split("#")[0]
        if clean_c not in seen:
            seen.add(clean_c)
            out.append(clean_c)
    print(f"✅ {len(out)} catégories candidates trouvées.")
    return out

def clean_category_name(category_url):
    path = urlparse(category_url).path
    name = path.strip("/").split("/")[-1]
    name = name.replace(".html", "").replace("-", " ").title()
    return name

# ----------------------------------------------------------------------
# Reprise : liens déjà présents dans le fichier de sortie (évite les doublons entre runs)
# ----------------------------------------------------------------------
def load_done_links(path):
    done = set()
    if not os.path.exists(path):
        return done
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                link = (row.get("Lien") or "").strip()
                if link.startswith("http"):
                    done.add(link)
    except Exception:
        pass
    return done

# ----------------------------------------------------------------------
# Producer: parcourt les catégories (requests) et push les produits dans la queue
# ----------------------------------------------------------------------
def producer_parse_categories(categories):
    """
    Parcours du sitemap categories et push dans product_queue:
    chaque item est un dict: {'cat':..., 'name':..., 'ref':..., 'link':...}
    """
    total_pushed = 0
    seen_links = load_done_links(OUTPUT_FILE) if DEDUPE_BY_LINK else set()
    if seen_links:
        print(f"[Producer] Reprise : {len(seen_links)} liens déjà présents dans {OUTPUT_FILE}, ils seront ignorés.")
    try:
        for category_url in categories:
            if stop_event.is_set():
                break
            cat_name = clean_category_name(category_url)
            url = category_url
            while url and not stop_event.is_set():
                try:
                    r = requests.get(url, headers=HEADERS, timeout=20)
                    r.raise_for_status()
                except Exception as e:
                    print(f" ! [Producer {cat_name}] Erreur requête {url} : {e}")
                    break

                soup = BeautifulSoup(r.text, "html.parser")
                # on garde les mêmes selecteurs que ton script original, pour être sûr
                cards = soup.select("div.card-body, div.product-item-info")

                # si aucun card trouvé, on peut aussi tenter fallback (optionnel)
                if not cards:
                    cards = soup.select("div.product-item, li.item.product.product-item")

                for card in cards:
                    if stop_event.is_set():
                        break
                    title_tag = card.select_one("h2.product-item-name a.product-item-link")
                    # fallback si structure différente
                    if not title_tag:
                        title_tag = card.select_one("a.product-item-link")

                    name = title_tag.get_text(strip=True) if title_tag else ""
                    link = urljoin(BASE_URL, title_tag["href"]) if title_tag and title_tag.has_attr("href") else ""

                    ref_tag = card.select_one("div.sku, span.sku")
                    ref = ref_tag.get_text(strip=True).strip("[]").strip() if ref_tag else ""

                    if DEDUPE_BY_LINK and link:
                        if link in seen_links:
                            continue
                        seen_links.add(link)

                    item = {
                        "Catégorie": cat_name,
                        "Nom": name,
                        "Référence": ref or "",
                        "Lien": link or ""
                    }

                    # push dans la queue (bloquant si plein)
                    try:
                        product_queue.put(item, timeout=5)
                        total_pushed += 1
                    except queue.Full:
                        # improbable car taille illimitée, mais on gère
                        print(" ! Queue pleine, attente...")
                        product_queue.put(item)

                # pagination next
                next_page = soup.select_one('a.page-link[aria-label="Next"]')
                if next_page and next_page.get("href"):
                    url = urljoin(BASE_URL, next_page.get("href"))
                    time.sleep(REQUEST_DELAY)
                else:
                    url = None

            # fin d'une catégorie
    except Exception as e:
        print(f"❌ Producer erreur: {e}")

    # signaler la fin: on placera autant de None que de workers pour arrêter proprement
    print(f"[Producer] Terminé — produits pushés: {total_pushed}")
    return total_pushed

# ----------------------------------------------------------------------
# Consumer: thread Selenium réutilisant un driver du pool
# ----------------------------------------------------------------------
def consumer_worker(worker_id, driver):
    """
    Boucle qui consomme depuis product_queue, utilise driver pour fetch_dynamic_details,
    et écrit le résultat final dans le fichier.
    """
    processed = 0
    print(f"[Consumer-{worker_id}] Démarré.")
    try:
        while not stop_event.is_set():
            try:
                item = product_queue.get(timeout=3)
            except queue.Empty:
                # queue vide momentanément -> continuer / vérifier stop_event
                if stop_event.is_set():
                    break
                else:
                    # si producer terminé mais queue vide, on attend un peu plus
                    continue

            # sentinelle : le producer en envoie une par consumer -> on la consomme et on quitte
            if item is None:
                product_queue.task_done()
                break

            link = item.get("Lien", "")
            price = ""
            dispo = ""
            if link:
                try:
                    details = fetch_dynamic_details(link, driver)
                    price = details.get("price", "")
                    dispo = details.get("availability", "")
                except Exception as e:
                    # protéger contre bug selenium isolé
                    print(f" ! [Consumer-{worker_id}] Erreur fetch {link}: {e}")
                    price = ""
                    dispo = ""

            row = {
                "Catégorie": item.get("Catégorie", ""),
                "Nom": item.get("Nom", ""),
                "Référence": item.get("Référence", ""),
                "Prix": price or "",
                "Disponibilité": dispo or "",
                "Lien": link or ""
            }

            # écrire en mode thread-safe
            with file_lock:
                with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
                    fieldnames = ["Catégorie", "Nom", "Référence", "Prix", "Disponibilité", "Lien"]
                    writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=',')
                    writer.writerow(row)
                    f.flush()

            processed += 1
            product_queue.task_done()

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print(f"[Consumer-{worker_id}] Terminé - traités: {processed}")

# ----------------------------------------------------------------------
# Gestion du signal Ctrl+C pour arrêt propre
# ----------------------------------------------------------------------
def _signal_handler(sig, frame):
    print("\n🔔 Signal d'interruption reçu. Arrêt propre en cours...")
    stop_event.set()
    # vider la queue en remettant des sentinels pour réveiller consumers
    try:
        for _ in range(MAX_WORKERS):
            product_queue.put(None)
    except Exception:
        pass


# ----------------------------------------------------------------------
# MAIN: orchestration producer + pool selenium consumers
# ----------------------------------------------------------------------
def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    start_time = time.time()
    print(f"🤖 Lancement du Scraper Mytek (Pool Selenium x{MAX_WORKERS})...")

    categories = get_categories()
    if not categories:
        print("Aucune catégorie trouvée. Terminé.")
        return

    # préparer fichier de sortie
    write_header = not os.path.exists(OUTPUT_FILE) or os.stat(OUTPUT_FILE).st_size == 0
    if write_header:
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            fieldnames = ["Catégorie", "Nom", "Référence", "Prix", "Disponibilité", "Lien"]
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=',')
            writer.writeheader()

    # --- Lancer le pool Selenium (drivers)
    drivers = []
    for i in range(MAX_WORKERS):
        try:
            drv = webdriver.Chrome(options=get_chrome_options())
            drivers.append(drv)
        except Exception as e:
            print(f"❌ Impossible d'initialiser Chrome driver #{i}: {e}")
            # fermer ceux déjà ouverts
            for d in drivers:
                try:
                    d.quit()
                except:
                    pass
            return

    # --- Lancer consumers (chaque consumer possède 1 driver)
    consumer_threads = []
    for i, drv in enumerate(drivers):
        t = threading.Thread(target=consumer_worker, args=(i+1, drv), daemon=True)
        t.start()
        consumer_threads.append(t)

    # --- Lancer le producer (parsing categories) dans un thread séparé
    # Wrapper : une fois le parsing terminé, on dépose une sentinelle par consumer
    def producer_parse_and_signal(categories):
        pushed = producer_parse_categories(categories)
        # après la fin du parsing, déposer sentinels (None) pour chaque consumer
        for _ in range(MAX_WORKERS):
            product_queue.put(None)
        return pushed

    producer_thread = threading.Thread(target=producer_parse_and_signal, args=(categories,), daemon=True)
    producer_thread.start()

    # --- Attendre la fin du producer puis la vidange de la queue
    try:
        # on join le producer
        while producer_thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _signal_handler(None, None)

    # Attendre les consumers : ils vident la queue puis s'arrêtent sur leur sentinelle.
    # (Après Ctrl+C, stop_event est posé : on laisse 30 s max pour fermer proprement.)
    for t in consumer_threads:
        while t.is_alive():
            t.join(timeout=30 if stop_event.is_set() else 1)
            if stop_event.is_set():
                break

    end_time = time.time()
    elapsed = end_time - start_time

    print("\n" + "="*60)
    print("🎉 Scraping terminé")
    print(f"Temps écoulé : {elapsed:.2f} secondes ({elapsed/60:.2f} minutes)")
    # compter lignes dans le fichier (excl header)
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
            count = max(0, len(lines) - 1)
    except Exception:
        count = "?"
    print(f"Total produits écrits : {count}")
    print(f"Fichier : {OUTPUT_FILE}")
    print("="*60)

if __name__ == "__main__":
    main()
