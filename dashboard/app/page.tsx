"use client";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { Shell, type PaletteItem } from "@/components/Shell";
import { play } from "@/components/sound";
import { jobAction, usePoll } from "@/components/usePoll";
import { ago, money } from "@/lib/labels";
import type { Overview } from "@/lib/data";

/** "Oct 2, 06:25 → 16:36" in the viewer's time zone (adds the end date when it differs). */
function runDates(startIso: string, endIso: string | null) {
  const day = (d: Date) => d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const time = (d: Date) => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const start = new Date(startIso);
  if (!endIso) return `${day(start)}, ${time(start)} → …`;
  const end = new Date(endIso);
  return `${day(start)}, ${time(start)} → ${day(end) === day(start) ? "" : `${day(end)}, `}${time(end)}`;
}

export default function Home() {
  const { data, live, reload } = usePoll<Overview>("/api/overview");
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const prev = useRef<Record<string, string>>({});

  // Sounds on status changes: chime when something finishes, low tone when it needs you.
  useEffect(() => {
    if (!data) return;
    for (const p of data.projects) {
      const before = prev.current[p.id];
      if (before && before !== p.statusKey) play(p.statusKey === "done" ? "chime" : p.statusKey === "need" ? "low" : "tick");
      prev.current[p.id] = p.statusKey;
    }
  }, [data]);

  const act = async (id: string, action: string) => {
    setBusy(id + action);
    const { ok, body } = await jobAction(id, action);
    setBusy(null);
    setNote(ok ? null : body.error ?? "Could not reach TierHopper");
    reload();
  };

  const items: PaletteItem[] = useMemo(
    () => (data?.projects ?? []).map((p) => ({
      label: `Open ${p.name}`,
      hint: `${p.statusLabel} · ${p.statusKey === "running" ? p.eta : ago(p.when)}`,
      run: () => router.push(`/jobs/${p.id}`),
    })),
    [data, router],
  );

  const onRowKey = (e: React.KeyboardEvent<HTMLDivElement>, id: string) => {
    const rows = [...document.querySelectorAll<HTMLDivElement>(".row")];
    const i = rows.indexOf(e.currentTarget);
    if (e.key === "Enter") router.push(`/jobs/${id}`);
    if (e.key === "j") rows[Math.min(i + 1, rows.length - 1)]?.focus();
    if (e.key === "k") rows[Math.max(i - 1, 0)]?.focus();
  };

  const pct = Math.round((data?.progress ?? 0) * 100);
  const c = data?.counts ?? {};
  return (
    <Shell live={live} items={items}>
      {note && <div className="toast">{note}</div>}
      <section className="card overall">
        <div className="overall-top">
          <div>
            <div className="h">All projects</div>
            <div className="big"><span className="mono">{data ? pct : "–"}</span>% done</div>
          </div>
          <div className="stats">
            <div className="stat"><b>{data?.timeLeft ?? "—"}</b><small>time left</small></div>
            <div className="stat"><b>{money(data?.spentMonth)}</b><small>spent this month</small></div>
            <div className="stat save"><b>{money(data?.savedMonth)}</b><small>saved vs. market</small></div>
          </div>
        </div>
        <div className="bar">
          <div className="fill shine" style={{ width: `${pct}%`, background: "linear-gradient(90deg,var(--run),var(--brand))" }} />
        </div>
        <div className="legend">
          {!!c.running && <span><i style={{ background: "var(--run)" }} />{c.running} running</span>}
          {!!c.need && <span><i style={{ background: "var(--need)" }} />{c.need} need{c.need === 1 ? "s" : ""} your OK</span>}
          {!!c.wait && <span><i style={{ background: "var(--wait)" }} />{c.wait} waiting in line</span>}
          {!!c.done && <span><i style={{ background: "var(--done)" }} />{c.done} done</span>}
          {!!c.stopped && <span><i style={{ background: "var(--stop)" }} />{c.stopped} stopped</span>}
          <span className="spacer" />
          {data && data.spentMonth === 0 && data.streakDays > 0 && <span className="streak">🔥 {data.streakDays} day{data.streakDays === 1 ? "" : "s"} at $0</span>}
        </div>
      </section>

      <div className="grid">
        <section className="card" style={{ overflow: "hidden" }}>
          <div className="list-head"><div className="h">Projects</div><span className="kbd">j / k to move · enter to open</span></div>
          {data && !data.projects.length && (
            <div className="empty">No projects yet. From Claude Code, ask to run a job with TierHopper.</div>
          )}
          {data?.projects.map((p) => (
            <div key={p.id} className="row rise" tabIndex={0} onKeyDown={(e) => onRowKey(e, p.id)}
              onClick={(e) => { if (!(e.target as HTMLElement).closest("button")) { play("tick"); router.push(`/jobs/${p.id}`); } }}>
              <div className="name">
                {p.project && p.project !== "default" ? `${p.project} · ` : ""}{p.name}
                <small>{p.line}</small>
                {p.approval && (
                  <div className="need-actions">
                    <button className="btn primary" disabled={!!busy} onClick={() => act(p.id, "approve")}>Approve</button>
                    <button className="btn" disabled={!!busy} onClick={() => act(p.id, "deny")}>{p.approval.paid ? "Wait for free credit" : "Cancel"}</button>
                  </div>
                )}
              </div>
              <div className="bar"><div className={`fill ${p.statusKey === "running" ? "shine" : ""} s-${p.statusKey}`} style={{ width: `${Math.round(p.progress * 100)}%`, background: "var(--c)" }} /></div>
              <div><span className={`pill s-${p.statusKey} ${p.statusKey === "running" ? "running" : ""}`}>{p.statusLabel}</span></div>
              <div className="eta">
                {p.count && <span className="count">{p.count}</span>}
                {p.eta && <span className="left">{p.eta}</span>}
                <span className="dates" title="Started → finished, in your local time">{runDates(p.startedAt, p.finishedAt)}</span>
              </div>
            </div>
          ))}
        </section>

        <aside className="side">
          <section className="card">
            <div className="h">Free credit left</div>
            {data?.credits.map((cr) => (
              <div className="credit" key={cr.id}>
                <div className="credit-top"><span>{cr.name}{cr.paid && <> <span className="kbd">paid</span></>}</span><span>{cr.left}</span></div>
                <div className="bar"><div className="fill" style={{ width: `${cr.pct}%`, background: cr.color }} /></div>
                <small>{cr.note}</small>
              </div>
            ))}
            {data && !data.credits.length && <div className="credit muted">No providers connected yet.</div>}
          </section>
          <section className="card">
            <div className="h">Savings</div>
            <div className="saving-big mono">{money(data?.savedMonth)}</div>
            <small className="muted">this month vs. on-demand GPU prices · {(data?.gpuHoursMonth ?? 0).toFixed(1)} GPU-hours</small>
            <div className="milestones">
              {data?.milestones.map((m) => <div key={m.label} className={`ms ${m.hit ? "hit" : ""}`}>{m.label}</div>)}
            </div>
          </section>
        </aside>
      </div>
    </Shell>
  );
}
