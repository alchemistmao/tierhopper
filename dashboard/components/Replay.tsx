"use client";
import { useEffect, useMemo, useState } from "react";

type Ev = { ts: string; type: string; text: string };
type Seg = { name: string; color: string; seconds: number };

const clock = (ms: number) => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const KIND: Record<string, string> = {
  hop: "var(--need)", attempt_ended: "var(--need)", runner_exit: "var(--need)", done: "var(--done)", failed: "var(--stop)",
  submit_failed: "var(--stop)", checkpoint: "var(--run)", approval_requested: "var(--need)",
};

/** Job replay: scrub or play through every event (hops, saves, stops) like a video. */
export function Replay({ events, journey }: { events: Ev[]; journey: Seg[] }) {
  const times = useMemo(() => events.map((e) => new Date(e.ts).getTime()), [events]);
  const start = times[0] ?? 0;
  const end = Math.max(times[times.length - 1] ?? 0, start + 1);
  const [i, setI] = useState(Math.max(events.length - 1, 0));
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);

  useEffect(() => {
    if (!playing) return;
    if (i >= events.length - 1) {
      setPlaying(false);
      return;
    }
    const timer = setTimeout(() => setI((n) => Math.min(n + 1, events.length - 1)), 900 / speed);
    return () => clearTimeout(timer);
  }, [playing, i, speed, events.length]);

  if (!events.length) return <p className="muted">No events yet.</p>;
  const total = journey.reduce((s, j) => s + j.seconds, 0) || 1;
  const pct = ((times[i] - start) / (end - start)) * 100;
  const play = () => {
    if (i >= events.length - 1) setI(0);
    setPlaying((p) => !p);
  };

  return (
    <div className="replay">
      <div className="replay-now">
        <span className="mono muted">{clock(times[i])}</span>
        <b>{events[i].text}</b>
        <span className="mono muted">{i + 1} / {events.length}</span>
      </div>
      <div className="replay-track">
        <div className="jbar" style={{ height: 14 }}>
          {journey.map((j, k) => <div key={k} className="jseg" style={{ width: `${(j.seconds / total) * 100}%`, background: j.color, padding: 0 }} title={j.name} />)}
        </div>
        {events.map((e, k) => (
          <button key={k} className={`replay-dot ${k === i ? "on" : ""}`} title={e.text} aria-label={e.text}
            style={{ left: `${((times[k] - start) / (end - start)) * 100}%`, background: KIND[e.type] ?? "var(--muted)" }}
            onClick={() => { setPlaying(false); setI(k); }} />
        ))}
        <div className="replay-head" style={{ left: `${pct}%` }} />
      </div>
      <input className="replay-range" type="range" min={0} max={events.length - 1} value={i} aria-label="Replay position"
        onChange={(e) => { setPlaying(false); setI(Number(e.target.value)); }} />
      <div className="actions" style={{ margin: "10px 0 0" }}>
        <button className="btn brand" onClick={play}>{playing ? "Pause" : i >= events.length - 1 ? "Replay" : "Play"}</button>
        <button className="btn" onClick={() => { setPlaying(false); setI((n) => Math.max(n - 1, 0)); }}>← Step</button>
        <button className="btn" onClick={() => { setPlaying(false); setI((n) => Math.min(n + 1, events.length - 1)); }}>Step →</button>
        {[1, 2, 4].map((s) => (
          <button key={s} className="btn" style={speed === s ? { borderColor: "var(--brand)", color: "var(--brand)" } : undefined} onClick={() => setSpeed(s)}>{s}×</button>
        ))}
      </div>
      <div className="events" style={{ marginTop: 14 }}>
        {events.slice(0, i + 1).slice(-6).map((e, k, arr) => (
          <div className="event" key={k} style={{ opacity: k === arr.length - 1 ? 1 : 0.55 }}>
            <span className="mono muted">{clock(new Date(e.ts).getTime())}</span><span>{e.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
