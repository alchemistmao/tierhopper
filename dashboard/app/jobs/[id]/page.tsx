"use client";
import Link from "next/link";
import { use, useState } from "react";
import { Replay } from "@/components/Replay";
import { Shell } from "@/components/Shell";
import { jobAction, usePoll } from "@/components/usePoll";
import { duration, money } from "@/lib/labels";
import type { JobDetail } from "@/lib/data";

const clock = (iso: string | null) =>
  iso ? new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : "—";

export default function JobPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: d, live, reload } = usePoll<JobDetail>(`/api/jobs/${id}`);
  const [tech, setTech] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const act = async (action: string) => {
    setBusy(true);
    const { ok, body } = await jobAction(id, action);
    setBusy(false);
    if (action === "results" && ok) {
      const links = (body.links ?? []) as { url: string; kind: string }[];
      if (!links.length) setMsg("Nothing saved yet — try again after the first save.");
      links.forEach((l) => window.open(l.url, "_blank", "noopener"));
      if (links.length) setMsg(links.some((l) => l.kind === "checkpoint") ? "Downloading the latest save (work in progress)." : "Downloading the results.");
    } else setMsg(ok ? null : body.error ?? "Could not reach TierHopper");
    reload();
  };

  if (!d) return <Shell live={live}><Link href="/" className="back">← All projects</Link><div className="muted">Loading…</div></Shell>;

  const total = d.journey.reduce((s, j) => s + j.seconds, 0) + d.remainingSeconds;
  const nowPct = total ? ((total - d.remainingSeconds) / total) * 100 : 0;
  const canPause = ["running", "wait", "need"].includes(d.statusKey);

  return (
    <Shell live={live}>
      <div className="rise">
        <Link href="/" className="back">← All projects</Link>
        <div className="dtitle">
          <h1>{d.name}</h1>
          <span className={`pill s-${d.statusKey} ${d.statusKey === "running" ? "running" : ""}`}>{d.statusLabel}</span>
        </div>
        {msg && <div className="toast">{msg}</div>}
        {d.approval && (
          <div className="card section">
            <div className="h">Needs your OK</div>
            <p style={{ margin: "10px 0 14px" }}>{d.approval.reason}</p>
            <div className="actions" style={{ margin: 0 }}>
              <button className="btn primary" disabled={busy} onClick={() => act("approve")}>Approve</button>
              <button className="btn" disabled={busy} onClick={() => act("deny")}>{d.approval.paid ? "Wait for free credit" : "Cancel"}</button>
            </div>
          </div>
        )}

        {(d.count || d.progress > 0) && d.statusKey !== "done" && (
          <section className="card section progress-card">
            <div className="progress-head">
              <b>{d.count ?? `${Math.round(d.progress * 100)}% done`}</b>
              <span className="mono muted">{Math.round(d.progress * 100)}%{d.statusKey === "running" ? ` · ~${d.timeLeft} left` : ""}</span>
            </div>
            <div className="bar" style={{ height: 12 }}>
              <div className={`fill ${d.statusKey === "running" ? "shine" : ""} s-${d.statusKey}`} style={{ width: `${Math.round(d.progress * 100)}%`, background: "var(--c)" }} />
            </div>
            {d.pace && <small className="muted">{d.pace} at the current pace</small>}
          </section>
        )}

        <div className="tiles">
          <div className="card tile"><div className="h">Running for</div><b>{d.runningFor}</b></div>
          <div className="card tile"><div className="h">Time left</div><b>{d.statusKey === "done" ? "—" : `~${d.timeLeft}`}</b></div>
          <div className="card tile"><div className="h">Providers used</div><b>{d.providersUsed}</b></div>
          <div className="card tile save"><div className="h">Saved</div><b>{money(d.saved)}</b></div>
        </div>

        <section className="card section">
          <div className="h">Journey</div>
          {d.journey.length ? (
            <div className="journey">
              <span className="mark" style={{ left: 0 }}>started {clock(d.startedAt)}</span>
              <span className="mark" style={{ right: 0 }}>{d.statusKey === "done" ? "finished" : "done ≈"} {clock(d.finishAt)}</span>
              <div className="jbar">
                {d.journey.map((j, i) => (
                  <div key={i} className="jseg" style={{ width: `${(j.seconds / total) * 100}%`, background: j.color }} title={`${j.name} · ${duration(j.seconds)}`}>
                    {j.name} · {duration(j.seconds)}
                  </div>
                ))}
                {d.remainingSeconds > 0 && <div className="jrest">≈ {duration(d.remainingSeconds)} to go</div>}
              </div>
              {d.statusKey !== "done" && <div className="now-line" style={{ left: `${nowPct}%` }} />}
            </div>
          ) : (
            <p className="muted" style={{ marginTop: 12 }}>Not started yet.</p>
          )}
        </section>

        <section className="card section">
          <div className="h" style={{ marginBottom: 6 }}>Stops</div>
          {d.stops.map((s) => (
            <div className="stop" key={s.provider}>
              <div className="pname"><i style={{ background: s.color }} />{s.name}</div>
              <div className="bar" style={{ height: 6 }}><div className="fill" style={{ width: `${Math.round(s.share * 100)}%`, background: s.color }} /></div>
              <div className="mono muted">{Math.round(s.share * 100)}% · {duration(s.seconds)}</div>
              <div className="why">{s.why}</div>
            </div>
          ))}
          {!d.stops.length && <p className="muted">No stops yet.</p>}
        </section>

        <div className="actions">
          {canPause && <button className="btn" disabled={busy} onClick={() => act("pause")}>Pause</button>}
          {d.statusKey === "paused" && <button className="btn" disabled={busy} onClick={() => act("resume")}>Resume</button>}
          <button className="btn brand" disabled={busy} onClick={() => act("results")}>{d.statusKey === "done" ? "Get results" : "Get results so far"}</button>
          <button className="btn" onClick={() => setTech((t) => !t)}>{tech ? "Hide technical details" : "Technical details →"}</button>
        </div>

        {tech && (
          <section className="card section rise">
            {d.gpu && (
              <>
                <div className="h" style={{ marginBottom: 10 }}>GPU now</div>
                <div className="tiles" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
                  <div className="card tile"><div className="h">Utilisation</div><b>{d.gpu.util ?? "—"}%</b></div>
                  <div className="card tile"><div className="h">Memory</div><b>{d.gpu.vramUsed != null ? `${(d.gpu.vramUsed / 1024).toFixed(1)} / ${((d.gpu.vramTotal ?? 0) / 1024).toFixed(0)} GB` : "—"}</b></div>
                  <div className="card tile"><div className="h">Trend</div><Spark values={d.gpuHistory} /></div>
                </div>
              </>
            )}
            {!!d.logTail.length && (
              <>
                <div className="h" style={{ marginBottom: 6 }}>Latest output</div>
                <pre className="msg" style={{ marginBottom: 20 }}>{d.logTail.join("\n")}</pre>
              </>
            )}
            <div className="h" style={{ marginBottom: 10 }}>Replay</div>
            <Replay events={d.events} journey={d.journey} />
            <div className="h" style={{ margin: "20px 0 10px" }}>Attempts</div>
            {d.technical.map((t, i) => (
              <div key={i} style={{ marginBottom: 14 }}>
                <div className="mono">{t.provider} · {t.gpu} · {t.status}{t.reason ? ` · ${t.reason}` : ""}{t.link && <> · <a href={t.link} target="_blank" rel="noopener noreferrer" style={{ color: "var(--brand)" }}>open</a></>}</div>
                {t.message && <pre className="msg">{t.message}</pre>}
              </div>
            ))}
          </section>
        )}
      </div>
    </Shell>
  );
}

function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return <b>—</b>;
  const w = 120, h = 28;
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * w},${h - (Math.min(100, v) / 100) * h}`).join(" ");
  return (
    <svg width={w} height={h} style={{ marginTop: 10 }} aria-label="GPU utilisation trend">
      <polyline points={pts} fill="none" stroke="var(--run)" strokeWidth="1.5" />
    </svg>
  );
}
