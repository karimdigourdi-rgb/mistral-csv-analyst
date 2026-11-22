"""Entrées et sorties des workflows d'extraction du catalogue Études en France."""

from pydantic import BaseModel, Field


class ProbeInput(BaseModel):
    detail_key: str | None = Field(
        default=None,
        description="Clé 'idFormation/idSite' à sonder en détail ; sinon le premier résultat de la page 1 est utilisé.",
    )
    page_size: int = 5


class ProbeResult(BaseModel):
    list_endpoint_ok: bool
    item_fields: list[str]
    type_diplome_values: dict[str, int]
    first_item: dict
    detail_key: str | None
    detail_endpoint_ok: bool
    detail_fields: list[str]
    detail_sample: dict


class ListExtractInput(BaseModel):
    page_size: int = Field(default=100, ge=1, le=100)
    start_page: int = 1
    max_pages: int = Field(default=0, description="Nombre maximal de pages à parcourir ; 0 = toutes.")
    type_diplomes: list[str] = Field(default_factory=list, description="Filtrage sur 'typeDiplome' (libellés exacts).")
    libelle_contains: list[str] = Field(default_factory=list, description="Filtrage complémentaire sur 'libelleFormation'.")
    delay_seconds: float = 1.0


class ListExtractResult(BaseModel):
    scanned_pages: int
    total_formations: int
    nb_pages_total: int
    matched_count: int
    type_diplome_compteur: dict[str, int]
    matched: list[dict]


class DetailsExtractInput(BaseModel):
    keys: list[str] = Field(description="Clés 'idFormation/idSite' à détailler.")
    chunk_size: int = Field(default=20, ge=1, le=50)
    delay_seconds: float = 0.5


class DetailsExtractResult(BaseModel):
    requested: int
    ok: int
    errors: dict[str, str]
    rows: list[dict]
