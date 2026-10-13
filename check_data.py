#!/usr/bin/env python3
"""check_data.py - contrôle qualité (lecture seule) des CSV produits par les scrapers.

Usage :
    python check_data.py produits_tunisianet_fixed.csv produits_hybrides_pool.txt
    python check_data.py produits_hybrides_pool.txt --export propre.csv   # version dédoublonnée

Ne modifie jamais les fichiers d'entrée.
"""
import argparse
import csv
import sys
from collections import Counter

PLACEHOLDER_PRICES = {"1,000 DT"}  # prix "bidon" observé sur Mytek (fiche sans vrai prix)


def check(path):
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print(f"{path} : vide")
        return rows, []

    links = Counter(r["Lien"] for r in rows)
    dupes = sum(c - 1 for c in links.values() if c > 1)
    no_price = sum(1 for r in rows if not r["Prix"].strip())
    placeholder = sum(1 for r in rows if r["Prix"].strip() in PLACEHOLDER_PRICES)
    no_ref = sum(1 for r in rows if not r["Référence"].strip())
    dispo = Counter(r["Disponibilité"] for r in rows)

    print(f"\n=== {path} ===")
    print(f"Lignes                : {len(rows)}  ({len(links)} liens uniques)")
    print(f"Doublons (même lien)  : {dupes}")
    print(f"Prix manquants        : {no_price}")
    print(f"Prix suspects (1 DT)  : {placeholder}")
    print(f"Références manquantes : {no_ref}")
    print(f"Catégories distinctes : {len({r['Catégorie'] for r in rows})}")
    print("Disponibilités        : " + ", ".join(f"{k or '(vide)'}={v}" for k, v in dispo.most_common()))
    if len(dispo) == 1:
        print("  ⚠️  une seule valeur de disponibilité : vérifier le sélecteur de stock")

    seen, unique = set(), []
    for r in rows:
        if r["Lien"] not in seen:
            seen.add(r["Lien"])
            unique.append(r)
    return rows, unique


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--export", help="écrit le premier fichier dédoublonné par lien dans ce CSV")
    args = ap.parse_args()

    first_unique = None
    for i, p in enumerate(args.files):
        try:
            _, unique = check(p)
        except OSError as e:
            sys.exit(f"Impossible de lire {p}: {e}")
        if i == 0:
            first_unique = unique

    if args.export and first_unique:
        with open(args.export, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(first_unique[0].keys()))
            w.writeheader()
            w.writerows(first_unique)
        print(f"\n→ {len(first_unique)} lignes uniques écrites dans {args.export}")


if __name__ == "__main__":
    main()
