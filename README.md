# Benchmark prix – scrapers Mytek & Tunisianet

Scrapers Python qui récupèrent le catalogue de deux boutiques tunisiennes d'informatique
([Mytek](https://www.mytek.tn) et [Tunisianet](https://www.tunisianet.com.tn)) et l'exportent en CSV,
pour comparer les prix, références et disponibilités.

Colonnes produites (identiques pour les deux sites) :

| Catégorie | Nom | Référence | Prix | Disponibilité | Lien |
|---|---|---|---|---|---|

Les prix sont au format tunisien avec millimes : `1.234,000 DT`.

---

## Contenu du dépôt

| Fichier | Rôle |
|---|---|
| `tunisianet2.py` | **Scraper Tunisianet recommandé.** Sitemaps → pages produit via `requests` uniquement (rapide, pas de Chrome). Reprise automatique, liste des liens échoués. |
| `tunisianet.py` | Variante Tunisianet avec **repli Selenium** si le prix/la référence n'est pas dans le HTML brut. Écriture par lots. |
| `mytek2.py` | **Scraper Mytek recommandé.** Architecture producteur / consommateurs : un thread parcourt les catégories (`requests`), un pool de Chrome headless lit prix et stock sur chaque fiche. |
| `mytek.py` | Première version Mytek : un Chrome par catégorie. Conservée pour référence (plus lente que `mytek2.py`). |
| `check_data.py` | Contrôle qualité en lecture seule d'un CSV (doublons, prix manquants, prix suspects…). |
| `produits_tunisianet_fixed.csv` | Exemple de sortie de `tunisianet2.py` (3 383 produits, 208 catégories). |
| `produits_hybrides_pool.txt` | Exemple de sortie de `mytek2.py` (CSV malgré l'extension `.txt`). |

## Pourquoi deux techniques ?

- **Tunisianet** : prix et stock sont présents dans le HTML → `requests` + BeautifulSoup suffit.
- **Mytek** : prix et disponibilité sont chargés dynamiquement → il faut un vrai navigateur (Selenium). Les listes de catégories, elles, restent lisibles avec `requests`, d'où l'approche « hybride ».

---

## Installation

Prérequis : Python 3.9+ et **Google Chrome** installé (uniquement pour `mytek*.py` et le repli de `tunisianet.py`).
Selenium ≥ 4.6 télécharge lui-même le bon chromedriver.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

## Utilisation

```bash
python tunisianet2.py     # → produits_tunisianet_fixed.csv  (+ missed_links.txt si des pages échouent)
python mytek2.py          # → produits_hybrides_pool.txt
python check_data.py produits_tunisianet_fixed.csv produits_hybrides_pool.txt
```

`Ctrl+C` arrête proprement les scrapers : les lignes déjà écrites sont conservées et les Chrome sont fermés.

### Reprise après interruption

`tunisianet.py`, `tunisianet2.py` et `mytek2.py` relisent le fichier de sortie au démarrage et **ignorent les liens déjà présents**.
Pour repartir de zéro, supprimez (ou renommez) le fichier de sortie.
`mytek.py` n'a pas de reprise : il ajoute à la suite du fichier existant.

## Configuration

Les réglages sont des constantes en haut de chaque script :

| Constante | Défaut | Effet |
|---|---|---|
| `OUTPUT_FILE` | selon le script | Fichier CSV de sortie |
| `MAX_WORKERS` | 12 (Tunisianet) / 4 (Mytek) | Threads, ou nombre de Chrome pour Mytek. Chaque Chrome consomme beaucoup de RAM : baissez cette valeur sur une petite machine |
| `REQUEST_DELAY` | 0.03 s (Tunisianet) / 0.25 s (Mytek) | Pause entre requêtes |
| `TIMEOUT` / `REQUEST_TIMEOUT` | 15 s / 10 s | Délai maximal d'une requête |
| `DEDUPE_BY_LINK` (`mytek2.py`) | `True` | Une fiche présente dans plusieurs catégories n'est écrite qu'une fois (la première catégorie rencontrée est conservée) |

---

## Limites connues (à connaître avant d'exploiter les données)

- **Mytek – prix « 1,000 DT »** : environ 8 % des lignes de l'exemple (116 sur 1 482) affichent exactement `1,000 DT`, surtout des accessoires tablette. Ce sont très probablement des prix de remplacement du site, pas de vrais prix : à filtrer avant toute comparaison (`check_data.py` les signale).
- **Mytek – couverture** : l'exemple ne couvre que 8 catégories (consommables, toners, sacoches, protections d'écran…), pas tout le catalogue.
- **Tunisianet – disponibilité** : dans l'exemple, les 3 383 produits sont « En stock ». Une boutique réelle a normalement des ruptures : vérifiez le sélecteur de stock sur quelques fiches avant de vous fier à cette colonne.
- **Tunisianet – filtrage des liens** : `tunisianet2.py` accepte toute URL `.html` des sitemaps ; une page non-produit éventuelle est donc traitée comme un produit.
- **Sélecteurs CSS** : ils dépendent du HTML actuel des deux sites. Si un site change son thème, les champs concernés deviendront vides.
- Le fichier `produits_hybrides_pool.txt` est un CSV ; seule l'extension prête à confusion.

## Utilisation responsable

Ces scripts interrogent des sites tiers : respectez leurs conditions d'utilisation et leur `robots.txt`, gardez des délais raisonnables entre requêtes et évitez de lancer plusieurs scrapes complets à la suite. Les données restent la propriété des boutiques.

---

## Changelog des améliorations

La logique d'extraction (sélecteurs, parsing des prix, format de sortie) n'a **pas** été modifiée. Corrections apportées :

- `mytek2.py`
  - **Fin de run bloquée** : `queue.join()` ne se terminait jamais (sentinelle remise dans la file sans `task_done`). Les consommateurs s'arrêtent maintenant sur leur sentinelle et le script se termine.
  - **Doublons** supprimés par lien (415 doublons dans l'exemple) et **reprise** entre deux exécutions.
  - Gestionnaires de signaux déplacés dans `main()` (le module est importable), code mort supprimé.
- `mytek.py`
  - Le verrou d'écriture était tenu pendant tous les chargements Selenium d'une page, ce qui **sérialisait les workers** : il n'entoure plus que l'écriture.
- `tunisianet.py`
  - Variable `text` potentiellement non définie dans le repli Selenium (`UnboundLocalError`) ; les pages 404/410 ne lancent plus Chrome pour rien.
- `tunisianet2.py`
  - Écriture de l'en-tête CSV aussi si le fichier existe mais est vide ; branche 404 simplifiée.
- Tous : `except:` nus remplacés par `except Exception:` (Ctrl+C n'est plus avalé), fins de ligne converties en LF.
- Nouveau : `check_data.py`, `requirements.txt`, `.gitignore`, `.gitattributes`, ce README.

## Idées pour la suite

- Fusionner les scripts dans un package avec une interface commune (`--site mytek|tunisianet`).
- Utiliser `logging` plutôt que `print`, et paramétrer les constantes par ligne de commande.
- Script de comparaison Mytek ↔ Tunisianet par référence produit.
