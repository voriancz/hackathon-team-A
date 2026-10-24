"""FastAPI backend: POST /check {claim} -> card + evidence + timings. Run: uvicorn main:app --port 8000"""
from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from engine import Engine, Stopwatch, causal_claim, grounding, nonsense_marker, relation_check, relation_claim, split_sentences
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

# user-facing hints per language; {title} and {missing} are filled in, {e}/{c} are the NLI scores
MSG = {
    "cs": {
        "nazor": "To je názor, ne ověřitelné tvrzení.", "otazka": "To je otázka, ne tvrzení.",
        "nesmysl": "Věta nedává smysl jako tvrzení.", "neuplne": "Věta je neúplná, chybí tvrzení.",
        "causal": "Nelze ověřit: kauzální tvrzení, že někdo něco způsobil. Ověřit lze jen samotnou statistiku, a tu Wikipedie neeviduje.",
        "nosource": "Nelze ověřit: k výroku jsem nenašel žádný zdroj.",
        "nosubject": "Nelze ověřit: k podmětu výroku jsem nenašel žádný článek.",
        "nonsense_src": "Věta nedává smysl jako tvrzení: zdroj {title} o „{missing}“ nic neříká.",
        "ungrounded": "Nelze ověřit: zdroj {title} o „{missing}“ nic neříká.",
        "undecided": "Nelze ověřit: zdroj {title} výrok nepotvrzuje ani nevyvrací (souhlas {e} %, rozpor {c} %).",
    },
    "sk": {
        "nazor": "To je názor, nie overiteľné tvrdenie.", "otazka": "To je otázka, nie tvrdenie.",
        "nesmysl": "Veta nedáva zmysel ako tvrdenie.", "neuplne": "Veta je neúplná, chýba tvrdenie.",
        "causal": "Nedá sa overiť: kauzálne tvrdenie, že niekto niečo spôsobil. Overiť sa dá len samotná štatistika, a tú Wikipédia neeviduje.",
        "nosource": "Nedá sa overiť: k výroku som nenašiel žiadny zdroj.",
        "nosubject": "Nedá sa overiť: k podmetu výroku som nenašiel žiadny článok.",
        "nonsense_src": "Veta nedáva zmysel ako tvrdenie: zdroj {title} o „{missing}“ nič nehovorí.",
        "ungrounded": "Nedá sa overiť: zdroj {title} o „{missing}“ nič nehovorí.",
        "undecided": "Nedá sa overiť: zdroj {title} výrok nepotvrdzuje ani nevyvracia (súhlas {e} %, rozpor {c} %).",
    },
    "en": {
        "nazor": "That is an opinion, not a checkable claim.", "otazka": "That is a question, not a claim.",
        "nesmysl": "The sentence makes no sense as a claim.", "neuplne": "The sentence is incomplete, there is no claim.",
        "causal": "Unverifiable: a causal claim that someone caused something. Only the statistic itself could be checked, and Wikipedia does not keep it.",
        "nosource": "Unverifiable: I found no source for this claim.",
        "nosubject": "Unverifiable: I found no article about the subject of the claim.",
        "nonsense_src": "The sentence makes no sense as a claim: the source {title} says nothing about “{missing}”.",
        "ungrounded": "Unverifiable: the source {title} says nothing about “{missing}”.",
        "undecided": "Unverifiable: the source {title} neither confirms nor refutes the claim (agreement {e} %, contradiction {c} %).",
    },
}


def msg(lang: str, key: str, **kw) -> str:
    return MSG.get(lang, MSG["cs"]).get(key, "").format(**kw)


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
        return {**base, "card": "NESMYSL", "hint": msg(lang, t.label), "evidence": None, "timings": _done(sw)}
    if causal_claim(claim):
        return {**base, "card": "NEOVERITELNE", "hint": msg(lang, "causal"),
                "evidence": None, "timings": _done(sw)}

    docs = get_evidence(claim, lang=lang)
    if (not docs or docs[0].get("title_overlap", 1) == 0) and context:
        cdocs = get_evidence(context, lang=lang)
        if cdocs and cdocs[0].get("title_overlap", 1) > 0:
            docs = [{**d, "source": d["source"] + "+context"} for d in cdocs]
    sw.lap("evidence_ms")
    if not docs:
        return {**base, "card": "NEOVERITELNE", "hint": msg(lang, "nosource"), "evidence": None, "timings": _done(sw)}

    if docs[0].get("title_overlap", 1) == 0:
        return {**base, "card": "NEOVERITELNE", "hint": msg(lang, "nosubject"), "evidence": None, "timings": _done(sw)}

    # Nonsense check: claim words must relate to the article found (numbers are handled by the numeric rule)
    ok, missing = grounding(claim, docs[0]["text"], docs[0]["title"] or "")
    if not ok:
        sw.lap("grounding_ms")
        if nonsense_marker(claim):
            hint = msg(lang, "nonsense_src", title=docs[0]["title"], missing=" ".join(missing))
            return {**base, "card": "NESMYSL", "hint": hint, "evidence": None, "timings": _done(sw)}
        hint = msg(lang, "ungrounded", title=docs[0]["title"], missing=" ".join(missing))
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
    # a relation claim the rule could not settle: NLI on its own tends to agree with any sentence that mentions
    # "hlavné mesto" (Košice, European Capital of Volunteering), so it needs to be much surer to decide
    if best["card"] != "NEOVERITELNE" and not best["by_rule"] and relation_claim(claim) and max(best["entail"], best["contra"]) < 0.92:
        best = {**best, "card": "NEOVERITELNE"}
    sw.lap("verdict_ms")
    hint = ""
    if best["card"] == "NEOVERITELNE":
        hint = msg(lang, "undecided", title=best["title"], e=round(best["entail"] * 100), c=round(best["contra"] * 100))
    return {**base, "card": best["card"], "hint": hint, "evidence": best, "timings": _done(sw)}


def _done(sw: Stopwatch):
    laps = dict(sw.laps)
    laps["total_ms"] = round(sum(laps.values()), 1)
    return laps
