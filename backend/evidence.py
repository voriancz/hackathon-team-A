"""Evidence retrieval: local SQLite FTS5 index of Czech Wikipedia intros, or one MediaWiki call."""
from __future__ import annotations

import os
import re
import sqlite3
import urllib.parse

import httpx

INDEX_PATH = os.environ.get("WIKI_INDEX", os.path.join(os.path.dirname(__file__), "data", "wiki.sqlite"))
UA = os.environ.get("WIKI_UA", "VorianFactcheck/0.1 (https://github.com/vorian; hackathon demo)")
_client = httpx.Client(timeout=4.0, headers={"User-Agent": UA})
_STOP = {"je", "jsou", "byl", "byla", "bylo", "má", "mají", "měří", "než", "ale", "nebo", "and", "the"}


def _tokens(claim: str) -> list[str]:
    toks = re.findall(r"[\wá-žÁ-Ž]+", claim)
    return [t for t in toks if len(t) > 2 and t.lower() not in _STOP]


def from_index(claim: str, limit: int = 2) -> list[dict]:
    if not os.path.exists(INDEX_PATH):
        return []
    toks = _tokens(claim)
    if not toks:
        return []
    q = " OR ".join(f'"{t}"' for t in toks)
    con = sqlite3.connect(INDEX_PATH)
    try:
        rows = con.execute(
            "SELECT title, url, intro FROM wiki WHERE wiki MATCH ? ORDER BY rank LIMIT ?", (q, limit)
        ).fetchall()
    finally:
        con.close()
    return [{"title": t, "url": u, "text": i, "source": "index", "title_overlap": title_overlap(claim, t)} for t, u, i in rows]


_UNIT = re.compile(r"^(km|m|cm|mm|kg|g|t|%|procent\w*|kilometr\w*|metr\w*|let|lidí|obyvatel\w*|rok\w*|hodin\w*|minut\w*)$")
_VERBISH = {"měří", "je", "jsou", "byl", "byla", "bylo", "byli", "má", "mají", "měl", "měla", "leží", "stojí", "vznikl",
            "vznikla", "vzniklo", "postaven", "postavena", "postaveno", "otevřen", "otevřena", "otevřeno", "narodil",
            "narodila", "zemřel", "zemřela", "dlouhý", "dlouhá", "dlouhé", "vysoký", "vysoká", "vysoké", "přes", "více",
            "méně", "než", "skoro", "asi", "přibližně", "zhruba", "celkem", "dnes", "včera", "také", "taky", "už", "ještě",
            "roce", "roku", "letech", "století", "tvoje", "tvůj", "moje", "můj", "naše", "náš"}


def _query_tokens(claim: str) -> list[str]:
    out = []
    for t in re.findall(r"[\wá-žÁ-Ž]+", claim):
        tl = t.lower()
        if len(tl) < 3 or tl in _STOP or tl in _VERBISH or tl.isdigit() or _UNIT.match(tl) or re.match(r"^\d", tl):
            continue
        out.append(t)
    return out


_search_cache: dict[tuple[str, int], list[dict]] = {}


def _search(q: str, limit: int, prefix: bool = False) -> list[dict]:
    """Fulltext search (generator=search) or, with prefix=True, title prefix search – the way to get the nominative
    article for a declined object ("Česk" -> Česko, "Morav" -> Morava) without guessing Czech morphology."""
    key = (q.lower(), limit, prefix)
    if key in _search_cache:
        return _search_cache[key]
    params = {
        "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
        "prop": "extracts|info", "exintro": "1", "explaintext": "1", "exlimit": str(limit), "inprop": "url",
    }
    if prefix:
        params.update({"generator": "prefixsearch", "gpssearch": q, "gpslimit": str(limit), "gpsnamespace": "0"})
    else:
        params.update({"generator": "search", "gsrsearch": q, "gsrlimit": str(limit), "gsrnamespace": "0"})
    r = _client.get("https://cs.wikipedia.org/w/api.php", params=params)
    r.raise_for_status()
    pages = r.json().get("query", {}).get("pages", [])
    pages.sort(key=lambda p: p.get("index", 99))
    docs = [{"title": p.get("title"), "url": p.get("fullurl"), "text": p.get("extract", ""), "source": "api"}
            for p in pages if p.get("extract")]
    _search_cache[key] = docs
    return docs


