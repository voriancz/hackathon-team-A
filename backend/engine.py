"""NLI engine: claim typing (zero-shot) + verdict per evidence sentence + numeric rule.

No generative model anywhere. One multilingual NLI model does both rounds.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

MODEL = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"

# Round 1: claim type via zero-shot NLI. Keys are what the API returns.
TYPES = {
    "fakticke": "Tato věta je ověřitelné faktické tvrzení.",
    "nazor": "Tato věta vyjadřuje názor.",
    "otazka": "Tato věta je otázka.",
    "nesmysl": "Tato věta nedává smysl.",
    "neuplne": "Této větě chybí informace.",
}
HINTS = {
    "nazor": "To je názor, ne ověřitelné tvrzení.",
    "otazka": "To je otázka, ne tvrzení.",
    "nesmysl": "Věta nedává smysl jako tvrzení.",
    "neuplne": "Věta je neúplná, chybí tvrzení.",
}

ENTAIL_T = 0.70
CONTRA_T = 0.70
NUM_TOL = 0.05  # ±5 % for lengths and counts

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9„])")

# --- numeric rule -----------------------------------------------------------
_UNITS = {
    "km": ("len", 1000.0), "kilometr": ("len", 1000.0), "kilometry": ("len", 1000.0), "kilometrů": ("len", 1000.0),
    "m": ("len", 1.0), "metr": ("len", 1.0), "metry": ("len", 1.0), "metrů": ("len", 1.0),
    "cm": ("len", 0.01),
    "%": ("pct", 1.0), "procent": ("pct", 1.0), "procenta": ("pct", 1.0),
    # Slovak / English
    "kilometrov": ("len", 1000.0), "kilometre": ("len", 1000.0), "metrov": ("len", 1.0), "metre": ("len", 1.0),
    "kilometres": ("len", 1000.0), "kilometers": ("len", 1000.0), "kilometer": ("len", 1000.0),
    "metres": ("len", 1.0), "meters": ("len", 1.0), "meter": ("len", 1.0),
    "mi": ("len", 1609.344), "mile": ("len", 1609.344), "miles": ("len", 1609.344),
    "ft": ("len", 0.3048), "foot": ("len", 0.3048), "feet": ("len", 0.3048), "percent": ("pct", 1.0),
}
_NUM = re.compile(r"(?<![\w,.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{1,3}(?:[  ]\d{3})+|\d+(?:[.,]\d+)?)\s*([a-zA-Zá-ž%]+)?")


def extract_numbers(text: str) -> list[tuple[str, float]]:
    """Return (dimension, value) pairs: len in metres, year, pct, count."""
    out = []
    for m in _NUM.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").lower()
        if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):  # English thousands: 8,848.86 / 1,083
            raw = raw.replace(",", "")
        val = float(raw.replace(" ", "").replace(" ", "").replace(",", "."))
        if unit in _UNITS:
            dim, mult = _UNITS[unit]
            out.append((dim, val * mult))
        elif unit == "":
            if 1000 <= val <= 2100 and val.is_integer():
                out.append(("year", val))
            else:
                out.append(("count", val))
        # unknown unit (e.g. "lidí", "let") -> count
        else:
            out.append(("count", val))
    return out


def numeric_check(claim: str, sentence: str) -> bool | None:
    """True/False when claim and sentence carry comparable numbers, else None."""
    c = extract_numbers(claim)
    if len(c) != 1:
        return None
    dim, cv = c[0]
    cands = [v for d, v in extract_numbers(sentence) if d == dim]
    if not cands:
        return None
    sv = min(cands, key=lambda v: abs(v - cv))
    if dim == "year":
        return sv == cv
    if dim == "pct":
        return abs(sv - cv) <= 1.0
    return abs(sv - cv) <= NUM_TOL * max(abs(sv), 1e-9)



# --- round 1: rules -----------------------------------------------------------
_INTERROG = {"kolik", "kdo", "kde", "kdy", "proč", "jak", "co", "jaký", "jaká", "jaké", "který", "která",
             "které", "zda", "kam", "odkud", "čí", "kolikrát", "copak", "jestli",
             # sk / en
             "koľko", "kto", "kedy", "prečo", "ako", "čo", "aký", "aká", "aké", "ktorý", "ktorá", "ktoré", "či",
             "how", "who", "where", "when", "why", "what", "which", "is", "are", "does", "do", "did", "was", "were", "can", "could"}
_OPINION = {"nejkrásnější", "nejhezčí", "nejlepší", "nejhorší", "krásný", "krásná", "krásné", "hezký", "hezká",
            "hezké", "ošklivý", "ošklivá", "ošklivé", "skvělý", "skvělá", "skvělé", "hrozný", "hrozná", "hrozné",
            "super", "nudný", "nudná", "nudné", "úžasný", "úžasná", "úžasné", "lepší", "horší", "myslím",
            "podle", "měl by", "měla by", "mělo by", "zajímavý", "zajímavá", "zajímavé", "příjemný", "nejlepsi",
            # sk / en
            "najkrajší", "najkrajšia", "najkrajšie", "najlepší", "najlepšia", "najlepšie", "najhorší", "krásny", "krásna", "krásne",
            "pekný", "pekná", "pekné", "škaredý", "skvelý", "skvelá", "skvelé", "hrozný", "hrozná", "hrozné", "nudný", "nudná",
            "úžasný", "lepší", "horší", "myslím", "podľa", "zaujímavý", "zaujímavá", "zaujímavé",
            "beautiful", "prettiest", "best", "worst", "nice", "ugly", "great", "awesome", "boring", "better", "worse",
            "think", "should", "amazing", "terrible", "wonderful", "interesting", "lovely"}
_STOP = {"je", "jsou", "byl", "byla", "bylo", "byli", "má", "mají", "než", "ale", "nebo", "the", "and", "a", "i",
         "v", "ve", "na", "do", "od", "za", "po", "se", "si", "to", "ten", "ta", "tak", "jako", "přes", "pod", "nad",
         "roce", "roku", "let", "metrů", "metry", "metr", "km", "kilometrů", "tisíc", "milionů", "milion", "milionu",
         "tvoje", "tvůj", "tvá", "moje", "můj", "má", "naše", "náš", "jeho", "její", "jejich", "svůj", "svoje",
         # sk / en
         "sú", "bol", "bola", "bolo", "boli", "alebo", "ako", "vo", "pre", "pri", "cez", "roku", "rokov", "metrov", "kilometrov",
         "is", "are", "was", "were", "of", "in", "on", "at", "to", "an", "has", "have", "had", "than", "that", "this", "it", "its",
         "by", "for", "with", "from", "as", "over", "about", "year", "years", "your", "my", "our", "his", "her", "their"}


def _toks(text: str) -> list[str]:
    return re.findall(r"[\wá-žÁ-Ž]+", text.lower())


def rule_type(claim: str) -> str:
    text = claim.strip()
    toks = _toks(text)
    if len(toks) < 3:
        return "neuplne"
    if text.endswith("?") or toks[0] in _INTERROG:
        return "otazka"
    if not re.search(r"\d", text) and (any(t in _OPINION for t in toks) or re.search(r"\b(měl|měla|mělo|mal|mala|malo) by\b", text.lower())):
        return "nazor"
    return "fakticke"


_PERSONAL = {"tvoje", "tvůj", "tvá", "tvé", "tvoji", "tvého", "moje", "můj", "má", "mé", "moji", "mého", "naše", "náš",
             "vaše", "váš", "ty", "já", "mě", "tebe", "tobě", "mně", "bába", "babička", "děda", "dědeček", "máma", "táta",
             "sestra", "bratr", "manželka", "manžel", "kamarád", "soused", "sousedka", "teta", "strýc",
             # sk / en
             "tvoja", "tvoj", "tvojho", "moja", "môj", "môjho", "naša", "vaša", "babka", "dedo", "dedko", "mama", "otec", "brat",
             "kamarát", "sused", "susedka", "strýko", "ujo",
             "your", "my", "our", "yours", "mine", "you", "me", "grandma", "grandmother", "granny", "grandpa", "grandfather",
             "mom", "mum", "mother", "dad", "father", "sister", "brother", "wife", "husband", "friend", "neighbour", "neighbor",
             "aunt", "uncle", "cousin"}


_CAUSAL = {"snížil", "snížila", "snížilo", "zvýšil", "zvýšila", "zvýšilo", "způsobil", "způsobila", "zajistil", "zajistila",
           "prosadil", "prosadila", "zlepšil", "zlepšila", "zhoršil", "zhoršila", "zachránil", "zachránila", "zničil", "zničila",
           "vyřešil", "vyřešila", "zavinil", "zavinila", "přinesl", "přinesla", "zdražil", "zdražila", "zlevnil", "zlevnila",
           "snížili", "zvýšili", "zajistili", "prosadili", "zlepšili", "zhoršili",
           # sk / en
           "znížil", "znížila", "znížili", "zvýšil", "zvýšila", "zvýšili", "spôsobil", "spôsobila", "zabezpečil", "zabezpečila",
           "presadil", "presadila", "zlepšil", "zlepšila", "zhoršil", "zhoršila", "zachránil", "zachránila", "zničil", "zničila",
           "vyriešil", "vyriešila", "zavinil", "zavinila", "priniesol", "priniesla", "zdražil", "zlacnil",
           "reduced", "increased", "caused", "ensured", "secured", "improved", "worsened", "saved", "destroyed", "solved",
           "lowered", "raised", "boosted", "fixed", "ruined", "delivered", "created", "cut", "slashed", "doubled", "halved"}


def causal_claim(claim: str) -> bool:
    """A person or party credited with an outcome ("X snížil chudobu"): a judgement, not a checkable fact."""
    return bool(set(_toks(claim)) & _CAUSAL)


# --- relation rule: "X je hlavní město Y" and superlatives, decided from the article text like the numeric rule ---
_RELS = {  # cs | sk | en
    "capital": r"hlavní(?:m)?\s+měst(?:o|em)|hlavn(?:é|ým|ého)\s+mest(?:o|om|a)|capital(?:\s+city)?",
    "peak": r"nejvyšší\s+hor(?:a|ou)|najvyšš(?:í|ia|ím|ou)\s+(?:vrch|vrchom|hora|horou)|highest\s+(?:mountain|peak|point)",
    "river": r"nejdelší\s+řek(?:a|ou)|najdlhš(?:ia|ou)\s+riek(?:a|ou)|longest\s+river",
    "largest": r"(?:největší|nejlidnatější)(?:m)?\s+měst(?:o|em)|najväčš(?:ie|ím)\s+mest(?:o|om)|(?:largest|biggest|most\s+populous)\s+city",
}
_IS = r"(?:je|jsou|sú|is|are|was|were)"
_CAPW = r"[A-ZÁ-Ž][\wá-ž]{2,}"
_REL_CLAIM = re.compile(
    r"^\s*(?:[Tt]he\s+)?(?P<x>\S+(?:\s+\S+)?)\s+" + _IS + r"\s+(?:the\s+)?(?i:" + "|".join(f"(?P<{k}>{v})" for k, v in _RELS.items()) + r")\s+(?:of\s+)?(?P<y>[^.,!?]+)", re.U)
_REL_AFTER = {k: re.compile(r"(?P<rel>(?i:" + v + r"))\s+(?:\S+\s+){0,5}?" + _IS + r"\s+(?P<z>" + _CAPW + ")", re.U) for k, v in _RELS.items()}
# subject-first definition sentence: "Sněžka (polsky Śnieżka, 1603 m n. m.) je se svou výškou … nejvyšší horou Česka" – anchored at the
# sentence start so a sentence-initial word like "Vzhledem" is never taken for the subject
_REL_BEFORE = {k: re.compile(r"^(?:[Tt]he\s+)?(?P<z>" + _CAPW + r")(?:\s+[\wá-ž]+)?(?:\s*\([^)]*\))?(?:,[^,]*,)?\s+" + _IS + r"\s+(?:\S+\s+){0,8}?(?P<rel>(?i:" + v + "))", re.U) for k, v in _RELS.items()}
_ORDINAL = re.compile(r"\b(?:druh|třet|čtvrt|pát|šest|sedm|osm|devát|desát|tret|štvrt|piat|second|third|fourth|fifth|sixth)[\wá-ž]*\s*$")
_NEG = ("není", "nebyl", "nie je", "nebol", "is not", "isn't", "was not", "wasn't")
_PREP = {"v", "ve", "vo", "na", "z", "ze", "zo", "of", "in", "the"}


def _st(word: str) -> str:
    w = re.sub(r"[^\wá-žÁ-Ž]", "", word.lower())
    return w[: max(3, min(5, len(w) - 1))]


def _same(a: str, b: str) -> bool:
    sa, sb = _st(a), _st(b)
    n = min(len(sa), len(sb))
    return n >= 3 and sa[:n] == sb[:n]


def relation_check(claim: str, sentences: list[str], title: str = "") -> Optional[tuple[bool, str]]:
    """Claim "X je <hlavní město|nejvyšší hora|nejdelší řeka|největší město> Y": find who holds that relation for Y
    in a sentence that is about Y (article titled Y, or the sentence names Y). Returns (claim_true, sentence) or None."""
    m = _REL_CLAIM.match(claim)
    if not m:
        return None
    rel = next(k for k in _RELS if m.group(k))
    x = m.group("x").split()[0]
    ywords = [w for w in m.group("y").split() if w.lower() not in _PREP]
    if not ywords:
        return None
    y = ywords[0]
    about_y = bool(title) and _same(title.split(" (")[0].split()[0], y)
    for s in sentences:
        if not about_y and _st(y) not in s.lower():
            continue
        if any(n in s for n in _NEG):
            continue
        for rx in (_REL_AFTER[rel], _REL_BEFORE[rel]):
            mm = rx.search(s)
            if mm and not _ORDINAL.search(s[max(0, mm.start("rel") - 24):mm.start("rel")].lower()):
                return _same(mm.group("z"), x), s
    return None


def nonsense_marker(claim: str) -> bool:
    """Claims that bind a public entity to the speaker's private world ("je tvoje bába") are nonsense, not unverifiable."""
    toks = set(_toks(claim))
    return bool(toks & _PERSONAL)


