"""
routers/similarity.py
POST /api/dfmea/similarity/suggest

Uses Amazon Titan embeddings (cosine similarity) to suggest which lower
and higher functions should be connected to each focus function.

Replaces the old keyword-matching approach.  No LLM calls — pure maths.
"""

import os
import json
import requests
import numpy as np
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

# ── Titan embedding config ────────────────────────────────────────────────────

REGION      = os.environ.get("BEDROCK_REGION", "ap-south-1")
TITAN_MODEL = "amazon.titan-embed-text-v2:0"
TITAN_URL   = f"https://bedrock-runtime.{REGION}.amazonaws.com/model/{TITAN_MODEL}/invoke"
BEDROCK_KEY = os.environ.get("BEDROCK_API_KEY", "")


def _get_embedding(text: str) -> np.ndarray:
    headers = {
        "Authorization": f"Bearer {BEDROCK_KEY}",
        "Content-Type":  "application/json",
    }
    payload = {
        "inputText": text,
        "dimensions": 512,
        "normalize": True,          # unit vectors → dot product == cosine sim
    }
    resp = requests.post(TITAN_URL, headers=headers, json=payload)
    resp.raise_for_status()
    return np.array(resp.json()["embedding"], dtype=np.float32)


# ── Simple in-request cache (avoids re-embedding the same text twice) ─────────

class _EmbeddingCache:
    def __init__(self):
        self._store: dict[str, np.ndarray] = {}

    def get(self, text: str) -> np.ndarray:
        if text not in self._store:
            self._store[text] = _get_embedding(text)
        return self._store[text]


# ── Request / Response models ─────────────────────────────────────────────────

class FunctionItem(BaseModel):
    id:          str
    name:        str
    elementName: str


class SuggestRequest(BaseModel):
    lower_functions:  list[FunctionItem]
    focus_functions:  list[FunctionItem]
    higher_functions: list[FunctionItem]
    top_k_lower:      int   = 3
    top_k_higher:     int   = 2
    threshold:        float = 0.55


# ── Core logic ────────────────────────────────────────────────────────────────

def _suggest_for_focus(
    focus_fn:        FunctionItem,
    lower_fns:       list[FunctionItem],
    higher_fns:      list[FunctionItem],
    cache:           _EmbeddingCache,
    top_k_lower:     int,
    top_k_higher:    int,
    threshold:       float,
) -> dict:
    focus_emb = cache.get(focus_fn.name)

    # Score lower functions
    lower_scored = []
    for lf in lower_fns:
        sim = float(np.dot(focus_emb, cache.get(lf.name)))
        if sim >= threshold:
            lower_scored.append({
                "id":          lf.id,
                "name":        lf.name,
                "elementName": lf.elementName,
                "similarity":  round(sim, 4),
            })
    lower_scored.sort(key=lambda x: x["similarity"], reverse=True)

    # Score higher functions
    higher_scored = []
    for hf in higher_fns:
        sim = float(np.dot(focus_emb, cache.get(hf.name)))
        if sim >= threshold:
            higher_scored.append({
                "id":          hf.id,
                "name":        hf.name,
                "elementName": hf.elementName,
                "similarity":  round(sim, 4),
            })
    higher_scored.sort(key=lambda x: x["similarity"], reverse=True)

    return {
        "lower":  lower_scored[:top_k_lower],
        "higher": higher_scored[:top_k_higher],
    }


# ── Endpoint ──────────────────────────────────────────────────────────────────

@router.post("/similarity/suggest")
def similarity_suggest(req: SuggestRequest):
    """
    For each focus function, return the most semantically similar
    lower and higher functions by Titan cosine similarity.

    Response shape:
    {
      "suggestions": {
        "<focus_fn_id>": {
          "lower":  [{ id, name, elementName, similarity }, ...],
          "higher": [{ id, name, elementName, similarity }, ...]
        },
        ...
      }
    }
    """
    cache = _EmbeddingCache()

    # Pre-embed everything upfront
    for fn in [*req.lower_functions, *req.focus_functions, *req.higher_functions]:
        if fn.name.strip():
            cache.get(fn.name)

    suggestions: dict[str, dict] = {}
    for ff in req.focus_functions:
        if not ff.name.strip():
            continue
        suggestions[ff.id] = _suggest_for_focus(
            focus_fn=ff,
            lower_fns=[lf for lf in req.lower_functions if lf.name.strip()],
            higher_fns=[hf for hf in req.higher_functions if hf.name.strip()],
            cache=cache,
            top_k_lower=req.top_k_lower,
            top_k_higher=req.top_k_higher,
            threshold=req.threshold,
        )

    return {"suggestions": suggestions}
