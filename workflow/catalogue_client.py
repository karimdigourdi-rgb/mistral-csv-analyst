"""Client HTTP (stdlib, sans dépendance) pour l'API publique du catalogue Études en France.

Endpoints confirmés :
- POST /parcours-api/public/catalogue/formations  {"page": N, "size": 100}
  → {"donnees": [...], "pagination": {"nbPages": ...}} (jeton XSRF requis)
- GET  /parcours-api/public/catalogue/formations/{idFormation}/site/{idSite}
  → JSON de détail (formationInformationGenerale, formationsInformationsEntree, ...)

Le portail affiche une page de maintenance si on le sollicite trop vite : prévoir
des retries avec backoff et un délai entre les appels.
"""

from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.request

BASE = "https://etudesenfrance.diplomatie.gouv.fr"
API = BASE + "/parcours-api/public/catalogue/formations"

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; efr-catalogue-worker/1.0)", "Accept": "application/json"}

_MAINTENANCE_MSG = (
    "Le portail Études en France répond par sa page de maintenance (limitation de débit). "
    "Relancez plus tard ou augmentez delay_seconds."
)


class PortalError(RuntimeError):
    pass


class PortalMaintenance(PortalError):
    pass


class PortalClient:
    def __init__(self) -> None:
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))

    def xsrf_token(self) -> str:
        for url in (BASE + "/catalogue-formations", API + "/filters/formations"):
            try:
                self._opener.open(urllib.request.Request(url, headers=_HEADERS), timeout=30).read()
            except Exception:
                continue
        return next((c.value for c in self._jar if c.name == "XSRF-TOKEN"), "")

    def list_page(self, page: int, size: int, delay_seconds: float = 1.0, max_attempts: int = 5) -> dict:
        token = self.xsrf_token()
        body = json.dumps({"page": page, "size": size}).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                request = urllib.request.Request(
                    API,
                    data=body,
                    method="POST",
                    headers={**_HEADERS, "Content-Type": "application/json", "X-XSRF-TOKEN": token},
                )
                raw = self._opener.open(request, timeout=60).read().decode("utf-8")
                if raw.lstrip().startswith("{"):
                    data = json.loads(raw)
                    if delay_seconds:
                        time.sleep(delay_seconds)
                    return data
                last_error = PortalMaintenance(_MAINTENANCE_MSG)
            except PortalMaintenance:
                raise
            except Exception as error:  # noqa: BLE001 - retry any transient failure
                last_error = error
            token = self.xsrf_token()
            time.sleep(min(20 + attempt * 20, 120))
        raise PortalError(f"list page {page} failed: {last_error!r}") from last_error

    def detail(self, id_formation: str, id_site: str, max_attempts: int = 3) -> dict:
        url = f"{API}/{id_formation}/site/{id_site}"
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                request = urllib.request.Request(url, headers=_HEADERS)
                raw = self._opener.open(request, timeout=40).read().decode("utf-8")
                if raw.lstrip().startswith("{"):
                    return json.loads(raw)
                last_error = PortalMaintenance(_MAINTENANCE_MSG)
            except urllib.error.HTTPError as error:
                if error.code in (404, 410):
                    raise PortalError(f"detail {id_formation}/{id_site} introuvable (HTTP {error.code})") from error
                last_error = error
            except Exception as error:  # noqa: BLE001
                last_error = error
            time.sleep(min(20 + attempt * 20, 90))
        raise PortalError(f"detail {id_formation}/{id_site} failed: {last_error!r}") from last_error
