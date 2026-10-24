"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import LieStrip, { type Seg } from "./LieStrip";
import MicWave from "./MicWave";

const API = process.env.NEXT_PUBLIC_API ?? "http://localhost:8000";

type Card = "PRAVDA" | "NEPRAVDA" | "NESMYSL" | "NEOVERITELNE";
type Result = { claim: string; card: Card; hint?: string; evidence?: { sentence: string | null; title: string; url: string; entail: number; contra: number; by_rule: boolean; source?: string } | null; timings?: Record<string, number>; type?: { label: string } };
const DECISIVE = new Set<Card>(["PRAVDA", "NEPRAVDA"]);
const STATE: Record<Card, string> = { PRAVDA: "pravda", NEPRAVDA: "nepravda", NESMYSL: "nesmysl", NEOVERITELNE: "neutrální" };
const TAG: Record<Card, string> = { PRAVDA: "pravda", NEPRAVDA: "nepravda", NESMYSL: "nesmysl", NEOVERITELNE: "nelze ověřit" };

// Simulation script: typed word by word, checked continuously, like speech. Key "d" or ?demo.
const DEMO = [
  "Tunel Blanka měří 5,5 km.",
  "Tunel Blanka je tunelový komplex. Je dlouhý 1000 km.",
  "Sněžka je nejvyšší hora Česka.",
  "Karlův most byl postaven v roce 1990.",
  "Tunel Blanka je tvoje bába.",
  "Vltava je nejdelší řeka v Česku.",
  "Brno je hlavní město Česka.",
  "Praha má přes milion obyvatel.",
];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const norm = (s: string) => s.trim().toLowerCase().replace(/\s+/g, " ").replace(/[.!?]+$/, "");
const splitSentences = (t: string) => t.split(/(?<=[.!?])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9„])/).map((s) => s.trim()).filter(Boolean);

