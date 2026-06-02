"""Search, tag, and graph discovery endpoints."""

from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Depends, Query

from ..core.vault import Vault
from ..dependencies import get_vault, require_api_key
from ..models import NoteStub

router = APIRouter(prefix="/api/v1", tags=["search"], dependencies=[Depends(require_api_key)])


class SearchHit(BaseModel):
    path: str
    name: str
    snippet: str
    score: int


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


def _snippet(body: str, needle: str, width: int = 120) -> str:
    idx = body.lower().find(needle.lower())
    if idx < 0:
        return body[:width].strip()
    start = max(0, idx - width // 2)
    return ("…" if start else "") + body[start : start + width].strip() + "…"


@router.get("/search", response_model=list[SearchHit])
def search(
    q: str = Query(..., min_length=1),
    tag: str | None = Query(None),
    folder: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    vault: Vault = Depends(get_vault),
) -> list[SearchHit]:
    candidates = vault.list_notes(folder=folder, tag=tag)
    ql = q.lower()
    hits: list[SearchHit] = []
    for path in candidates:
        entry = vault.index.get(path)
        if not entry:
            continue
        body = entry.parsed.content
        count = body.lower().count(ql)
        name_hit = ql in entry.name.lower()
        if count or name_hit:
            hits.append(
                SearchHit(
                    path=entry.path,
                    name=entry.name,
                    snippet=_snippet(body, q),
                    score=count + (5 if name_hit else 0),
                )
            )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


@router.get("/tags", response_model=list[TagCount])
def list_tags(vault: Vault = Depends(get_vault)) -> list[TagCount]:
    return [TagCount(tag=t, count=c) for t, c in vault.index.all_tags().items()]


@router.get("/tags/{tag:path}", response_model=list[NoteStub])
def notes_for_tag(tag: str, vault: Vault = Depends(get_vault)) -> list[NoteStub]:
    out = []
    for path in vault.index.notes_with_tag(tag):
        entry = vault.index.get(path)
        if entry:
            out.append(NoteStub(path=entry.path, name=entry.name, tags=entry.parsed.tags))
    return out


@router.get("/graph", response_model=Graph)
def graph(vault: Vault = Depends(get_vault)) -> Graph:
    nodes = [GraphNode(path=e.path, name=e.name) for e in vault.index.notes.values()]
    edges: list[GraphEdge] = []
    for path, entry in vault.index.notes.items():
        for link in entry.links:
            if link.resolved and link.target:
                edges.append(GraphEdge(source=path, target=link.target))
    return Graph(nodes=nodes, edges=edges)
