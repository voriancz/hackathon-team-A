"""Run the pipeline over tests/claims.json and print accuracy + confusion. Usage: python eval.py"""
import json
import os
import time

from engine import Engine, split_sentences
from evidence import get_evidence


def run_one(engine, claim):
    t = engine.claim_type(claim)
    if t.label != "fakticke":
        return "NESMYSL", t.label
    docs = get_evidence(claim)
    if not docs:
        return "NEOVERITELNE", "no-evidence"
    for d in docs:
        v = engine.verdict(claim, split_sentences(d["text"]))
        if v.card != "NEOVERITELNE":
            return v.card, (v.sentence or "")[:60]
    return "NEOVERITELNE", d["title"]


def main():
    cases = json.load(open(os.path.join(os.path.dirname(__file__), "tests", "claims.json"), encoding="utf-8"))
    engine = Engine()
    ok = 0
    conf = {}
    t0 = time.perf_counter()
    for c in cases:
        got, why = run_one(engine, c["claim"])
        hit = got == c["expected"]
        ok += hit
        conf[(c["expected"], got)] = conf.get((c["expected"], got), 0) + 1
        print(("OK " if hit else "XX ") + f"{c['expected']:<13} got {got:<13} | {c['claim']}  [{why}]")
    dt = (time.perf_counter() - t0) / max(len(cases), 1)
    print(f"\naccuracy {ok}/{len(cases)} = {ok/len(cases):.0%}, avg {dt*1000:.0f} ms/claim (incl. evidence)")
    for (e, g), n in sorted(conf.items()):
        if e != g:
            print(f"  {e} -> {g}: {n}")


if __name__ == "__main__":
    main()