def _stems(text: str) -> set[str]:
    # 4-char stems so Czech declension still matches (Praha/Praze, Česko/Česka)
    return {t[:4] for t in re.findall(r"[\wá-žÁ-Ž]+", text.lower()) if len(t) > 2 and t not in _STOP}


def title_overlap(claim: str, title: str) -> int:
    return len(_stems(claim) & _stems(title or ""))


def _entity_query(claim: str, toks: list[str]) -> str:
    """The subject is usually the leading run of capitalised tokens: "Tunel Blanka", "Brno", "Karlův"."""
    run = []
    for t in toks:
        if t[0].isupper():
            run.append(t)
        else:
            break
    if run:
        return " ".join(run[:3])
    return " ".join(toks[:2])


def title_score(claim: str, title: str, toks: list[str]) -> float:
    """Rank candidate articles: shared stems, bonus when the title starts with the claim's subject, penalty for long titles."""
    overlap = title_overlap(claim, title)
    if overlap == 0:
        return 0.0
    tw = re.findall(r"[\wá-žÁ-Ž]+", title)
    first_claim = (toks[0][:4].lower() if toks else "")
    bonus = 2.0 if tw and first_claim and tw[0][:4].lower() == first_claim else 0.0
    return overlap + bonus - 0.15 * max(0, len(tw) - 1)


def _secondary_entities(toks: list[str]) -> list[str]:
    """Capitalised tokens after the leading subject run: the objects of the claim ("Česka" in "Brno je hlavní město Česka")."""
    i = 0
    while i < len(toks) and toks[i][0].isupper():
        i += 1
    return [t for t in toks[i:] if t[0].isupper()][:2]


def _object_stem(ent: str) -> str:
    """Prefix for the nominative title of a declined object: drop the ending ("Česka" -> "Česk", "Moravy" -> "Morav")."""
    return ent[:-1] if len(ent) >= 4 else ent


def object_docs(claim: str, toks: list[str], exclude_titles: set[str]) -> list[dict]:
    out = []
    for ent in _secondary_entities(toks):
        docs: list[dict] = []
        for q, lim, pre in ((_object_stem(ent), 8, True), (ent, 3, False)):
            try:
                docs += _search(q, lim, pre)
            except Exception:  # noqa: BLE001
                continue
        best = None
        seen_titles: set[str] = set()
        for d in docs:
            if d["title"] in exclude_titles or d["title"] in seen_titles:
                continue
            seen_titles.add(d["title"])
            sc = title_score(ent, d["title"] or "", [ent])
            if sc > 0 and (best is None or sc > best[0]):
                best = (sc, d)
        if best:
            d = dict(best[1])
            d["title_overlap"] = title_overlap(claim, d["title"] or "")
            d["source"] = d["source"] + "+object"
            out.append(d)
    return out


def from_api(claim: str, limit: int = 2) -> list[dict]:
    """Entity-first: search the likely entity, re-rank top hits by title overlap with the claim.
    Falls back to content words with shortening. Each doc carries title_overlap (0 = no entity match)."""
    toks = _query_tokens(claim)
    if not toks:
        return []
    queries = [_entity_query(claim, toks), " ".join(toks), " ".join(toks[:3]), " ".join(toks[:1])]
    seen = set()
    best_docs: list[dict] = []
    for q in queries:
        if not q or q in seen:
            continue
        seen.add(q)
        docs = _search(q, 5)
        for d in docs:
            d["title_overlap"] = title_overlap(claim, d["title"] or "")
            d["title_score"] = title_score(claim, d["title"] or "", toks)
        hits = sorted([d for d in docs if d["title_overlap"] > 0], key=lambda d: -d["title_score"])
        if hits:
            subject = hits[:1]
            return subject + object_docs(claim, toks, {d["title"] for d in subject})
        if docs and not best_docs:
            best_docs = docs[:limit]
    return best_docs


def get_evidence(claim: str, limit: int = 2) -> list[dict]:
    hits = from_index(claim, limit)
    if hits:
        return hits
    try:
        return from_api(claim, limit)
    except Exception as e:  # noqa: BLE001
        import traceback
        print(f"[evidence] Wikipedia lookup failed for {claim!r}: {e!r}")
        traceback.print_exc()
        return []


def wiki_url(title: str) -> str:
    return "https://cs.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
