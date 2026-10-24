"use client";

import { useEffect, useRef, type MutableRefObject } from "react";

export type Seg = { id: number; start: number; end: number | null; card: string | null; prelim: string | null };

const LEVEL: Record<string, number> = { PRAVDA: 1, NEPRAVDA: -1, NESMYSL: 0, NEOVERITELNE: 0 };

const KEEP_MS = 30 * 60 * 1000; // history kept for scrolling back

/** Truth seismograph. Height = verdict (up true, down false, middle neutral), needle swing = voice.
 *  While `paused` the paper stops; wheel, trackpad or drag scrolls back through the recorded history. */
export default function LieStrip({ analyser, segments, ampRef, windowMs = 7000, paused = false }: { analyser: AnalyserNode | null; segments: Seg[]; ampRef?: MutableRefObject<number>; windowMs?: number; paused?: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const samples = useRef<{ t: number; v: number; r: number }[]>([]);
  const segRef = useRef(segments);
  const anRef = useRef(analyser);
  const frozenAt = useRef<number | null>(null); // time shown at the right edge while paused
  const backMs = useRef(0); // how far the view is scrolled back from frozenAt
  segRef.current = segments;
  anRef.current = analyser;

  useEffect(() => {
    frozenAt.current = paused ? performance.now() : null;
    backMs.current = 0;
  }, [paused]);

  // scroll back while paused: wheel / trackpad (either axis) or drag the paper
  useEffect(() => {
    const c = canvasRef.current;
    if (!c) return;
    const maxBack = () => {
      const pts = samples.current;
      if (frozenAt.current === null || !pts.length) return 0;
      return Math.max(0, frozenAt.current - pts[0].t - windowMs);
    };
    const shift = (px: number) => {
      if (frozenAt.current === null) return;
      backMs.current = Math.max(0, Math.min(maxBack(), backMs.current + (px / c.clientWidth) * windowMs));
    };
    const onWheel = (e: WheelEvent) => {
      if (frozenAt.current === null) return;
      e.preventDefault();
      const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
      shift(-d); // scroll right / down = forward in time
    };
    let dragX: number | null = null;
    const onDown = (e: PointerEvent) => { if (frozenAt.current === null) return; dragX = e.clientX; c.setPointerCapture(e.pointerId); };
    const onMove = (e: PointerEvent) => { if (dragX === null) return; shift(e.clientX - dragX); dragX = e.clientX; };
    const onUp = () => { dragX = null; };
    c.addEventListener("wheel", onWheel, { passive: false });
    c.addEventListener("pointerdown", onDown);
    c.addEventListener("pointermove", onMove);
    c.addEventListener("pointerup", onUp);
    c.addEventListener("pointercancel", onUp);
    return () => {
      c.removeEventListener("wheel", onWheel);
      c.removeEventListener("pointerdown", onDown);
      c.removeEventListener("pointermove", onMove);
      c.removeEventListener("pointerup", onUp);
      c.removeEventListener("pointercancel", onUp);
    };
  }, [windowMs]);

  useEffect(() => {
    let raf = 0;
    const buf = new Uint8Array(512);
    const css = getComputedStyle(document.documentElement);
    const C_TRUE = css.getPropertyValue("--true").trim() || "#22a85d";
    const C_FALSE = css.getPropertyValue("--false").trim() || "#e04b4b";
    const C_NONE = css.getPropertyValue("--neutral").trim() || "#8E877C";
    const C_INK = css.getPropertyValue("--ink").trim() || "#2A2724";
    const C_NONSENSE = css.getPropertyValue("--nonsense").trim() || "#C9941D";

    const HOLD_MS = 8000;
    const segAt = (t: number): { level: number; card: string | null } => {
      let held: Seg | null = null;
      for (const s of segRef.current) {
        if (t >= s.start && (s.end === null || t <= s.end)) {
          if (s.card) return { level: LEVEL[s.card] ?? 0, card: s.card };
          if (s.prelim) return { level: 0.5 * (LEVEL[s.prelim] ?? 0), card: s.prelim };
          return { level: 0, card: null }; // a new sentence is being spoken: neutral until we know
        }
        if (s.end !== null && s.end <= t && (!held || s.end > (held.end as number))) held = s;
      }
      // after a sentence the needle holds its verdict, then relaxes to neutral
      if (!held) return { level: 0, card: null };
      const age = t - (held.end as number);
      const lv = held.card ? LEVEL[held.card] ?? 0 : 0;
      const k = age < HOLD_MS ? 1 : Math.max(0, 1 - (age - HOLD_MS) / 1500);
      return { level: lv * k, card: k > 0 ? held.card : null };
    };
    const levelAt = (t: number) => segAt(t).level;

    const draw = () => {
      const c = canvasRef.current;
      if (!c) return;
      const dpr = window.devicePixelRatio || 1;
      const W = c.clientWidth, H = c.clientHeight;
      if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
      const ctx = c.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const now = performance.now();

      let v = 0;
      const an = anRef.current;
      if (an) {
        an.getByteTimeDomainData(buf);
        let s = 0;
        for (let i = 0; i < buf.length; i++) { const d = (buf[i] - 128) / 128; s += d * d; }
        v = Math.sqrt(s / buf.length);
      } else if (ampRef && ampRef.current > 0) {
        v = ampRef.current * (0.7 + 0.6 * Math.random()); // simulated voice
      }
      const all = samples.current;
      // seismograph needle: alternating sign, random magnitude, occasional big spike (kept per sample so the past does not flicker)
      const sign = all.length % 2 ? 1 : -1;
      const r = sign * (0.25 + 0.75 * Math.random()) * (Math.random() < 0.06 ? 2.2 : 1);
      all.push({ t: now, v, r });
      if (all[0].t < now - KEEP_MS) all.splice(0, all.findIndex((p) => p.t >= now - KEEP_MS));

      // the time at the right edge: live, or frozen (and scrolled back) while paused
      const viewEnd = frozenAt.current !== null ? frozenAt.current - backMs.current : now;
      const t0 = viewEnd - windowMs - 300;
      let i0 = all.length - 1;
      while (i0 > 0 && all[i0 - 1].t >= t0) i0--;
      let i1 = all.length;
      while (i1 > i0 && all[i1 - 1].t > viewEnd + 50) i1--;
      const pts = all.slice(i0, i1);

      // smoothed level per sample: moving average over ±220 ms so the wave glides between verdicts
      const lv = pts.map((p) => levelAt(p.t));
      const sm = new Float32Array(pts.length);
      let lo = 0, hi = 0, acc = 0;
      for (let i = 0; i < pts.length; i++) {
        while (hi < pts.length && pts[hi].t <= pts[i].t + 110) { acc += lv[hi]; hi++; }
        while (lo < i && pts[lo].t < pts[i].t - 110) { acc -= lv[lo]; lo++; }
        sm[i] = acc / Math.max(1, hi - lo);
      }

      const x = (t: number) => W - ((viewEnd - t) / windowMs) * W;
      const mid = H / 2, lift = H * 0.36; // true/false lines; the needle swing stays inside the margin to the canvas edge
      const margin = H / 2 - lift - 3;
      ctx.clearRect(0, 0, W, H);

      // stippled field, denser towards the true/false bands, like a halftone
      ctx.fillStyle = C_INK;
      const step = 14;
      for (let gy = step / 2; gy < H; gy += step) {
        const d = Math.abs(gy - mid) / (H / 2);
        ctx.globalAlpha = 0.05 + d * 0.16;
        for (let gx = ((gy / step) % 2) * step / 2; gx < W; gx += step) { ctx.beginPath(); ctx.arc(gx, gy, 0.9, 0, Math.PI * 2); ctx.fill(); }
      }
      ctx.globalAlpha = 1;
      ctx.strokeStyle = C_INK;
      ctx.globalAlpha = 0.22;
      ctx.lineWidth = 1;
      for (const y of [mid - lift, mid, mid + lift]) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
      ctx.globalAlpha = 0.6;
      ctx.fillStyle = C_INK;
      ctx.font = "12px ui-monospace, 'SF Mono', Menlo, monospace";
      ctx.fillText("pravda", 16, mid - lift - 8);
      ctx.fillText("neutrální", 16, mid - 8);
      ctx.fillText("nepravda", 16, mid + lift - 8);
      if (frozenAt.current !== null) {
        const back = backMs.current;
        const label = back > 100 ? `pauza · −${(back / 1000).toFixed(1)} s` : "pauza";
        ctx.fillText(label, W - ctx.measureText(label).width - 16, mid - lift - 8);
      }
      ctx.globalAlpha = 1;

      ctx.lineWidth = 1;
      ctx.lineJoin = "miter";
      ctx.lineCap = "butt";
      const yOf = (i: number) => {
        const l = sm[i];
        const voice = Math.min(1, pts[i].v * 6);
        // neutral: flat with hairline jitter; true/false: full needle swing inside its band
        const band = Math.abs(l) < 0.2 ? 1.5 + voice * 3 : 2 + voice * margin * 0.9;
        const off = Math.max(-margin, Math.min(margin, pts[i].r * band));
        return mid - l * lift + off;
      };
      let runColor = "";
      for (let i = 1; i < pts.length; i++) {
        const l = sm[i];
        const color = l > 0.25 ? C_TRUE : l < -0.25 ? C_FALSE : segAt(pts[i].t).card === "NESMYSL" ? C_NONSENSE : C_NONE;
        if (color !== runColor) {
          if (runColor) ctx.stroke();
          runColor = color;
          ctx.strokeStyle = color;
          ctx.beginPath();
          ctx.moveTo(x(pts[i - 1].t), yOf(i - 1));
        }
        ctx.lineTo(x(pts[i].t), yOf(i));
      }
      if (runColor) ctx.stroke();

      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [windowMs, ampRef]);

  return <canvas ref={canvasRef} className={`wave ${paused ? "paused" : ""}`} aria-hidden="true" />;
}
