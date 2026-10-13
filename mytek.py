#!/usr/bin/env python3
# mytek_scraper_final_v10_hybride_safe.py - Optimisation Hybride et Sûreté

import requests
from bs4 import BeautifulSoup
import csv
import re
import os
import time
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor # Pour l'exécution parallèle
import threading

# --- IMPORTS SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# --- CONFIGURATION GLOBALE ---
BASE_URL = "https://www.mytek.tn"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
OUTPUT_FILE = "produits_hybrides.txt" # Nouveau nom de fichier
SITEMAP = f"{BASE_URL}/media/sitemap/sitemap.xml"
REQUEST_DELAY = 0.5 # Délai entre les pages de catégorie (requests)
MAX_WORKERS = 4 # Nombre de navigateurs Chrome à lancer en parallèle (ajustez si besoin)

# Verrou pour garantir que seule une thread à la fois écrit dans le fichier
file_lock = threading.Lock() 

# --- Configuration du WebDriver ---
def get_chrome_options():
    """Configure les options pour le mode headless de Chrome."""
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")
    return options

# ----------------------------------------------------------------------
# FONCTIONS UTILITAIRES 
# ----------------------------------------------------------------------

def get_categories():
    """Récupère les URLs de catégories depuis le sitemap XML."""
    # (Fonction inchangée, utilise requests)
    print(f"📄 Lecture du sitemap : {SITEMAP}")
    # ... [Code de la fonction get_categories] ...
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
    """Extrait le nom lisible de la catégorie depuis l'URL."""
    path = urlparse(category_url).path
    name = path.strip("/").split("/")[-1]
    name = name.replace(".html", "").replace("-", " ").title()
    return name


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
# FONCTIONS D'EXTRACTION AVEC SELENIUM (Prix et Disponibilité)
# ----------------------------------------------------------------------

def fetch_dynamic_details(product_url, driver):
    """
    Charge la page produit avec Selenium pour extraire UNIQUEMENT le prix et la disponibilité.
    """
    try:
        driver.get(product_url)
    except WebDriverException:
        return {"price": "", "availability": ""}
        
    # Attendre que le prix ou l'indicateur de disponibilité soit chargé (max 10s)
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.product-info-price, div.stock"))
        )
    except TimeoutException:
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
    
    if "instock" in availability.lower():
        availability = "En stock"
    elif "outofstock" in availability.lower():
        availability = "Épuisé"

    return {"price": price, "availability": availability}


# ----------------------------------------------------------------------
# FONCTION WORKER PARALLÈLE
# ----------------------------------------------------------------------

def parse_category_and_write(category_url):
    """
    Worker principal. Initialise un driver, parcourt la catégorie 
    et écrit dans le fichier global.
    """
    start_time = time.time()
    cat_name = clean_category_name(category_url)
    url = category_url
    total_processed = 0
    driver = None # Déclaration initiale du driver

    print(f"\n[Worker {cat_name}] Démarré.")

    # 1. Initialiser le Driver pour ce worker
    try:
        driver = webdriver.Chrome(options=get_chrome_options())
    except Exception as e:
        print(f"❌ Worker échec d'initialisation du driver pour {cat_name}: {e}")
        return 0 
        
    # 2. Boucle de pagination (requests)
    while url:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
        except Exception as e:
            print(f"    ! [Worker {cat_name}] Erreur requête {url} : {e}")
            break

        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("div.card-body, div.product-item-info") 

        # 3. Extraction des produits de la page (SANS tenir le verrou :
        #    le chargement Selenium est lent et bloquerait les autres workers)
        rows = []
        for card in cards:
            # --- Extraction STATIQUE (requests) ---
            title_tag = card.select_one("h2.product-item-name a.product-item-link")
            name = title_tag.get_text(strip=True) if title_tag else ""
            link = urljoin(BASE_URL, title_tag["href"]) if title_tag and title_tag.has_attr("href") else ""

            ref_tag = card.select_one("div.sku, span.sku")
            ref = ref_tag.get_text(strip=True).strip("[]").strip() if ref_tag else ""

            # --- Extraction DYNAMIQUE (Selenium) ---
            price = ""
            dispo = ""
            if link:
                details = fetch_dynamic_details(link, driver)
                price = details.get("price", "")
                dispo = details.get("availability", "")

            rows.append({
                "Catégorie": cat_name,
                "Nom": name,
                "Référence": ref or "",
                "Prix": price or "",
                "Disponibilité": dispo or "",
                "Lien": link or ""
            })

        # 4. Écriture des résultats de la page (verrou court, une seule fois par page)
        if rows:
            with file_lock:
                with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
                    fieldnames = ["Catégorie", "Nom", "Référence", "Prix", "Disponibilité", "Lien"]
                    writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=',')
                    writer.writerows(rows)
                    f.flush()
            total_processed += len(rows)
        
        # Next page (pagination)
        next_page = soup.select_one('a.page-link[aria-label="Next"]')
        if next_page and next_page.get("href"):
            url = urljoin(BASE_URL, next_page.get("href"))
            time.sleep(REQUEST_DELAY)
        else:
            url = None
            
    # 5. Fermer le driver
    if driver:
        try:
            driver.quit()
        except Exception:
            pass
        
    end_time = time.time()
    elapsed_time = end_time - start_time
    
    print(f"  ✅ [Worker {cat_name}] Terminé. {total_processed} produits. Temps: {elapsed_time:.2f} secondes.")
    return total_processed

# ----------------------------------------------------------------------
# FONCTION main (Orchestration Parallèle et Gestion Sûre)
# ----------------------------------------------------------------------

def main():
    start_time = time.time()
    print(f"🤖 Lancement du Scraper Mytek (Mode Hybride/Parallèle avec {MAX_WORKERS} workers)...")
    
    categories = get_categories()
    if not categories:
        print("Aucune catégorie trouvée. Terminé.")
        return

    # Préparation du fichier de sortie
    write_header = not os.path.exists(OUTPUT_FILE) or os.stat(OUTPUT_FILE).st_size == 0
    
    if write_header:
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            fieldnames = ["Catégorie", "Nom", "Référence", "Prix", "Disponibilité", "Lien"]
            writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=',')
            writer.writeheader()
            
    print(f"📝 Les résultats seront écrits dans : {OUTPUT_FILE}")
    
    total_products = 0
    
    try:
        # Utiliser ThreadPoolExecutor pour l'exécution parallèle
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # map exécute la fonction pour chaque URL de catégorie
            results = executor.map(parse_category_and_write, categories)
            
            for result in results:
                total_products += result
                
    except KeyboardInterrupt:
        # Cette partie est gérée par l'Executor qui s'arrêtera et fermera les threads
        print("\nInterruption demandée par l'utilisateur (Ctrl+C). Arrêt des workers...")

    end_time = time.time()
    elapsed_time = end_time - start_time
    
    print("\n" + "="*70)
    print(f"🎉 **Scraping Terminé**")
    print(f"Total de produits ajoutés : {total_products}")
    print(f"Temps total écoulé : {elapsed_time:.2f} secondes (environ {elapsed_time / 60:.2f} minutes)")
    print(f"Le fichier de sortie est : {OUTPUT_FILE}")
    print("="*70)


if __name__ == "__main__":
    main()