export default function Page() {
  const [listening, setListening] = useState(false);
  const [segments, setSegments] = useState<Seg[]>([]);
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null);
  const [showInput, setShowInput] = useState(false);
  const [debug, setDebug] = useState(false);
  const [history, setHistory] = useState<Result[]>([]);
  const [last, setLast] = useState<{ final: boolean; results: Result[]; at: string } | null>(null);
  const [typed, setTyped] = useState("");
  const [transcript, setTranscript] = useState("");
  const [demo, setDemo] = useState(false);
  const demoRef = useRef(false);
  const ampRef = useRef(0);

  const audioRef = useRef<{ ac: AudioContext; stream: MediaStream } | null>(null);
  const cache = useRef(new Map<string, Result[]>());
  const inflight = useRef<AbortController | null>(null);
  const debounce = useRef<number | null>(null);
  const currentInterim = useRef("");
  const listeningRef = useRef(false);
  const recRef = useRef<any>(null);

  // --- wave segments: one per sentence, addressed by id so late verdicts land on the right one ---
  const openSegIdRef = useRef<number | null>(null);
  const openSegment = useCallback((start = performance.now()): number => {
    if (openSegIdRef.current !== null) return openSegIdRef.current;
    const id = start;
    openSegIdRef.current = id;
    setSegments((segs) => [...segs.filter((x) => x.end === null || x.end > start - 60000), { id, start, end: null, card: null, prelim: null }]);
    return id;
  }, []);
  const prelimSegment = useCallback((id: number, card: Card) => {
    setSegments((segs) => segs.map((x) => (x.id === id && x.end === null ? { ...x, prelim: card } : x)));
  }, []);
  const closeSegment = useCallback((id: number, results: Result[]) => {
    const now = performance.now();
    if (openSegIdRef.current === id) openSegIdRef.current = null;
    setSegments((segs) => {
      const i = segs.findIndex((x) => x.id === id);
      if (i < 0) return segs;
      const seg = segs[i];
      const end = seg.end ?? now;
      const n = results.length;
      const slot = Math.max(600, (end - seg.start) / n);
      const closed: Seg[] = results.map((r, k) => ({
        id: k === 0 ? seg.id : seg.id + k, start: k === 0 ? seg.start : end - (n - k) * slot, end: end - (n - k - 1) * slot, card: r.card, prelim: null,
      }));
      return [...segs.slice(0, i), ...closed, ...segs.slice(i + 1)];
    });
  }, []);

  // --- speculative checking: interim text is checked while you speak ----------
  const check = useCallback(async (text: string, isFinal: boolean, segId: number) => {
    const nk = norm(text);
    if (!nk) return;
    const many = isFinal && splitSentences(text).length > 1;
    const key = (many ? "many:" : "one:") + nk;
    const hit = cache.current.get(key);
    if (hit) {
      if (isFinal) { closeSegment(segId, hit); setHistory((h) => [...hit.slice().reverse(), ...h].slice(0, 40)); }
      else if (DECISIVE.has(hit[hit.length - 1].card)) prelimSegment(segId, hit[hit.length - 1].card);
      return;
    }
    if (!isFinal) inflight.current?.abort(); // a newer interim supersedes the older one; finals are never aborted
    const ctrl = new AbortController();
    if (!isFinal) inflight.current = ctrl;
    try {
      const sentences = splitSentences(text);
      let results: Result[];
      if (isFinal && sentences.length > 1) {
        const r = await fetch(`${API}/check_many`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }), signal: ctrl.signal });
        results = ((await r.json()) as { results: Result[] }).results;
      } else {
        const claim = sentences[sentences.length - 1] ?? text;
        const context = sentences.length > 1 ? sentences[sentences.length - 2] : undefined;
        const r = await fetch(`${API}/check`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ claim, context }), signal: ctrl.signal });
        results = [(await r.json()) as Result];
      }
      cache.current.set(key, results);
      setLast({ final: isFinal, results, at: new Date().toLocaleTimeString() });
      console.log("[check]", isFinal ? "FINAL" : "interim", text, results.map((r) => `${r.card}${r.hint ? " (" + r.hint + ")" : ""}`), results[0]?.timings);
      if (isFinal) { closeSegment(segId, results); setHistory((h) => [...results.slice().reverse(), ...h].slice(0, 40)); }
      else if (norm(currentInterim.current) === key && DECISIVE.has(results[results.length - 1].card)) prelimSegment(segId, results[results.length - 1].card);
    } catch (e: any) { if (e?.name !== "AbortError") { console.error("[check] backend error", e); setLast({ final: isFinal, results: [], at: `chyba: ${e?.message ?? e}` }); } }
  }, [closeSegment, prelimSegment]);

  const scheduleSpeculative = useCallback((text: string) => {
    currentInterim.current = text;
    const segId = openSegment();
    if (debounce.current) window.clearTimeout(debounce.current);
    debounce.current = window.setTimeout(() => check(text, false, segId), 200);
  }, [check, openSegment]);

  const submitFinal = useCallback((text: string) => {
    if (!text.trim()) return;
    if (debounce.current) window.clearTimeout(debounce.current);
    const segId = openSegment(performance.now() - 900);
    setTranscript(text);
    void check(text, true, segId);
  }, [check, openSegment]);

  // --- simulation: "types" sentences word by word through the same pipeline ----
  const stopDemo = useCallback(() => { demoRef.current = false; ampRef.current = 0; setDemo(false); }, []);
  const startDemo = useCallback(async () => {
    if (demoRef.current) return;
    demoRef.current = true;
    setDemo(true);
    let i = 0;
    while (demoRef.current) {
      const sentence = DEMO[i % DEMO.length];
      i++;
      const words = sentence.split(" ");
      let acc = "";
      for (const w of words) {
        if (!demoRef.current) return;
        acc = acc ? `${acc} ${w}` : w;
        setTranscript(acc);
        ampRef.current = 0.06 + Math.random() * 0.05;
        scheduleSpeculative(acc);
        await sleep(320 + Math.random() * 180);
      }
      ampRef.current = 0;
      submitFinal(sentence);
      await sleep(4500 + Math.random() * 1000); // long pause after the full stop so the verdict is seen holding
    }
  }, [openSegment, scheduleSpeculative, submitFinal]);

  // --- microphone ---------------------------------------------------------------
  const stop = useCallback(() => {
    listeningRef.current = false;
    recRef.current?.stop();
    setListening(false);
    audioRef.current?.stream.getTracks().forEach((t) => t.stop());
    audioRef.current?.ac.close().catch(() => undefined);
    audioRef.current = null;
    setAnalyser(null);
  }, []);

  const start = useCallback(async () => {
    const SR = (window as any).webkitSpeechRecognition || (window as any).SpeechRecognition;
    if (!SR) { setShowInput(true); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ac = new AudioContext();
      const an = ac.createAnalyser();
      an.fftSize = 512;
      ac.createMediaStreamSource(stream).connect(an);
      audioRef.current = { ac, stream };
      setAnalyser(an);
    } catch { /* no waveform amplitude; levels still work */ }
    const rec = new SR();
    rec.lang = "cs-CZ";
    rec.interimResults = true;
    rec.continuous = true;
    rec.maxAlternatives = 1;
    rec.onstart = () => { listeningRef.current = true; setListening(true); };
    rec.onresult = (ev: any) => {
      let interimText = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) submitFinal(r[0].transcript.trim());
        else interimText += r[0].transcript;
      }
      if (interimText) {
        setTranscript(interimText);
        scheduleSpeculative(interimText);
      }
    };
    rec.onend = () => { if (listeningRef.current) { try { rec.start(); } catch { /* restarting too fast */ } } else setListening(false); };
    recRef.current = rec;
    rec.start();
  }, [scheduleSpeculative, submitFinal, openSegment]);

  useEffect(() => {
    fetch(`${API}/health`).catch(() => undefined);
    const onKey = (e: KeyboardEvent) => {
      const inInput = (e.target as HTMLElement)?.tagName === "INPUT";
      if (e.code === "Space" && !inInput) { e.preventDefault(); if (listeningRef.current) stop(); else void start(); }
      if (e.key === "/" && !inInput) { e.preventDefault(); setShowInput((v) => !v); }
      if ((e.key === "d" || e.key === "D") && !inInput) { e.preventDefault(); if (demoRef.current) stopDemo(); else void startDemo(); }
      if ((e.key === "i" || e.key === "I") && !inInput) { e.preventDefault(); setDebug((v) => !v); }
      if (e.key === "Escape") { setShowInput(false); stopDemo(); }
    };
    window.addEventListener("keydown", onKey);
    if (new URLSearchParams(window.location.search).has("demo")) void startDemo();
    return () => window.removeEventListener("keydown", onKey);
  }, [start, stop, startDemo, stopDemo]);

  const lastSeg = segments[segments.length - 1];
  const stateWord = lastSeg?.card ? STATE[lastSeg.card as Card] : lastSeg?.prelim ? STATE[lastSeg.prelim as Card] : "neutrální";
  const stateClass = stateWord === "pravda" ? "t" : stateWord === "nepravda" ? "f" : stateWord === "nesmysl" ? "s" : "n";

  return (
    <main className="stage">
      <section className="left">
        <LieStrip analyser={analyser} segments={segments} ampRef={ampRef} />

        <footer className="status">
          <div className={`state ${stateClass} ${lastSeg && !lastSeg.card && lastSeg.prelim ? "prelim" : ""}`}>{stateWord}</div>
          <div className="line">{transcript || (demo ? "…" : "")}</div>
        </footer>
      </section>

      <aside className="side" aria-label="Historie výroků">
        <div className="brand" aria-label="Wiki Fact Check">Wiki Fact Check</div>
        <div className="hist-wrap">
        <ol className="hist">
          {history.map((r, i) => (
            <li key={i} className={r.card.toLowerCase()}>
              <span className="tag">{TAG[r.card]}</span>
              <p className="claim">{r.claim}</p>
              {r.hint && <p className="ev">{r.hint}</p>}
              {r.evidence?.sentence && (
                <p className="ev">„{r.evidence.sentence}“ <a href={r.evidence.url} target="_blank" rel="noreferrer">{r.evidence.title} ↗</a></p>
              )}
            </li>
          ))}
        </ol>
        </div>

        {listening || demo ? (
          <div className="dock live">
            <div className="dock-label">{demo ? "simulace" : "mikrofon"}</div>
            <MicWave analyser={analyser} ampRef={ampRef} />
            <button className="stop" onClick={() => { if (demo) stopDemo(); else stop(); }} aria-label={demo ? "Zastavit simulaci" : "Zastavit mikrofon"}>
              <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" /></svg>
            </button>
          </div>
        ) : (
          <div className="dock idle">
            <button className="mic" onClick={() => void start()} aria-label="Mluvit">
              <svg viewBox="0 0 24 24" width="34" height="34" aria-hidden="true">
                <rect x="9" y="3" width="6" height="12" rx="3" fill="currentColor" />
                <path d="M5 11a7 7 0 0 0 14 0" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
                <path d="M12 18v3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
            <p className="hint"><kbd>d</kbd> pro simulaci</p>
          </div>
        )}
      </aside>

      {debug && (
        <aside className="debug">
          <div>API {API} · segmentů {segments.length} · poslední {lastSeg ? `${lastSeg.card ?? "?"} / prelim ${lastSeg.prelim ?? "-"} / ${lastSeg.end === null ? "otevřený" : "uzavřený"}` : "žádný"}</div>
          {last ? (
            <>
              <div>{last.final ? "FINAL" : "interim"} · {last.at}</div>
              {last.results.map((r, i) => (
                <div key={i}>
                  <b>{r.card}</b> {r.claim} {r.hint ? `· ${r.hint}` : ""} {r.evidence?.title ? `· ${r.evidence.title} (${r.evidence.source})` : ""}
                  {r.evidence?.sentence ? ` · „${r.evidence.sentence.slice(0, 90)}…“` : ""} {r.evidence ? `· e ${r.evidence.entail} c ${r.evidence.contra}${r.evidence.by_rule ? " · pravidlo" : ""}` : ""}
                  {r.timings ? ` · ${r.timings.total_ms} ms` : ""}
                </div>
              ))}
            </>
          ) : <div>zatím žádná odpověď</div>}
        </aside>
      )}

      {showInput && (
        <form className="hidden-input" onSubmit={(e) => { e.preventDefault(); submitFinal(typed.trim()); setTyped(""); }}>
          <input autoFocus value={typed} onChange={(e) => { setTyped(e.target.value); scheduleSpeculative(e.target.value); }} aria-label="Výrok" autoComplete="off" />
        </form>
      )}
    </main>
  );
}