def grounding(claim: str, intro: str, title: str) -> tuple[bool, list[str]]:
    """Do the claim's content words (outside the entity name) appear in the article intro?

    Returns (grounded, missing_words). A claim with a number is always grounded (numbers get the numeric rule);
    otherwise at least one content word must share a 5-char stem with the intro. "Tunel Blanka je tvoje bába"
    -> ("tvoje", "bába") missing -> not grounded -> NESMYSL, nutné doplnění.
    """
    if re.search(r"\d", claim):
        return True, []
    title_stems = {t[:5] for t in _toks(title)}
    intro_stems = {t[:5] for t in _toks(intro)}
    content = [t for t in _toks(claim) if len(t) > 2 and t not in _STOP and t[:5] not in title_stems]
    if not content:
        return True, []
    missing = [t for t in content if t[:5] not in intro_stems]
    return len(missing) < len(content), missing

# --- engine -----------------------------------------------------------------
@dataclass
class TypeResult:
    label: str
    score: float
    probs: dict = field(default_factory=dict)


@dataclass
class VerdictResult:
    card: str                      # PRAVDA | NEPRAVDA | NEOVERITELNE
    sentence: Optional[str]
    entail: float
    contra: float
    by_rule: bool
    per_sentence: list = field(default_factory=list)


