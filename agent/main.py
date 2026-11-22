"""Analyste de données conversationnel — Mistral Agents SDK.

L'utilisateur colle un lien vers un CSV de son Google Drive et pose ses
questions en langage naturel. Cinq outils fixes et déclarés
``model_access="direct"`` : aucune exécution de code arbitraire, le modèle
n'appelle que ces fonctions typées.

Entrypoint déclaré dans pyproject.toml : ``main:agent``.
"""

from __future__ import annotations

import csv
import io
import re
import urllib.request
from typing import Annotated, Literal

from pydantic import Field

from mistralai import agents

_store: dict[str, dict[str, list[dict]]] = {}
_current: dict[str, str] = {}

_OPERATEURS = Literal[
    "égale",
    "différente",
    "supérieure",
    "supérieure_ou_égale",
    "inférieure",
    "inférieure_ou_égale",
    "contient",
    "commence_par",
    "dans",
    "entre",
    "est_vide",
    "non_vide",
]

_AGGREGATS = Literal["compte", "somme", "moyenne", "médiane", "min", "max", "valeurs_uniques"]

_DRIVE_ID_PATTERNS = (
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"/document/d/([A-Za-z0-9_-]{10,})"),
)

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; csv-analyst/1.0)"}


def _is_csv_text(source: str) -> bool:
    return "\n" in source and ("," in source or ";" in source or "\t" in source)


def _drive_download_url(source: str) -> str | None:
    for pattern in _DRIVE_ID_PATTERNS:
        m = pattern.search(source)
        if m:
            return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    if "://" in source:
        return source
    return None


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=60) as response:
        final = response.geturl()
        raw = response.read()
    text = raw.decode("utf-8-sig", errors="replace")
    if "<html" in text[:500].lower():
        if "drive.google.com" in final or "docs.google.com" in final:
            big = "https://drive.google.com/uc?export=download&confirm=t&id="
            fid = ""
            for pattern in _DRIVE_ID_PATTERNS:
                mm = pattern.search(url)
                if mm:
                    fid = mm.group(1)
                    break
            if fid:
                req2 = urllib.request.Request(f"{big}{fid}", headers=_HEADERS)
                with urllib.request.urlopen(req2, timeout=120) as response2:
                    text = response2.read().decode("utf-8-sig", errors="replace")
        if "<html" in text[:500].lower():
            raise agents.ToolError(
                "L'URL renvoie une page HTML, pas un CSV. "
                "Partagez le fichier Drive en 'Tous les utilisateurs disposant du lien' ou collez le contenu CSV."
            )
    return text


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    t = value.strip().replace(" ", "").replace("\u00a0", "")
    if not t:
        return None
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", t):
        return float(t.replace(",", "."))
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?%", t):
        return float(t.rstrip("%").replace(",", ".")) / 100
    return None


def _get_rows(session_id: str, dataset: str | None) -> tuple[str, list[dict]]:
    session = _store.get(session_id)
    if not session:
        raise agents.ToolError(
            "Aucun jeu de données chargé dans cette session. "
            "Commencez par charger_csv avec le lien du fichier ou son contenu."
        )
    name = dataset or _current.get(session_id)
    if not name or name not in session:
        raise agents.ToolError(
            f"Jeu de données introuvable ({dataset!r}). Jeux disponibles : {sorted(session)}."
        )
    return name, session[name]


def _parse_csv(text: str, name: str) -> tuple[list[dict], list[str]]:
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for row in reader:
        cleaned = {k: v for k, v in row.items() if k is not None}
        rows.append(cleaned)
    if not rows:
        raise agents.ToolError("CSV vide ou illisible : aucune ligne de données détectée.")
    columns = list(rows[0].keys())
    return rows, columns


def _column_stats(rows: list[dict], columns: list[str]) -> list[dict]:
    stats = []
    for col in columns:
        values = [r.get(col) for r in rows]
        non_null = [v for v in values if v is not None and str(v).strip() != ""]
        numbers = [n for v in non_null if (n := _as_number(v)) is not None]
        kind = "numérique" if non_null and len(numbers) > len(non_null) / 2 else "texte"
        entry: dict = {
            "colonne": col,
            "type": kind,
            "renseignees": len(non_null),
            "vides": len(rows) - len(non_null),
        }
        if numbers:
            entry["min"] = min(numbers)
            entry["max"] = max(numbers)
            entry["moyenne"] = round(sum(numbers) / len(numbers), 4)
        elif non_null:
            exemples = [str(v) for v in non_null[:3]]
            entry["exemples"] = exemples
        stats.append(entry)
    return stats


