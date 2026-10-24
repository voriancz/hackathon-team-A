"""Build data/wiki.sqlite: intros of the most-viewed Czech Wikipedia articles + seed titles.

Usage: python scripts/build_index.py [--days 30]   (run in background; ~10-15 min)
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import time

import httpx

UA = "VorianFactcheck/0.1 (hackathon demo)"
OUT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "wiki.sqlite")
SEED = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "seed_titles.txt")
client = httpx.Client(timeout=20.0, headers={"User-Agent": UA})


def top_titles(days: int) -> set[str]:
    titles: set[str] = set()
    today = dt.date.today()
    for i in range(2, days + 2):  # pageviews lag ~1-2 days
        d = today - dt.timedelta(days=i)
        url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/top/cs.wikipedia/all-access/{d:%Y/%m/%d}"
        try:
            r = client.get(url)
            r.raise_for_status()
            for a in r.json()["items"][0]["articles"]:
                t = a["article"].replace("_", " ")
                if ":" in t or t == "Hlavní strana":
                    continue
                titles.add(t)
        except Exception as e:  # noqa: BLE001
            print("skip", d, e)
        time.sleep(0.2)
    return titles


def fetch_intros(titles: list[str]):
    for i in range(0, len(titles), 20):
        chunk = titles[i:i + 20]
        params = {"action": "query", "format": "json", "formatversion": "2", "titles": "|".join(chunk),
                  "prop": "extracts|info", "exintro": "1", "explaintext": "1", "exlimit": "20", "inprop": "url", "redirects": "1"}
        try:
            r = client.get("https://cs.wikipedia.org/w/api.php", params=params)
            r.raise_for_status()
            for p in r.json().get("query", {}).get("pages", []):
                if p.get("extract"):
                    yield p["title"], p.get("fullurl", ""), p["extract"]
        except Exception as e:  # noqa: BLE001
            print("skip chunk", i, e)
        time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    titles = top_titles(args.days)
    if os.path.exists(SEED):
        titles |= {l.strip() for l in open(SEED, encoding="utf-8") if l.strip()}
    titles = sorted(titles)
    print(f"{len(titles)} titles")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    con = sqlite3.connect(OUT)
    con.execute("DROP TABLE IF EXISTS wiki")
    con.execute("CREATE VIRTUAL TABLE wiki USING fts5(title, url UNINDEXED, intro, tokenize='unicode61 remove_diacritics 2')")
    n = 0
    for title, url, intro in fetch_intros(titles):
        con.execute("INSERT INTO wiki(title, url, intro) VALUES (?,?,?)", (title, url, intro[:3000]))
        n += 1
        if n % 500 == 0:
            con.commit()
            print(n)
    con.commit()
    con.close()
    print(f"done: {n} intros -> {OUT}")


if __name__ == "__main__":
    main()