def _pick_device():
    try:
        import torch
        if torch.cuda.is_available():
            return 0
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return -1


class Engine:
    def __init__(self, device=None):
        from transformers import pipeline
        device = _pick_device() if device is None else device
        self.zs = pipeline("zero-shot-classification", model=MODEL, device=device)
        self.nli = pipeline("text-classification", model=MODEL, top_k=None, device=device)
        self.warmup()

    def warmup(self):
        self.claim_type("Tunel Blanka měří 5 km.")
        self.verdict("Tunel Blanka měří 5 km.", ["Tunel Blanka je dlouhý 5,5 km."])

    def claim_type(self, claim: str) -> TypeResult:
        """Rules first (fast, predictable); NLI zero-shot only when TYPE_MODE=nli."""
        label = rule_type(claim)
        if label == "fakticke" and os.environ.get("TYPE_MODE") == "nli":
            r = self.zs(claim, candidate_labels=list(TYPES.values()), hypothesis_template="{}")
            inv = {v: k for k, v in TYPES.items()}
            probs = {inv[l]: float(sc) for l, sc in zip(r["labels"], r["scores"])}
            best = max(probs, key=probs.get)
            return TypeResult(best, probs[best], probs)
        return TypeResult(label, 1.0, {label: 1.0})

    def verdict(self, claim: str, sentences: list[str], title: str = "") -> VerdictResult:
        sentences = [s for s in sentences if s.strip()]
        if not sentences:
            return VerdictResult("NEOVERITELNE", None, 0.0, 0.0, False)
        # keep the 4 sentences that share most word stems with the claim (NLI cost is linear in sentences)
        cstems = {t[:5] for t in _toks(claim) if len(t) > 2 and t not in _STOP}
        ranked = sorted(sentences, key=lambda s: -len(cstems & {t[:5] for t in _toks(s)}))
        sentences = [s[:400] for s in ranked[:4]]
        # numeric rule first: cheap and more reliable than NLI on numbers
        for s in sentences:
            nc = numeric_check(claim, s)
            if nc is not None:
                return VerdictResult("PRAVDA" if nc else "NEPRAVDA", s, 1.0 if nc else 0.0, 0.0 if nc else 1.0, True)
        batch = [{"text": (f"{title}: {s}" if title and not s.startswith(title.split(" (")[0]) else s), "text_pair": claim} for s in sentences]
        outs = self.nli(batch, truncation=True, max_length=256)
        per = []
        for s, scores in zip(sentences, outs):
            d = {x["label"].lower(): float(x["score"]) for x in scores}
            per.append({"sentence": s, "entail": d.get("entailment", 0.0),
                        "contra": d.get("contradiction", 0.0), "neutral": d.get("neutral", 0.0)})
        best = max(per, key=lambda p: max(p["entail"], p["contra"]))
        if best["entail"] >= ENTAIL_T:
            card = "PRAVDA"
        elif best["contra"] >= CONTRA_T:
            card = "NEPRAVDA"
        else:
            card = "NEOVERITELNE"
        return VerdictResult(card, best["sentence"], best["entail"], best["contra"], False, per)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_SPLIT.split(text) if s.strip()]


class Stopwatch:
    def __init__(self):
        self.t = time.perf_counter()
        self.laps = {}

    def lap(self, name: str):
        now = time.perf_counter()
        self.laps[name] = round((now - self.t) * 1000, 1)
        self.t = now
        return self.laps[name]
