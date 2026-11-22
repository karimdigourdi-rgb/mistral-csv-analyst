"""Workflows d'extraction du catalogue Études en France (niveau master, management).

Trois workflows découverts automatiquement par le worker :
- ``efr_probe``          : sonde les deux endpoints (liste + détail) et documente leur schéma.
- ``efr_list_extract``   : parcourt la liste paginée, filtre par type de diplôme / libellé,
                           renvoie les formations correspondantes (+ compteurs des types vus).
- ``efr_details_extract``: passe un lot de clés "idFormation/idSite", renvoie les détails
                           (prérequis, frais, langue, domaine, contact...).

Toute la logique réseau vit dans les activités ; le corps de workflow reste déterministe.
"""

from __future__ import annotations

import html as html_module
import re
import time
from datetime import timedelta

import mistralai.workflows as workflows

from models import DetailsExtractInput, DetailsExtractResult, ListExtractInput, ListExtractResult, ProbeInput, ProbeResult

_LIST_PAGE_TIMEOUT = timedelta(seconds=180)
_DETAIL_TIMEOUT = timedelta(seconds=300)


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<br\s*/?>|</p>|</li>", "\n", value)
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(html_module.unescape(value).split())


def _project_list_item(item: dict) -> dict:
    keep = (
        "idFormation",
        "idSite",
        "libelleFormation",
        "typeDiplome",
        "libelleEtablissement",
        "libelleGroupement",
        "libelleSite",
        "libelleCatalogue",
        "valeurComNiveau",
        "procedureParallele",
    )
    return {k: item.get(k) for k in keep if k in item}


def _project_detail(item_key: str, detail: dict) -> dict:
    general = detail.get("formationInformationGenerale") or {}
    entrees = detail.get("formationsInformationsEntree") or []
    contact = detail.get("siteInformationContact") or {}
    fees = []
    dates = []
    for entree in entrees:
        if isinstance(entree, dict) and not entree.get("ferme"):
            if entree.get("coutFormation"):
                fees.append({"annee": entree.get("annee"), "cout": entree.get("coutFormation")})
            if entree.get("dateFin"):
                dates.append(entree["dateFin"])
    web = None
    if isinstance(general.get("UrlInfoSpecifique"), str):
        match = re.search(r"https?://\S+|www\.\S+", general["UrlInfoSpecifique"])
        if match:
            web = match.group(0).rstrip(".,;)")
    return {
        "cle": item_key,
        "pre_requis": _clean_text(general.get("preRequis")),
        "description": _clean_text(general.get("descriptionFormation"))[:800],
        "langue": _clean_text(general.get("langueEnseignement")),
        "domaine": _clean_text(general.get("domaine")),
        "sous_domaine": _clean_text(general.get("sousDomaine")),
        "frais": fees,
        "date_candidature_limite": max(dates) if dates else None,
        "site_web": web,
        "email": (contact.get("emailContactSite") or "").strip() or None,
        "adresse": _clean_text(contact.get("adresse")),
    }


def _merge_deltas(target: dict, delta: dict) -> None:
    for key, value in delta.items():
        target[key] = target.get(key, 0) + value


@workflows.activity(start_to_close_timeout=_LIST_PAGE_TIMEOUT, retry_policy_max_attempts=1)
async def scan_list_page(page: int, size: int, type_diplomes: list[str], libelle_contains: list[str], delay_seconds: float) -> dict:
    """Scanne une page de la liste et renvoie uniquement les items correspondant aux filtres."""
    from catalogue_client import PortalClient

    client = PortalClient()
    data = client.list_page(page, size, delay_seconds)
    items = data.get("donnees") or []
    nb_pages = int((data.get("pagination") or {}).get("nbPages") or 0)
    total = int((data.get("pagination") or {}).get("total") or 0)
    types: dict[str, int] = {}
    for item in items:
        label = item.get("typeDiplome") or "(inconnu)"
        types[label] = types.get(label, 0) + 1
    matched = []
    for item in items:
        if type_diplomes and (item.get("typeDiplome") or "(inconnu)") not in type_diplomes:
            continue
        if libelle_contains:
            haystack = (item.get("libelleFormation") or "").lower()
            if not any(needle.lower() in haystack for needle in libelle_contains):
                continue
        matched.append(_project_list_item(item))
    return {"nb_pages": nb_pages, "total": total, "matched": matched, "types": types}


