#!/usr/bin/env python3
"""Extraction du catalogue Études en France — niveau master, domaine Management.

Périmètre strict :
- types de diplôme : Master (professionnel / recherche / indifférencié), MSc,
  Mastère spécialisé, Mastère en sciences (MSc), MBA,
  Diplôme d'école de commerce visé de niveau bac + 4 ou 5 ;
- domaine : « Management, gestion, finances et commerce » (fiche détail).

Sorties dans resultats/ :
- formations_management_master.csv (UTF-8 BOM, séparateur ';', prêt pour Excel)
- filters.json     : réponse GET de l'API des filtres (sonde)
- list.json        : liste paginée complète
- stats.json       : compteurs et diagnostics
- details.jsonl    : cache des fiches détail (reprise possible)

Endpoints (validés) :
- POST /parcours-api/public/catalogue/formations  {"page": N, "size": 100} + XSRF
- GET  /parcours-api/public/catalogue/formations/filters/formations
- GET  /parcours-api/public/catalogue/formations/{idFormation}/site/{idSite}
"""

from __future__ import annotations

import concurrent.futures as cf
import csv
import datetime as dt
import html as html_module
import http.cookiejar
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "resultats"
OUT.mkdir(exist_ok=True)

BASE = "https://etudesenfrance.diplomatie.gouv.fr"
API = BASE + "/parcours-api/public/catalogue/formations"
FICHE_URL = BASE + "/catalogue-formations/formation/{idf}/site/{ids}"

TYPE_DIPLOMES = [
    "Master indifférencié (recherche et professionnel)",
    "Master professionnel",
    "Master recherche",
    "Master of Science (MSc)",
    "Mastère spécialisé",
    "Mastère en sciences (MSc)",
    "MBA",
    "Diplôme d'école de commerce visé de niveau bac + 4 ou 5",
]

DOMAIN = "management, gestion, finances et commerce"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; efr-catalogue-extract/1.0)", "Accept": "application/json"}
DETAIL_THREADS = 4
DETAIL_DELAY = 0.25
LIST_DELAY = 1.0

LOCK = threading.Lock()


class Portal:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.token = ""

    def refresh_token(self) -> str:
        for url in (BASE + "/catalogue-formations", API + "/filters/formations"):
            try:
                self.opener.open(urllib.request.Request(url, headers=HEADERS), timeout=30).read()
            except Exception:
                continue
        self.token = next((c.value for c in self.jar if c.name == "XSRF-TOKEN"), "")
        return self.token

    def list_page(self, page: int, size: int = 100, max_attempts: int = 12) -> dict:
        body = json.dumps({"page": page, "size": size}).encode("utf-8")
        last_error = "?"
        for attempt in range(max_attempts):
            if not self.token:
                self.refresh_token()
            try:
                request = urllib.request.Request(
                    API,
                    data=body,
                    method="POST",
                    headers={**HEADERS, "Content-Type": "application/json", "X-XSRF-TOKEN": self.token},
                )
                raw = self.opener.open(request, timeout=60).read().decode("utf-8")
                if raw.lstrip().startswith("{"):
                    return json.loads(raw)
                last_error = "page de maintenance (trop rapide ?)"
            except Exception as error:  # noqa: BLE001
                last_error = repr(error)
            self.refresh_token()
            time.sleep(min(20 + attempt * 20, 180))
        raise RuntimeError(f"list page {page} failed: {last_error}")

    def detail(self, id_formation: str, id_site: str, max_attempts: int = 8) -> dict:
        url = f"{API}/{id_formation}/site/{id_site}"
        last_error = "?"
        for attempt in range(max_attempts):
            try:
                request = urllib.request.Request(url, headers=HEADERS)
                raw = self.opener.open(request, timeout=40).read().decode("utf-8")
                if raw.lstrip().startswith("{"):
                    return json.loads(raw)
                last_error = "maintenance"
            except urllib.error.HTTPError as error:
                if error.code in (404, 410):
                    return {"__error": error.code}
                last_error = str(error.code)
            except Exception as error:  # noqa: BLE001
                last_error = type(error).__name__
            time.sleep(min(20 + attempt * 20, 180))
        return {"__error": last_error}


def clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<br\s*/?>|</p>|</li>", "\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(html_module.unescape(value).split())


def detail_row(item: dict, detail: dict) -> dict:
    key = f"{item['idFormation']}/{item['idSite']}"
    ok = "formationInformationGenerale" in detail and "__error" not in detail
    general = (detail.get("formationInformationGenerale") or {}) if ok else {}
    entrees = (detail.get("formationsInformationsEntree") or []) if ok else []
    contact = (detail.get("siteInformationContact") or {}) if ok else {}
    frais = []
    date_limite = None
    for entree in entrees:
        if not isinstance(entree, dict):
            continue
        if not entree.get("ferme"):
            if entree.get("coutFormation"):
                frais.append((entree.get("annee") or "", entree.get("coutFormation")))
            if entree.get("dateFin"):
                d = entree["dateFin"]
                date_limite = d if date_limite is None or d > date_limite else date_limite
    web = None
    if isinstance(general.get("UrlInfoSpecifique"), str):
        match = re.search(r"https?://\S+|www\.\S+", general["UrlInfoSpecifique"])
        if match:
            web = match.group(0).rstrip(".,;)")
    return {
        "id_formation": item.get("idFormation"),
        "id_site": item.get("idSite"),
        "url_fiche": FICHE_URL.format(idf=item["idFormation"], ids=item["idSite"]),
        "type_de_diplome": item.get("typeDiplome"),
        "formation": clean_text(item.get("libelleFormation")),
        "etablissement": clean_text(item.get("libelleEtablissement")),
        "groupement": clean_text(item.get("libelleGroupement")),
        "site": clean_text(item.get("libelleSite")),
        "catalogue": item.get("libelleCatalogue"),
        "niveau": item.get("valeurComNiveau"),
        "domaine": clean_text(general.get("domaine")) if ok else "",
        "sous_domaine": clean_text(general.get("sousDomaine")) if ok else "",
        "langue_enseignement": clean_text(general.get("langueEnseignement")) if ok else "",
        "pre_requis": clean_text(general.get("preRequis")) if ok else "",
        "frais_formation": " | ".join(f"{a}: {c}" for a, c in frais) if frais else "",
        "date_limite_candidature": date_limite or "",
        "admission_parallele": bool(item.get("procedureParallele")) or any(
            e.get("procedureParallele") for e in entrees if isinstance(e, dict)
        ),
        "email_contact": (contact.get("emailContactSite") or "").strip() if ok else "",
        "adresse": clean_text(contact.get("adresse")) if ok else "",
        "site_web": web or "",
        "detail_ok": bool(ok),
    }


