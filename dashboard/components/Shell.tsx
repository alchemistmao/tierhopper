"use client";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { Logo } from "./Logo";
import { PushButton } from "./PushButton";
import { play } from "./sound";
import { jobAction } from "./usePoll";

type Plan =
  | { kind: "answer"; text: string }
  | { kind: "action"; action: string; args: Record<string, unknown>; summary: string; confirm: boolean };

const TABS = [
  { href: "/", label: "Projects" },
  { href: "/map", label: "Hop map" },
  { href: "/providers", label: "Providers" },
  { href: "/reports", label: "Reports" },
];
const THEMES = [
  { id: "mission", label: "Mission" },
  { id: "crt", label: "CRT" },
  { id: "light", label: "Light" },
];

export type PaletteItem = { label: string; hint?: string; run: () => void; keepOpen?: boolean };

export function Shell({ children, live = true, items = [] }: { children: React.ReactNode; live?: boolean; items?: PaletteItem[] }) {
  const path = usePathname();
  const router = useRouter();
  const [theme, setTheme] = useState("mission");
  const [sounds, setSounds] = useState(false);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const [asking, setAsking] = useState(false);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    try {
      setTheme(localStorage.getItem("th-theme") ?? "mission");
      setSounds(localStorage.getItem("th-sounds") === "on");
    } catch {
      // storage blocked
    }
  }, []);

  const applyTheme = (t: string) => {
    setTheme(t);
    document.documentElement.dataset.theme = t;
    try {
      localStorage.setItem("th-theme", t);
    } catch {}
    play("tick");
  };
  const toggleSounds = () => {
    const next = !sounds;
    setSounds(next);
    try {
      localStorage.setItem("th-sounds", next ? "on" : "off");
    } catch {}
    if (next) play("chime");
  };

  const all: PaletteItem[] = useMemo(
    () => [
      ...items,
      ...TABS.map((t) => ({ label: `Go to ${t.label}`, run: () => router.push(t.href) })),
      ...THEMES.map((t) => ({ label: `Theme: ${t.label}`, run: () => applyTheme(t.id) })),
      { label: sounds ? "Turn sounds off" : "Turn sounds on", run: toggleSounds },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [items, sounds],
  );
  // Empty palette: a few things to ask and where to go. Projects and the rest appear as you type.
  const typed = query.trim().toLowerCase();
  const starters: PaletteItem[] = [
    ...["What is running right now?", "How much free credit is left?", "How much did I save this month?"].map((q) => ({
      label: q,
      hint: "ask",
      run: () => setQuery(q),
      keepOpen: true,
    })),
    ...TABS.map((t) => ({ label: `Go to ${t.label}`, run: () => router.push(t.href) })),
  ];
  const shown = typed ? all.filter((i) => i.label.toLowerCase().includes(typed)) : starters;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
        setQuery("");
        setSel(0);
        setPlan(null);
        setNote(null);
      } else if (e.key === "Escape") closePalette();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const runItem = (i: PaletteItem | undefined) => {
    if (!i) return;
    if (!i.keepOpen) setOpen(false);
    i.run();
  };

  const closePalette = () => {
    setOpen(false);
    setPlan(null);
    setNote(null);
  };

  /** Runs a plan proposed by Claude. State-changing ones only get here after the user confirmed. */
  const execute = async (p: Plan) => {
    if (p.kind !== "action") return;
    const id = String(p.args.job_id ?? "");
    const actions: Record<string, () => Promise<string | null> | string | null> = {
      open_project: () => (router.push(`/jobs/${id}`), null),
      navigate: () => (router.push({ projects: "/", map: "/map", providers: "/providers" }[String(p.args.tab)] ?? "/"), null),
      set_theme: () => (applyTheme(String(p.args.theme)), null),
      pause_project: async () => ((await jobAction(id, "pause")).ok ? null : "Could not pause it."),
      resume_project: async () => ((await jobAction(id, "resume")).ok ? null : "Could not resume it."),
      approve_project: async () => ((await jobAction(id, "approve")).ok ? null : "Could not approve it."),
      rerun_project: async () => {
        const r = await jobAction(id, "rerun", { min_vram_gb: p.args.min_vram_gb, cheapest: p.args.cheapest });
        if (!r.ok) return "Could not start it.";
        router.push(`/jobs/${r.body.job_id}`);
        return null;
      },
      get_results: async () => {
        const r = await jobAction(id, "results");
        const links = (r.body.links ?? []) as { url: string }[];
        links.forEach((l) => window.open(l.url, "_blank", "noopener"));
        return links.length ? null : "Nothing saved yet for that project.";
      },
    };
    setAsking(true);
    const error = await actions[p.action]?.();
    setAsking(false);
    if (error) setNote(error);
    else {
      play("chime");
      closePalette();
    }
  };

  const ask = async () => {
    if (!query.trim() || asking) return;
    setAsking(true);
    setPlan(null);
    setNote(null);
    try {
      const res = await fetch("/api/command", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: query }),
      });
      const p = (await res.json()) as Plan;
      setAsking(false);
      if (p.kind === "action" && !p.confirm) await execute(p);
      else setPlan(p);
    } catch {
      setAsking(false);
      setNote("Could not reach TierHopper.");
    }
  };

  return (
    <>
      <header className="top">
        <Logo />
        <nav className="tabs">
          {TABS.map((t) => (
            <Link key={t.href} href={t.href} className={`tab ${path === t.href ? "on" : ""}`}>{t.label}</Link>
          ))}
        </nav>
        <div className="spacer" />
        <button className="kbar" onClick={() => setOpen(true)}>
          <span>Ask or run anything…</span><kbd className="kbd">⌘K</kbd>
        </button>
        <div className="live" title={live ? "Live" : "Reconnecting"}><span className={`dot ${live ? "" : "off"}`} />{live ? "Live" : "Offline"}</div>
        <div className="themes" role="group" aria-label="Theme">
          {THEMES.map((t) => (
            <button key={t.id} className={theme === t.id ? "on" : ""} onClick={() => applyTheme(t.id)}>{t.label}</button>
          ))}
        </div>
        <PushButton />
        <button className="iconbtn" onClick={toggleSounds} aria-pressed={sounds} title={sounds ? "Sounds on" : "Sounds off"}>
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
            <path d="M11 5 6 9H2v6h4l5 4V5z" />{!sounds && <path d="m23 9-6 6M17 9l6 6" />}
          </svg>
        </button>
      </header>
      <main>{children}</main>
      {open && (
        <div className="scrim" onClick={(e) => e.target === e.currentTarget && closePalette()}>
          <div className="card palette" role="dialog" aria-label="Command palette">
            <input
              autoFocus
              value={query}
              placeholder="Ask or type a command… e.g. “pause the layout project”"
              onChange={(e) => { setQuery(e.target.value); setSel(0); setPlan(null); setNote(null); }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") setSel((n) => Math.min(n + 1, shown.length));
                if (e.key === "ArrowUp") setSel((n) => Math.max(n - 1, 0));
                if (e.key === "Enter") {
                  if (plan?.kind === "action") execute(plan);
                  else if (shown[sel]) runItem(shown[sel]);
                  else ask();
                }
              }}
            />
            {plan?.kind === "action" && (
              <div className="pal-ai">
                <b>Plan</b> · {plan.summary}
                <div className="need-actions">
                  <button className="btn brand" disabled={asking} onClick={() => execute(plan)}>Confirm ↵</button>
                  <button className="btn" onClick={() => setPlan(null)}>Cancel</button>
                </div>
              </div>
            )}
            {plan?.kind === "answer" && <div className="pal-ai">{plan.text}</div>}
            {note && <div className="pal-ai" style={{ borderColor: "var(--stop)" }}>{note}</div>}
            {asking && <div className="pal-item"><span className="thinking">Thinking</span></div>}
            {!plan && !asking && (
              <>
                {shown.slice(0, 8).map((i, idx) => (
                  <div key={`${idx}-${i.label}`} className={`pal-item ${idx === sel ? "on" : ""}`} onMouseEnter={() => setSel(idx)} onClick={() => runItem(i)}>
                    <span>{i.label}</span>{i.hint && <span className="kbd">{i.hint}</span>}
                  </div>
                ))}
                {query.trim() && (
                  <div className={`pal-item ${sel >= Math.min(shown.length, 8) ? "on" : ""}`} onMouseEnter={() => setSel(shown.length)} onClick={ask}>
                    <span>Ask TierHopper: “{query}”</span><span className="kbd">↵</span>
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