def _match(row: dict, colonne: str, op: str, valeur: object, valeur_max: object) -> bool:
    cell = row.get(colonne)
    vide = cell is None or str(cell).strip() == ""
    if op == "est_vide":
        return vide
    if op == "non_vide":
        return not vide
    if vide:
        return False
    num = _as_number(cell)
    text = str(cell).strip()
    if op == "contient":
        return str(valeur).strip().lower() in text.lower()
    if op == "commence_par":
        return text.lower().startswith(str(valeur).strip().lower())
    if op in {"égale", "différente", "supérieure", "supérieure_ou_égale", "inférieure", "inférieure_ou_égale"}:
        target = valeur
        if isinstance(target, (int, float)) or (isinstance(target, str) and _as_number(target) is not None):
            if num is None:
                return False
            t = float(_as_number(target))  # type: ignore[arg-type]
            n = num
        else:
            t = str(target).strip().lower()
            n = text.lower()  # type: ignore[assignment]
        if op == "égale":
            return n == t
        if op == "différente":
            return n != t
        if op == "supérieure":
            return n > t
        if op == "supérieure_ou_égale":
            return n >= t
        if op == "inférieure":
            return n < t
        return n <= t
    if op == "dans":
        wanted = valeur if isinstance(valeur, list) else [valeur]
        parsed = [float(w) if _as_number(w) is not None else str(w).strip().lower() for w in wanted]
        probe = num if num is not None else text.lower()
        return probe in parsed
    if op == "entre":
        lo, hi = valeur, valeur_max
        if num is None:
            return False
        lo_n = _as_number(lo) if lo is not None else None
        hi_n = _as_number(hi) if hi is not None else None
        if lo_n is not None and num < lo_n:
            return False
        if hi_n is not None and num > hi_n:
            return False
        return lo_n is not None or hi_n is not None
    raise agents.ToolError(f"Opérateur inconnu : {op!r}.")


@agents.tool(model_access="direct")
def charger_csv(
    ctx: agents.ToolCallbackContext,
    /,
    source: Annotated[str, Field(description="Lien Google Drive partage du fichier CSV, ou le contenu CSV brut.")],
    nom: Annotated[str, Field(description="Nom court du jeu de données, réutilisable dans les autres outils.")] = "dataset",
) -> dict:
    """Charge un fichier CSV à partir d'un lien Google Drive (ou du texte CSV) et l'enregistre pour toute la session."""
    session_id = ctx.session.id
    if _is_csv_text(source):
        text = source
        resolved = "contenu collé"
    else:
        url = _drive_download_url(source)
        if url is None:
            raise agents.ToolError(
                "Source non reconnue : fournissez un lien Google Drive (https://drive.google.com/...) ou collez le contenu CSV."
            )
        text = _fetch_text(url)
        resolved = url
    rows, columns = _parse_csv(text, nom)
    session = _store.setdefault(session_id, {})
    session[nom] = rows
    _current[session_id] = nom
    return {
        "dataset": nom,
        "source": resolved,
        "lignes": len(rows),
        "colonnes": len(columns),
        "schema": _column_stats(rows[:500], columns),
        "echantillon": rows[:3],
    }


@agents.tool(model_access="direct")
def decrire_colonnes(
    ctx: agents.ToolCallbackContext,
    /,
    dataset: Annotated[str | None, Field(description="Nom du dataset (par défaut : le dernier chargé).")] = None,
) -> dict:
    """Décrit les colonnes du dataset chargé : type, taux de remplissage, étendue pour les colonnes numériques."""
    name, rows = _get_rows(ctx.session.id, dataset)
    columns = list(rows[0].keys())
    return {"dataset": name, "lignes": len(rows), "colonnes": _column_stats(rows, columns)}


@agents.tool(model_access="direct")
def filtrer_lignes(
    ctx: agents.ToolCallbackContext,
    /,
    colonne: Annotated[str, Field(description="Nom exact de la colonne à filtrer.")],
    operateur: _OPERATEURS,
    valeur: object = None,
    valeur_max: object = None,
    limite: Annotated[int, Field(description="Nombre maximal de lignes renvoyées (par défaut 20).")] = 20,
) -> dict:
    """Filtre les lignes du dataset selon une condition et renvoie le total plus un échantillon."""
    name, rows = _get_rows(ctx.session.id, None)
    if colonne not in rows[0]:
        raise agents.ToolError(
            f"Colonne {colonne!r} absente. Colonnes disponibles : {list(rows[0].keys())}."
        )
    matched = [r for r in rows if _match(r, colonne, operateur, valeur, valeur_max)]
    return {
        "dataset": name,
        "total_correspondances": len(matched),
        "echantillon": matched[: limite],
        "condition": {"colonne": colonne, "operateur": operateur, "valeur": valeur, "valeur_max": valeur_max},
    }


