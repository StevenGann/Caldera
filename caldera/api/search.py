"""Search, tag, and graph discovery endpoints."""

from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..config import Settings, get_settings
from ..core.search import keyword_search
from ..core.vault import Vault
from ..dependencies import get_semantic, get_vault, require_api_key
from ..models import NoteStub

router = APIRouter(prefix="/api/v1", tags=["search"], dependencies=[Depends(require_api_key)])


class SearchHit(BaseModel):
    path: str
    name: str
    snippet: str
    score: float
    match_type: str  # name | heading | body | tag | semantic | hybrid


class SearchStatus(BaseModel):
    keyword: str = "ready"
    semantic_enabled: bool = False
    state: str = "disabled"  # disabled | warming | ready | error
    model: str | None = None
    vectors: int = 0


class TagCount(BaseModel):
    tag: str
    count: int


class GraphNode(BaseModel):
    path: str
    name: str


class GraphEdge(BaseModel):
    source: str
    target: str


class Graph(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


@router.get("/search", response_model=list[SearchHit])
def search(
    q: str = Query(..., min_length=1),
    mode: str = Query("keyword", pattern="^(keyword|semantic|hybrid)$"),
    tag: str | None = Query(None),
    folder: str | None = Query(None),
    threshold: float | None = Query(None, ge=0, le=100),
    limit: int = Query(50, ge=1, le=500),
    vault: Vault = Depends(get_vault),
    settings: Settings = Depends(get_settings),
    semantic=Depends(get_semantic),
) -> list[SearchHit]:
    """Search notes. `mode=keyword` (default) is fuzzy; `mode=semantic` uses local
    embeddings when enabled (else keyword fallback or 409). Returns ranked hits with
    `score` and `match_type`."""
    if mode == "hybrid":
        # Hybrid fusion (Tier 3) is deferred; surface it honestly (not 501).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "hybrid_unavailable", "message": "hybrid search is not implemented"},
        )
    if mode == "semantic":
        if semantic is not None:
            allowed = set(vault.list_notes(folder=folder, tag=tag))
            hits = semantic.search(q, k=limit, threshold=(threshold or 0) / 100.0)
            out = []
            for h in hits:
                entry = vault.index.get(h.path)
                if entry and h.path in allowed:
                    out.append(SearchHit(path=h.path, name=entry.name, snippet=h.snippet,
                                         score=h.score, match_type="semantic"))
            return out
        if not settings.semantic_fallback:
            # Per RFC 9110, a configured-off feature is 409, not 501 (review m8).
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "semantic_disabled",
                        "message": "semantic search is not enabled"},
            )
        # else: fall through to keyword (hits report a keyword match_type — m16).

    candidates = vault.list_notes(folder=folder, tag=tag)
    hits = keyword_search(
        vault.index,
        q,
        candidates=candidates,
        limit=limit,
        threshold=threshold if threshold is not None else settings.search_fuzzy_threshold,
    )
    return [
        SearchHit(path=h.path, name=h.name, snippet=h.snippet, score=h.score,
                  match_type=h.match_type)
        for h in hits
    ]


@router.get("/search/status", response_model=SearchStatus)
def search_status(
    settings: Settings = Depends(get_settings), semantic=Depends(get_semantic)
) -> SearchStatus:
    if semantic is None:
        return SearchStatus(keyword="ready", semantic_enabled=False, state="disabled")
    return SearchStatus(
        keyword="ready",
        semantic_enabled=True,
        state="ready",
        model=semantic.store.model,
        vectors=semantic.store.count(),
    )


@router.get("/tags", response_model=list[TagCount])
def list_tags(vault: Vault = Depends(get_vault)) -> list[TagCount]:
    """All tags with note counts (alphabetical)."""
    return [TagCount(tag=t, count=c) for t, c in vault.index.all_tags().items()]


@router.get("/tags/{tag:path}", response_model=list[NoteStub])
def notes_for_tag(tag: str, vault: Vault = Depends(get_vault)) -> list[NoteStub]:
    """Notes carrying a given tag."""
    out = []
    for path in vault.index.notes_with_tag(tag):
        entry = vault.index.get(path)
        if entry:
            out.append(NoteStub(path=entry.path, name=entry.name, tags=entry.parsed.tags))
    return out


@router.get("/graph", response_model=Graph)
def graph(vault: Vault = Depends(get_vault)) -> Graph:
    """Whole-vault link graph: nodes (notes) and edges (resolved links)."""
    nodes = [GraphNode(path=e.path, name=e.name) for e in vault.index.notes.values()]
    edges: list[GraphEdge] = []
    for path, entry in vault.index.notes.items():
        for link in entry.links:
            if link.resolved and link.target:
                edges.append(GraphEdge(source=path, target=link.target))
    return Graph(nodes=nodes, edges=edges)