def main() -> int:
    portal = Portal()
    start = time.monotonic()

    # Sonde : la réponse des filtres sert aussi à diagnostiquer.
    try:
        filters_raw = portal.opener.open(
            urllib.request.Request(API + "/filters/formations", headers=HEADERS), timeout=30
        ).read().decode("utf-8")
        if filters_raw.lstrip().startswith("{"):
            (OUT / "filters.json").write_text(filters_raw, encoding="utf-8")
            print("filters: OK", flush=True)
        else:
            print("filters: page hors JSON (maintenance) — poursuivi sans", flush=True)
    except Exception as error:  # noqa: BLE001
        print(f"filters: échec ({error!r}) — poursuivi sans", flush=True)
    portal.refresh_token()

    # 1) Liste paginée complète, avec reprise.
    part = OUT / "list.partial.json"
    if part.exists():
        state = json.loads(part.read_text(encoding="utf-8"))
    else:
        state = {"page": 1, "items": [], "nb_pages": 0}
    page, items = state["page"], state["items"]
    nb_pages = state["nb_pages"]
    print(f"list: reprise à la page {page} ({len(items)} items en cache)", flush=True)
    while True:
        try:
            data = portal.list_page(page)
        except RuntimeError as error:
            (OUT / "list.partial.json").write_text(
                json.dumps({"page": page, "items": items, "nb_pages": nb_pages}, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"ERREUR liste: {error} — état sauvegardé, relancez le job pour reprendre.", flush=True)
            return 1
        items.extend(data.get("donnees") or [])
        nb_pages = int((data.get("pagination") or {}).get("nbPages") or nb_pages)
        (OUT / "list.partial.json").write_text(
            json.dumps({"page": page + 1, "items": items, "nb_pages": nb_pages}, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"list page {page}/{nb_pages} (total items {len(items)})", flush=True)
        if page >= nb_pages:
            break
        page += 1
        time.sleep(LIST_DELAY)
    (OUT / "list.json").write_text(
        json.dumps({"fetched": dt.date.today().isoformat(), "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )
    part.unlink(missing_ok=True)
    print(f"list: {len(items)} formations", flush=True)

    # 2) Périmètre strict : types de diplôme.
    types = {}
    for item in items:
        label = item.get("typeDiplome") or "(inconnu)"
        types[label] = types.get(label, 0) + 1
    keep_items = []
    seen = set()
    for item in items:
        if (item.get("typeDiplome") or "(inconnu)") not in TYPE_DIPLOMES:
            continue
        key = f"{item.get('idFormation')}/{item.get('idSite')}"
        if key in seen:
            continue
        seen.add(key)
        keep_items.append(item)

    # 3) Détails (cache jsonl, 4 threads, délai de politesse).
    detail_cache: dict[str, dict] = {}
    detail_file = OUT / "details.jsonl"
    if detail_file.exists():
        for line in detail_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            detail_cache[record["k"]] = record["d"]
    todo = [
        item for item in keep_items
        if f"{item['idFormation']}/{item['idSite']}" not in detail_cache
    ]
    print(f"details: {len(todo)} fiches à télécharger ({len(detail_cache)} en cache)", flush=True)
    keys_todo = [(item["idFormation"], item["idSite"]) for item in todo]
    index = {"n": 0}

    with detail_file.open("a", encoding="utf-8") as handle, cf.ThreadPoolExecutor(DETAIL_THREADS) as pool:
        def work(pair: tuple[str, str]) -> tuple[str, dict]:
            id_formation, id_site = pair
            result = portal.detail(id_formation, id_site)
            time.sleep(DETAIL_DELAY)
            with LOCK:
                index["n"] += 1
                if index["n"] % 250 == 0:
                    print(f"details {index['n']}/{len(keys_todo)}", flush=True)
            return f"{id_formation}/{id_site}", result

        for record_n, (key, detail_data) in enumerate(pool.map(work, keys_todo)):
            handle.write(json.dumps({"k": key, "d": detail_data}, ensure_ascii=False) + "\n")
            if record_n % 500 == 0:
                handle.flush()

    for line in detail_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            detail_cache[record["k"]] = record["d"]

    # 4) Fusion.
    rows = []
    for item in keep_items:
        key = f"{item['idFormation']}/{item['idSite']}"
        rows.append(detail_row(item, detail_cache.get(key, {})))

    # 5) Filtre domaine Management (fiche détail), avec comptage des exclus.
    in_scope = [
        row for row in rows
        if row["domaine"] and DOMAIN in row["domaine"].strip().lower()
    ]
    no_domain = sum(1 for row in rows if not row["domaine"])
    print(f"scope: {len(in_scope)} formations Management (hors périmètre: {len(rows) - len(in_scope)}, dont {no_domain} sans domaine lu)", flush=True)

    columns = list(in_scope[0].keys()) if in_scope else [
        "id_formation", "id_site", "url_fiche", "type_de_diplome", "formation", "etablissement",
        "groupement", "site", "catalogue", "niveau", "domaine", "sous_domaine", "langue_enseignement",
        "pre_requis", "frais_formation", "date_limite_candidature", "admission_parallele",
        "email_contact", "adresse", "site_web", "detail_ok",
    ]
    with (OUT / "formations_management_master.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(in_scope)

    stats = {
        "date": dt.date.today().isoformat(),
        "types_catalogue": types,
        "types_perimetre": {t: types.get(t, 0) for t in TYPE_DIPLOMES},
        "formations_perimetre_types": len(rows),
        "formations_management": len(in_scope),
        "sans_domaine_lu": no_domain,
        "details_erreurs": sorted(k for k, v in detail_cache.items() if "__error" in v),
        "duree_secondes": round(time.monotonic() - start, 1),
    }
    (OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if k != "types_catalogue"}, ensure_ascii=False, indent=2), flush=True)
    print(f"terminé en {stats['duree_secondes']} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
