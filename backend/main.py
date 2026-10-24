"""FastAPI backend: POST /check {claim} -> card + evidence + timings. Run: uvicorn main:app --port 8000"""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from engine import Engine, HINTS, Stopwatch, causal_claim, grounding, nonsense_marker, relation_check, split_sentences
from evidence import get_evidence

engine: Optional[Engine] = None
_nli_lock = threading.Lock()          # NLI on CPU: one at a time, otherwise speculative requests thrash each other
_results: dict = {}                   # (claim, context) -> (timestamp, response)
_RESULT_TTL = 600


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = Engine()  # loads + warms up the model once
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class CheckIn(BaseModel):
    claim: str
    context: Optional[str] = None  # previous sentence; supplies the entity when this one has none ("Je dlouhý 1000 km.")
    lang: str = "cs"  # cs | sk | en: which Wikipedia to ask


class TextIn(BaseModel):
    text: str
    lang: str = "cs"


_LANGS = {"cs", "sk", "en"}


def _lang(v: str) -> str:
    v = (v or "cs").lower()[:2]
    return v if v in _LANGS else "cs"


@app.get("/health")
def health():
    return {"ok": engine is not None}


@app.post("/check")
def check(body: CheckIn):
    return check_one(body.claim.strip(), (body.context or "").strip() or None, _lang(body.lang))


@app.post("/check_many")
def check_many(body: TextIn):
    """Split into sentences; each sentence is checked with the previous one as context."""
    sentences = split_sentences(body.text.strip())
    out, prev = [], None
    for s in sentences:
        out.append(check_one(s, prev, _lang(body.lang)))
        prev = s
    return {"results": out}


def check_one(claim: str, context: Optional[str], lang: str = "cs"):
    key = (claim.lower(), (context or "").lower(), lang)
    hit = _results.get(key)
    if hit and time.time() - hit[0] < _RESULT_TTL:
        return {**hit[1], "cached": True}
    res = _check_one(claim, context, lang)
    _results[key] = (time.time(), res)
    return res


def _check_one(claim: str, context: Optional[str], lang: str = "cs"):
    sw = Stopwatch()
    t = engine.claim_type(claim)
    sw.lap("type_ms")
    base = {"claim": claim, "type": {"label": t.label, "score": round(t.score, 3), "probs": {k: round(v, 3) for k, v in t.probs.items()}}}
    if t.label != "fakticke":
        return {**base, "card": "NESMYSL", "hint": HINTS.get(t.label, ""), "evidence": None, "timings": _done(sw)}
    if causal_claim(claim):
        return {**base, "card": "NEOVERITELNE", "hint": "Nelze ověřit: kauzální tvrzení, že někdo něco způsobil. Ověřit lze jen samotnou statistiku, a tu Wikipedie neeviduje.",
                "evidence": None, "timings": _done(sw)}

    docs = get_evidence(claim, lang=lang)
    if (not docs or docs[0].get("title_overlap", 1) == 0) and context:
        cdocs = get_evidence(context, lang=lang)
        if cdocs and cdocs[0].get("title_overlap", 1) > 0:
            docs = [{**d, "source": d["source"] + "+context"} for d in cdocs]
    sw.lap("evidence_ms")
    if not docs:
        return {**base, "card": "NEOVERITELNE", "hint": "Nelze ověřit: k výroku jsem nenašel žádný zdroj.", "evidence": None, "timings": _done(sw)}

    if docs[0].get("title_overlap", 1) == 0:
        return {**base, "card": "NEOVERITELNE", "hint": "Nelze ověřit: k podmětu výroku jsem nenašel žádný článek.", "evidence": None, "timings": _done(sw)}

    # Nonsense check: claim words must relate to the article found (numbers are handled by the numeric rule)
    ok, missing = grounding(claim, docs[0]["text"], docs[0]["title"] or "")
    if not ok:
        sw.lap("grounding_ms")
        if nonsense_marker(claim):
            hint = f"Věta nedává smysl jako tvrzení: zdroj {docs[0]['title']} o „{' '.join(missing)}“ nic neříká."
            return {**base, "card": "NESMYSL", "hint": hint, "evidence": None, "timings": _done(sw)}
        hint = f"Nelze ověřit: zdroj {docs[0]['title']} o „{' '.join(missing)}“ nic neříká."
        return {**base, "card": "NEOVERITELNE", "hint": hint, "evidence": {"sentence": None, "title": docs[0]["title"], "url": docs[0]["url"], "entail": 0, "contra": 0, "by_rule": True, "source": docs[0]["source"]}, "timings": _done(sw)}

    # relation rule first (capital city, highest mountain, longest river, largest city): decided from the text, no NLI
    for d in docs:
        rc = relation_check(claim, split_sentences(d["text"]), d["title"] or "")
        if rc is not None:
            ok_rel, sent = rc
            sw.lap("verdict_ms")
            ev = {"card": "PRAVDA" if ok_rel else "NEPRAVDA", "sentence": sent, "entail": 1.0 if ok_rel else 0.0, "contra": 0.0 if ok_rel else 1.0,
                  "by_rule": True, "title": d["title"], "url": d["url"], "source": d["source"]}
            return {**base, "card": ev["card"], "hint": "", "evidence": ev, "timings": _done(sw)}

    best = None
    for d in docs:
        with _nli_lock:
            v = engine.verdict(claim, split_sentences(d["text"]), d["title"] or "")
        cand = {"card": v.card, "sentence": v.sentence, "entail": round(v.entail, 3), "contra": round(v.contra, 3),
                "by_rule": v.by_rule, "title": d["title"], "url": d["url"], "source": d["source"]}
        if best is None or max(cand["entail"], cand["contra"]) > max(best["entail"], best["contra"]):
            best = cand
        if v.card != "NEOVERITELNE":
            break  # first decisive document wins; keeps latency low
    sw.lap("verdict_ms")
    hint = ""
    if best["card"] == "NEOVERITELNE":
        hint = (f"Nelze ověřit: zdroj {best['title']} výrok nepotvrzuje ani nevyvrací "
                f"(souhlas {round(best['entail'] * 100)} %, rozpor {round(best['contra'] * 100)} %).")
    return {**base, "card": best["card"], "hint": hint, "evidence": best, "timings": _done(sw)}


def _done(sw: Stopwatch):
    laps = dict(sw.laps)
    laps["total_ms"] = round(sum(laps.values()), 1)
    return laps
