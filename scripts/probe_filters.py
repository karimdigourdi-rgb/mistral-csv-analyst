#!/usr/bin/env python3
"""Sonde l'API du catalogue : structure des filtres et capacités de filtrage du POST liste.

Objectif : déterminer si le POST /parcours-api/public/catalogue/formations accepte des
filtres (domaine « Management, gestion, finances et commerce », type de diplôme) afin de
ne télécharger que les fiches détail réellement dans le périmètre.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_catalogue import API, HEADERS, Portal  # noqa: E402

CANDIDATE_KEYS = [
    "domainesFormations", "idDomainesFormations", "domaineFormationIds", "domainesFormationsId",
    "domaineIds", "idDomaine", "idDomaines", "domaines", "idDomaineEtude",
    "typesFormation", "idTypesFormation", "typeFormationIds", "typesFormationsId",
    "idTypeFormations", "typesFormationIds", "secteursFormations", "idSecteurs",
]
TYPE_IDS = [56, 75, 84, 97, 69, 76, 91, 66]


def post_json(portal: Portal, body: dict, max_attempts: int = 3) -> dict:
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            request = urllib.request.Request(
                API,
                data=json.dumps(body).encode("utf-8"),
                method="POST",
                headers={**HEADERS, "Content-Type": "application/json", "X-XSRF-TOKEN": portal.token},
            )
            raw = portal.opener.open(request, timeout=60).read().decode("utf-8")
            if raw.lstrip().startswith("{"):
                return json.loads(raw)
            last_error = RuntimeError("réponse hors JSON (maintenance)")
        except Exception as error:  # noqa: BLE001
            last_error = error
        portal.refresh_token()
        time.sleep(10 + attempt * 20)
    raise last_error  # type: ignore[misc]


def total_of(pagination: dict) -> object:
    for key in ("nbTotalFormations", "total", "nbFormations", "count"):
        if key in pagination:
            return pagination[key]
    return None


def main() -> int:
    portal = Portal()
    portal.refresh_token()

    raw = portal.opener.open(
        urllib.request.Request(API + "/filters/formations", headers=HEADERS), timeout=30
    ).read().decode("utf-8")
    if not raw.lstrip().startswith("{"):
        print("filters: réponse hors JSON (maintenance)")
        return 1
    filters = json.loads(raw)
    print("FILTERS KEYS:", sorted(filters.keys()) if isinstance(filters, dict) else type(filters).__name__)

    management: dict[str, object] = {}
    if isinstance(filters, dict):
        for group_key, entries in filters.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if group_key == "typesFormations":
                    print("TYPE:", entry.get("id"), "=", entry.get("libelle"))
                if "management" in str(entry.get("libelle", "")).lower():
                    management[group_key] = entry
                    print("MATCH:", group_key, "=", json.dumps(entry, ensure_ascii=False))
                    for sub_key in ("sousDomaines", "sousDomains", "children", "sous_domaines"):
                        for sub in entry.get(sub_key) or []:
                            if isinstance(sub, dict):
                                print("  SUB:", json.dumps(sub, ensure_ascii=False)[:200])

    baseline = post_json(portal, {"page": 1, "size": 10})
    base_pagination = baseline.get("pagination", {})
    base_total = total_of(base_pagination)
    print(f"BASELINE pagination={base_pagination}")
    donnees = baseline.get("donnees") or [{}]
    print("ITEM KEYS:", sorted(donnees[0].keys()))

    mgmt_id = None
    for entry in management.values():
        mgmt_id = entry.get("id")
        break

    trials: list[tuple[str, dict]] = []
    if mgmt_id is not None:
        for key in CANDIDATE_KEYS:
            trials.append((f"{key}=[mgmt:{mgmt_id}]", {"page": 1, "size": 10, key: [mgmt_id]}))
    for key in CANDIDATE_KEYS:
        if "type" in key.lower():
            trials.append((f"{key}=[8 types]", {"page": 1, "size": 10, key: TYPE_IDS}))

    for label, body in trials:
        try:
            result = post_json(portal, body)
        except Exception as error:  # noqa: BLE001
            print(f"VARIANT {label}: ERREUR {error!r}")
            time.sleep(2)
            portal.refresh_token()
            continue
        pagination = result.get("pagination", {})
        total = total_of(pagination)
        flag = "*** DIFFERENT ***" if total != base_total else "inchangé"
        print(f"VARIANT {label}: {flag} total={total} nbPages={pagination.get('nbPages')}")
        time.sleep(2)
    print("probe: fini")
    return 0


if __name__ == "__main__":
    sys.exit(main())