@workflows.activity(start_to_close_timeout=_LIST_PAGE_TIMEOUT, retry_policy_max_attempts=1)
async def fetch_raw_list_page(page: int, size: int, delay_seconds: float) -> dict:
    """Récupère une page brute (champ par champ) pour documenter le schéma de l'API."""
    from catalogue_client import PortalClient

    client = PortalClient()
    data = client.list_page(page, size, delay_seconds)
    items = data.get("donnees") or []
    types: dict[str, int] = {}
    for item in items:
        label = item.get("typeDiplome") or "(inconnu)"
        types[label] = types.get(label, 0) + 1
    first = {}
    if items:
        first = _project_list_item(items[0])
        first["__all_fields_sample"] = {k: str(items[0].get(k))[:120] for k in items[0]}
    return {
        "nb_pages": int((data.get("pagination") or {}).get("nbPages") or 0),
        "total": int((data.get("pagination") or {}).get("total") or 0),
        "first_items": items[:3],
        "types": types,
    }


@workflows.activity(start_to_close_timeout=_DETAIL_TIMEOUT, retry_policy_max_attempts=1)
async def fetch_details_chunk(keys: list[str], delay_seconds: float) -> dict:
    """Télécharge les détails d'un lot de clés 'idFormation/idSite' et les projette à plat."""
    from catalogue_client import PortalClient, PortalError

    client = PortalClient()
    rows: list[dict] = []
    errors: dict[str, str] = {}
    for key in keys:
        id_formation, _, id_site = key.partition("/")
        try:
            detail = client.detail(id_formation, id_site, max_attempts=3)
        except PortalError as error:
            errors[key] = str(error)
            continue
        except Exception as error:  # noqa: BLE001 - garde la trace du détail manquant
            errors[key] = repr(error)
            continue
        rows.append(_project_detail(key, detail))
        if delay_seconds:
            time.sleep(delay_seconds)
    return {"rows": rows, "errors": errors}


@workflows.workflow.define(name="efr_probe")
class EfrProbe:
    @workflows.workflow.entrypoint
    async def run(self, input: ProbeInput) -> ProbeResult:
        raw = await fetch_raw_list_page(1, input.page_size, 0.0)
        first_items = raw.get("first_items") or []
        first = first_items[0] if first_items else {}
        key = input.detail_key
        if not key and first.get("idFormation"):
            key = f"{first['idFormation']}/{first['idSite']}"
        detail_ok = False
        detail_fields: list[str] = []
        detail_sample: dict = {}
        if key:
            chunk = await fetch_details_chunk([key], 0.0)
            if chunk["rows"]:
                detail_sample = chunk["rows"][0]
                detail_fields = sorted(detail_sample.keys())
                detail_ok = True
            else:
                detail_sample = {"erreur": chunk["errors"]}
        return ProbeResult(
            list_endpoint_ok=True,
            item_fields=sorted(first.keys()) if first else [],
            type_diplome_values=raw.get("types", {}),
            first_item=first,
            detail_key=key,
            detail_endpoint_ok=detail_ok,
            detail_fields=detail_fields,
            detail_sample=detail_sample,
        )


@workflows.workflow.define(name="efr_list_extract")
class EfrListExtract:
    @workflows.workflow.entrypoint
    async def run(self, input: ListExtractInput) -> ListExtractResult:
        matched: list[dict] = []
        type_counter: dict[str, int] = {}
        scanned = 0
        nb_pages_total = 0
        total_formations = 0
        page = input.start_page
        while True:
            if input.max_pages and scanned >= input.max_pages:
                break
            page_data = await scan_list_page(
                page, input.page_size, input.type_diplomes, input.libelle_contains, input.delay_seconds
            )
            nb_pages_total = max(nb_pages_total, page_data["nb_pages"])
            total_formations = max(total_formations, page_data["total"])
            _merge_deltas(type_counter, page_data["types"])
            matched.extend(page_data["matched"])
            scanned += 1
            if page >= page_data["nb_pages"]:
                break
            page += 1
        return ListExtractResult(
            scanned_pages=scanned,
            total_formations=total_formations,
            nb_pages_total=nb_pages_total,
            matched_count=len(matched),
            type_diplome_compteur=type_counter,
            matched=matched,
        )


@workflows.workflow.define(name="efr_details_extract")
class EfrDetailsExtract:
    @workflows.workflow.entrypoint
    async def run(self, input: DetailsExtractInput) -> DetailsExtractResult:
        rows: list[dict] = []
        errors: dict[str, str] = {}
        step = input.chunk_size
        for start in range(0, len(input.keys), step):
            chunk = input.keys[start : start + step]
            result = await fetch_details_chunk(chunk, input.delay_seconds)
            rows.extend(result["rows"])
            errors.update(result["errors"])
        return DetailsExtractResult(requested=len(input.keys), ok=len(rows), errors=errors, rows=rows)


WORKFLOWS = [EfrProbe, EfrListExtract, EfrDetailsExtract]