@agents.tool(model_access="direct")
def agreger(
    ctx: agents.ToolCallbackContext,
    /,
    colonnes_groupe: Annotated[list[str], Field(description="Colonnes de regroupement (group by).")],
    colonne_valeur: Annotated[str | None, Field(description="Colonne numérique à agréger (inutile pour 'compte').")] = None,
    aggregat: _AGGREGATS = "compte",
    limite: Annotated[int, Field(description="Nombre maximal de groupes renvoyés.")] = 50,
) -> dict:
    """Calcule des agrégats group-by (compte, somme, moyenne, médiane, min, max, valeurs uniques) et renvoie le tableau."""
    name, rows = _get_rows(ctx.session.id, None)
    for col in colonnes_groupe:
        if col not in rows[0]:
            raise agents.ToolError(f"Colonne {col!r} absente. Colonnes disponibles : {list(rows[0].keys())}.")
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(row.get(col) for col in colonnes_groupe)
        groups.setdefault(key, []).append(row)
    result: list[dict] = []
    for key, members in groups.items():
        line: dict = dict(zip(colonnes_groupe, key))
        line["effectif"] = len(members)
        if aggregat != "compte":
            if colonne_valeur is None:
                raise agents.ToolError("colonne_valeur est requise pour un agrégat autre que 'compte'.")
            numbers = [n for r in members if (n := _as_number(r.get(colonne_valeur))) is not None]
            if aggregat == "somme":
                line[colonne_valeur] = round(sum(numbers), 4) if numbers else None
            elif aggregat == "moyenne":
                line[colonne_valeur] = round(sum(numbers) / len(numbers), 4) if numbers else None
            elif aggregat == "médiane":
                if numbers:
                    s = sorted(numbers)
                    m = len(s) // 2
                    line[colonne_valeur] = (s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2)
                else:
                    line[colonne_valeur] = None
            elif aggregat == "min":
                line[colonne_valeur] = min(numbers) if numbers else None
            elif aggregat == "max":
                line[colonne_valeur] = max(numbers) if numbers else None
            elif aggregat == "valeurs_uniques":
                line[colonne_valeur] = len({r.get(colonne_valeur) for r in members})
        result.append(line)
    if aggregat != "compte" and colonne_valeur is not None:
        orderable = [r for r in result if r.get(colonne_valeur) is not None]
        result = sorted(orderable, key=lambda r: r[colonne_valeur], reverse=True)
        count = len(result)
        return {"dataset": name, "groupes": count, "tableau": result[: limite]}
    count = len(result)
    return {"dataset": name, "groupes": count, "tableau": result[: limite]}


INSTRUCTIONS = """Tu es « Analyste CSV », un analyste de données conversationnel.

Mode d'emploi :
1. L'utilisateur te donne un lien vers un fichier CSV de son Google Drive (ou colle le contenu) puis pose ses questions en langage naturel.
2. Charge le fichier avec l'outil `charger_csv` avant la première analyse. Le jeu de données et l'intégralité de la conversation mémorisés pour la session : ne redemande jamais une information dé déjà fournie, et continue sur le dernier dataset chargé pour les questions suivantes.
3. Décris d'abord la structure avec `decrire_colonnes` si la question porte sur des colonnes que tu n'as pas encore vues.

Règles strictes :
- Réponds dans la langue de l'utilisateur (français par défaut).
- Chaque chiffre de tes réponses DOIT provenir d'un résultat d'outil. N'invente jamais une valeur, même approximative.
- Pour tout stat par groupe : `agreger` (compte, somme, moyenne, médiane, min, max, valeurs uniques).
- Pour sélectionner des lignes : `filtrer_lignes`, une condition par appel ; combine mentalement les appels successifs.
- Filtre numériques : passe les opérateurs `>, >=, <, <=` avec valeur numérique ; `entre` prend valeur + valeur_max.
- Si un filtre renvoie 0 ligne, vérifie les valeurs réelles de la colonne (casse, accent, unité) via `decrire_colonnes` puis reformule le filtre ; si le problème persiste, dis-le clairement.
- Présente les résultats en tableaux Markdown lisibles, montants et effectifs exacts, en précisant le dataset utilisé.
- Tu n'as pas d'exécution de code : uniquement les 4 outils fixes chargés dans la session.
"""

agent = agents.Agent(
    model="mistral-large-4",
    name="csv-analyst",
    instructions=INSTRUCTIONS,
    tools=[charger_csv, decrire_colonnes, filtrer_lignes, agreger],
)


async def demo() -> None:
    state = await agent.run("Voici mon CSV: https://drive.google.com/file/d/XXXXXXXXXXXXXXXXX/view — décris ses colonnes.")
    print(state.text)


if __name__ == "__main__":
    import asyncio

    asyncio.run(demo())
