"use client";

import { useEffect, useRef, type MutableRefObject } from "react";

/** Small oscilloscope of the microphone input (or a synthetic one for the demo). */
export default function MicWave({ analyser, ampRef }: { analyser: AnalyserNode | null; ampRef?: MutableRefObject<number> }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const anRef = useRef(analyser);
  anRef.current = analyser;

  useEffect(() => {
    let raf = 0;
    let buf = new Uint8Array(0);
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue("--ink").trim() || "#2A2724";
    const N = 220;
    const draw = () => {
      const c = ref.current;
      if (!c) return;
      const dpr = window.devicePixelRatio || 1;
      const W = c.clientWidth, H = c.clientHeight;
      if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
      const ctx = c.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);

      const an = anRef.current;
      const t = performance.now();
      let at: (i: number) => number;
      if (an) {
        if (buf.length !== an.fftSize) buf = new Uint8Array(an.fftSize); // the analyser fills exactly fftSize samples
        an.getByteTimeDomainData(buf);
        at = (i) => ((buf[Math.floor((i / N) * buf.length)] - 128) / 128) * 2.4;
      } else {
        const a = ampRef?.current ?? 0; // simulated voice: a couple of sines plus noise, scaled by the demo amplitude
        at = (i) => a * (Math.sin(i * 0.31 + t * 0.021) * 0.7 + Math.sin(i * 1.1 + t * 0.047) * 0.35 + (Math.random() - 0.5) * 0.5);
      }

      ctx.strokeStyle = ink;
      ctx.globalAlpha = 0.22;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, H / 2); ctx.lineTo(W, H / 2); ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.lineWidth = 1.2;
      ctx.lineJoin = "round";
      ctx.beginPath();
      for (let i = 0; i < N; i++) {
        const x = (i / (N - 1)) * W;
        const y = H / 2 - Math.max(-1, Math.min(1, at(i))) * (H / 2 - 2);
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      }
      ctx.stroke();
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [ampRef]);

  return <canvas ref={ref} className="micwave" aria-hidden="true" />;
}
