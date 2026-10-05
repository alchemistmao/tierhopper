import "server-only";
import { dataClient } from "./supabase";
import { PROVIDER_COLOR, STATUS, WHY, duration, money } from "./labels";

type Row = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

export type ProjectRow = {
  id: string;
  name: string;
  project: string;
  status: string;
  statusKey: string;
  statusLabel: string;
  progress: number;
  count: string | null;
  line: string;
  eta: string;
  etaSeconds: number | null;
  when: string;
  startedAt: string;
  finishedAt: string | null;
  approval?: { reason: string; paid: boolean };
};

export type Overview = {
  progress: number;
  timeLeft: string;
  spentMonth: number;
  savedMonth: number;
  streakDays: number;
  counts: Record<string, number>;
  projects: ProjectRow[];
  credits: { id: string; name: string; left: string; pct: number; note: string; color: string; paid: boolean }[];
  milestones: { label: string; hit: boolean }[];
  gpuHoursMonth: number;
  /** Where each running project really is; `from` is set only for a few minutes after a real switch. */
  moves: { job: string; name: string; to: string; from: string | null }[];
};

const now = () => Date.now();
/** Savings = market value of GPU time that cost nothing. Paid time shows up under "spent" instead. */
const savedOn = (a: Row) => (Number(a.cost_usd || 0) > 0 ? 0 : Number(a.market_cost_usd || 0));
const secondsSince = (iso?: string | null) => (iso ? (now() - new Date(iso).getTime()) / 1000 : 0);

function eta(job: Row): number | null {
  if (job.status === "done") return 0;
  const p = Number(job.progress) || 0;
  const elapsed = secondsSince(job.started_at);
  if (job.status === "running" && p > 0.02 && elapsed > 0) return (elapsed / p) * (1 - p);
  const hours = job.estimate?.gpu_hours;
  return hours ? Math.max(hours * 3600 * (1 - p) - (job.status === "running" ? elapsed : 0), 60) : null;
}

/** "37 of 150 pages", summed over the job's parts when they count the same thing. */
function counter(job: Row): { done: number; total: number; unit: string; label: string } | null {
  const parts = Object.values((job.estimate?.counters ?? {}) as Record<string, Row>);
  if (!parts.length) return null;
  const unit = parts.every((p) => p.unit === parts[0].unit) ? String(parts[0].unit) : "items";
  const done = parts.reduce((s, p) => s + Number(p.done || 0), 0);
  const total = parts.reduce((s, p) => s + Number(p.total || 0), 0);
  if (!total) return null;
  return { done, total, unit, label: `${done} of ${total} ${unit}` };
}

/** Seconds left from the recent pace (last ~15 min of progress samples); null if not enough data. */
function paceEta(samples: Row[], progress: number): number | null {
  const pts = samples
    .filter((m) => m.progress != null)
    .map((m) => ({ t: new Date(m.ts).getTime() / 1000, p: Number(m.progress) }))
    .sort((a, b) => a.t - b.t);
  if (pts.length < 2) return null;
  const last = pts[pts.length - 1];
  const window = pts.filter((x) => x.t >= last.t - 900);
  const first = window.length >= 2 ? window[0] : pts[0];
  const dp = last.p - first.p;
  const dt = last.t - first.t;
  if (dp <= 0 || dt < 60) return null;
  return Math.max(0, ((1 - Math.max(progress, last.p)) / dp) * dt);
}

async function paceByJob(jobs: Row[], attempts: Map<string, Row[]>) {
  const active = new Map<string, string>(); // attempt id -> job id
  for (const j of jobs) {
    if (j.status !== "running") continue;
    for (const a of attempts.get(j.id) ?? []) if (!a.ended_at) active.set(a.id, j.id);
  }
  const out = new Map<string, number>();
  if (!active.size) return out;
  const since = new Date(Date.now() - 3 * 3600 * 1000).toISOString();
  const rows = ((await dataClient().from("metrics").select("attempt_id,ts,progress").in("attempt_id", [...active.keys()])
    .not("progress", "is", null).gte("ts", since).order("ts", { ascending: false }).limit(1500)).data ?? []) as Row[];
  const byJob = new Map<string, Row[]>();
  for (const r of rows) {
    const j = active.get(r.attempt_id);
    if (j) byJob.set(j, [...(byJob.get(j) ?? []), r]);
  }
  for (const j of jobs) {
    const eta = paceEta(byJob.get(j.id) ?? [], Number(j.progress) || 0);
    if (eta != null) out.set(j.id, eta);
  }
  return out;
}

function providerName(id: string, providers: Map<string, Row>) {
  return providers.get(id)?.name ?? id;
}

function explain(job: Row, attempts: Row[], providers: Map<string, Row>): string {
  const ordered = [...attempts].sort((a, b) => a.started_at.localeCompare(b.started_at));
  const current = ordered.filter((a) => ["submitted", "queued", "running"].includes(a.status));
  const used = [...new Set(ordered.map((a) => a.provider_id))];
  switch (job.status) {
    case "running": {
      const where = [...new Set(current.map((a) => providerName(a.provider_id, providers)))].join(" + ");
      const moved = ordered.filter((a) => a.end_reason && a.end_reason !== "completed");
      const last = moved[moved.length - 1];
      const parts = current.length > 1 ? ` · ${current.length} parts in parallel` : "";
      if (last) return `Running on ${where || "…"} · moved from ${providerName(last.provider_id, providers)} when ${WHY[last.end_reason] ?? "it stopped"}${parts}`;
      return `Running on ${where || "…"}${parts}`;
    }
    case "awaiting_approval":
      return job.estimate?.approval?.reason ?? "Waiting for your OK";
    case "queued":
      return "Waiting for a free GPU — starts as soon as one frees up";
    case "done": {
      const spent = ordered.reduce((s, a) => s + Number(a.cost_usd || 0), 0);
      return `Results ready · ${used.length} provider${used.length === 1 ? "" : "s"}, ${money(spent)} spent`;
    }
    case "paused":
      return "Paused by you — resumes from the last save";
    case "failed":
      return "Stopped after repeated errors — see why";
    default:
      return "";
  }
}

async function load() {
  const db = dataClient();
  const monthStart = new Date();
  monthStart.setUTCDate(1);
  monthStart.setUTCHours(0, 0, 0, 0);
  const [jobs, providers, projects, credits, monthAttempts] = await Promise.all([
    db.from("jobs").select("*").order("created_at", { ascending: false }).limit(40),
    db.from("providers").select("*").order("id"),
    db.from("projects").select("id,slug,name"),
    db.from("credit_snapshots").select("*").order("measured_at", { ascending: false }).limit(200),
    db.from("attempts").select("provider_id,gpu_seconds,cost_usd,market_cost_usd,started_at").gte("started_at", monthStart.toISOString()),
  ]);
  return {
    jobs: (jobs.data ?? []) as Row[],
    providers: new Map(((providers.data ?? []) as Row[]).map((p) => [p.id, p])),
    projects: new Map(((projects.data ?? []) as Row[]).map((p) => [p.id, p])),
    credits: (credits.data ?? []) as Row[],
    monthAttempts: (monthAttempts.data ?? []) as Row[],
  };
}

async function attemptsFor(jobIds: string[]) {
  if (!jobIds.length) return new Map<string, Row[]>();
  const db = dataClient();
  const shards = ((await db.from("shards").select("id,job_id").in("job_id", jobIds)).data ?? []) as Row[];
  const shardJob = new Map(shards.map((s) => [s.id, s.job_id]));
  const attempts = shards.length
    ? (((await db.from("attempts").select("*").in("shard_id", shards.map((s) => s.id))).data ?? []) as Row[])
    : [];
  const byJob = new Map<string, Row[]>();
  for (const a of attempts) {
    const j = shardJob.get(a.shard_id);
    if (!j) continue;
    byJob.set(j, [...(byJob.get(j) ?? []), a]);
  }
  return byJob;
}

export async function overview(): Promise<Overview> {
  const { jobs, providers, projects, credits, monthAttempts } = await load();
  const visible = jobs.filter(
    (j) => projects.get(j.project_id)?.slug !== "tierhopper-smoke" &&
      !(j.status === "cancelled" && secondsSince(j.finished_at) > 86400) &&
      !(["done", "failed"].includes(j.status) && secondsSince(j.finished_at) > 7 * 86400),
  );
  const attempts = await attemptsFor(visible.map((j) => j.id));
  const pace = await paceByJob(visible, attempts);
  const rows: ProjectRow[] = visible.map((j) => {
    const st = STATUS[j.status] ?? { key: "wait", label: j.status };
    const secs = pace.get(j.id) ?? eta(j);
    return {
      id: j.id,
      name: j.name,
      project: projects.get(j.project_id)?.slug ?? "",
      status: j.status,
      statusKey: st.key,
      statusLabel: st.label,
      progress: Number(j.progress) || 0,
      count: counter(j)?.label ?? null,
      line: explain(j, attempts.get(j.id) ?? [], providers),
      when: j.finished_at ?? j.started_at ?? j.created_at,
      startedAt: j.started_at ?? j.created_at,
      finishedAt: j.finished_at ?? null,
      etaSeconds: secs,
      eta: j.status === "done" ? "" : j.status === "queued" ? "in line" : secs == null ? "—" : `${duration(secs)} left`,
      approval: j.status === "awaiting_approval"
        ? { reason: j.estimate?.approval?.reason ?? "", paid: !!j.estimate?.approval?.paid }
        : undefined,
    };
  });
  // Order of execution: the most recently started project first.
  rows.sort((a, b) => b.startedAt.localeCompare(a.startedAt));

  const active = rows.filter((r) => ["running", "need", "wait", "paused"].includes(r.statusKey));
  const progress = active.length ? active.reduce((s, r) => s + r.progress, 0) / active.length : rows.length ? 1 : 0;
  const timeLeft = Math.max(0, ...active.map((r) => r.etaSeconds ?? 0));
  const spentMonth = monthAttempts.reduce((s, a) => s + Number(a.cost_usd || 0), 0);
  const savedMonth = monthAttempts.reduce((s, a) => s + savedOn(a), 0);
  const gpuHoursMonth = monthAttempts.reduce((s, a) => s + Number(a.gpu_seconds || 0), 0) / 3600;
  const paidDays = monthAttempts.filter((a) => Number(a.cost_usd) > 0).map((a) => new Date(a.started_at).getTime());
  const firstUse = Math.min(...monthAttempts.map((a) => new Date(a.started_at).getTime()), now());
  const streakFrom = paidDays.length ? Math.max(...paidDays) : firstUse;
  const streakDays = Math.max(0, Math.floor((now() - streakFrom) / 86400000));

  const latest = new Map<string, Row>();
  for (const c of credits) if (!latest.has(c.provider_id)) latest.set(c.provider_id, c);
  const creditRows = [...providers.values()]
    .filter((p) => p.status === "active")
    .map((p) => {
      const c = latest.get(p.id);
      const total = Number(p.credit_model?.amount) || 0;
      const remaining = c ? Number(c.remaining) : total;
      const hours = c?.unit === "gpu_hours";
      const resets = c?.expires_at ? new Date(c.expires_at) : null;
      const note = p.kind === "paid" ? "Only with your OK"
        : resets ? `Resets ${resets.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" })}` : c?.source === "estimate" ? "Estimated" : "";
      return {
        id: p.id,
        name: p.name,
        left: hours ? `${remaining.toFixed(1)}h / ${total}h` : `${money(remaining)} / ${money(total)}`,
        pct: total ? Math.min(100, (remaining / total) * 100) : 0,
        note,
        color: PROVIDER_COLOR[p.id] ?? "var(--brand)",
        paid: p.kind === "paid",
      };
    });
  const moves: Overview["moves"] = [];
  for (const j of visible) {
    if (j.status !== "running") continue;
    const list = [...(attempts.get(j.id) ?? [])].sort((a, b) => a.started_at.localeCompare(b.started_at));
    for (const a of list.filter((x) => !x.ended_at)) {
      const prev = list.filter((x) => x.shard_id === a.shard_id && x.ended_at && x.started_at < a.started_at).pop();
      const justMoved = prev && prev.provider_id !== a.provider_id && secondsSince(a.started_at) < 600;
      moves.push({ job: j.id, name: j.name, to: a.provider_id, from: justMoved ? prev.provider_id : null });
    }
  }
  const counts: Record<string, number> = {};
  for (const r of rows) counts[r.statusKey] = (counts[r.statusKey] ?? 0) + 1;
  const milestones = [10, 25, 50, 100].map((m) => ({ label: `$${m}`, hit: savedMonth >= m }));
  return {
    progress, timeLeft: active.length ? duration(timeLeft) : "—", spentMonth, savedMonth, streakDays, counts, projects: rows,
    credits: creditRows, milestones, gpuHoursMonth, moves,
  };
}

export type JobDetail = {
  id: string;
  name: string;
  status: string;
  statusKey: string;
  statusLabel: string;
  line: string;
  runningFor: string;
  progress: number;
  count: string | null;
  pace: string | null;
  timeLeft: string;
  providersUsed: number;
  saved: number;
  startedAt: string | null;
  finishAt: string | null;
  journey: { provider: string; name: string; color: string; seconds: number; current: boolean }[];
  remainingSeconds: number;
  stops: { provider: string; name: string; color: string; share: number; seconds: number; why: string }[];
  events: { ts: string; type: string; text: string }[];
  technical: { provider: string; gpu: string; status: string; reason: string | null; message: string | null; link: string | null }[];
  gpu: { util: number | null; vramUsed: number | null; vramTotal: number | null; ts: string } | null;
  gpuHistory: number[];
  logTail: string[];
  approval?: { reason: string; paid: boolean };
};

const EVENT_TEXT: Record<string, (p: Row) => string> = {
  submitted: () => "Sent to TierHopper",
  attempt_started: (p) => `Started on ${p.provider}${p.resume ? " (resuming from the last save)" : ""}`,
  attempt_running: (p) => (p.resumed ? "Running — picked up from the last save" : "Running"),
  checkpoint: (p) => `Progress saved (${Math.round((p.progress ?? 0) * 100)}%)`,
  runner_exit: (p) => `Stopped here: ${WHY[p.reason] ?? p.reason}`,
  hop: (p) => `Moving on from ${p.from_provider}: ${WHY[p.reason] ?? p.reason}`,
  waiting: () => "Waiting for a free GPU",
  approval_requested: (p) => `Asked for your OK: ${p.reason}`,
  approved: (p) => `Approved (${p.channel})`,
  denied: () => "You declined",
  paused: () => "Paused",
  resumed: () => "Resumed",
  done: () => "Done — results ready",
  failed: (p) => `Stopped: ${p.reason}`,
  submit_failed: (p) => `Could not start on ${p.provider}`,
  spend_cap: () => "Paid option skipped: monthly cap reached",
};

export async function jobDetail(id: string): Promise<JobDetail | null> {
  const db = dataClient();
  const job = (await db.from("jobs").select("*").eq("id", id).maybeSingle()).data as Row | null;
  if (!job) return null;
  const providers = new Map((((await db.from("providers").select("id,name")).data ?? []) as Row[]).map((p) => [p.id, p]));
  const attempts = ((await attemptsFor([id])).get(id) ?? []).sort((a, b) => a.started_at.localeCompare(b.started_at));
  const events = ((await db.from("events").select("ts,type,payload").eq("job_id", id).order("ts").limit(300)).data ?? []) as Row[];
  const st = STATUS[job.status] ?? { key: "wait", label: job.status };
  const latestAttempt = attempts[attempts.length - 1];
  const metrics = latestAttempt
    ? (((await db.from("metrics").select("ts,gpu_util,vram_used_mb,vram_total_mb,progress").eq("attempt_id", latestAttempt.id)
        .order("ts", { ascending: false }).limit(120)).data ?? []) as Row[])
    : [];
  const paced = job.status === "running" ? paceEta(metrics, Number(job.progress) || 0) : null;
  const secs = paced ?? eta(job);
  const cnt = counter(job);
  // Pace in the job's own unit, e.g. "about 2.4 pages per minute".
  let paceText: string | null = null;
  if (cnt && paced != null && paced > 0 && cnt.total > cnt.done) {
    const perMinute = (cnt.total - cnt.done) / (paced / 60);
    paceText = perMinute >= 1 ? `about ${perMinute.toFixed(1)} ${cnt.unit} per minute`
      : `about ${(1 / perMinute).toFixed(1)} minutes per ${cnt.unit.replace(/s$/, "")}`;
  }
  const logs = latestAttempt
    ? (((await db.from("log_tail").select("line").eq("attempt_id", latestAttempt.id).order("id", { ascending: false })
        .limit(80)).data ?? []) as Row[]).reverse().map((r) => r.line as string)
    : [];
  const attemptSeconds = (a: Row) => {
    const end = a.ended_at ? new Date(a.ended_at).getTime() : now();
    return Math.max(0, (end - new Date(a.started_at).getTime()) / 1000);
  };
  const journey = attempts.map((a) => ({
    provider: a.provider_id,
    name: providerName(a.provider_id, providers),
    color: PROVIDER_COLOR[a.provider_id] ?? "var(--brand)",
    seconds: attemptSeconds(a),
    current: !a.ended_at,
  }));
  const byProvider = new Map<string, { seconds: number; share: number; whys: string[] }>();
  for (const a of attempts) {
    const cur = byProvider.get(a.provider_id) ?? { seconds: 0, share: 0, whys: [] };
    cur.seconds += attemptSeconds(a);
    cur.share += Math.max(0, Number(a.progress_end ?? (a.ended_at ? a.progress_start : job.progress)) - Number(a.progress_start || 0));
    cur.whys.push(!a.ended_at ? "here now" : a.end_reason === "completed" ? "finished here" : `stopped because ${WHY[a.end_reason] ?? "of an error"}`);
    byProvider.set(a.provider_id, cur);
  }
  const story = (whys: string[]) => {
    const text = whys.join(", then ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  };
  const started = job.started_at ?? attempts[0]?.started_at ?? null;
  return {
    id: job.id,
    name: job.name,
    status: job.status,
    statusKey: st.key,
    statusLabel: st.label,
    line: "",
    runningFor: duration(started ? ((job.finished_at ? new Date(job.finished_at).getTime() : now()) - new Date(started).getTime()) / 1000 : null),
    progress: Number(job.progress) || 0,
    count: cnt?.label ?? null,
    pace: paceText,
    timeLeft: job.status === "done" ? "0m" : duration(secs),
    providersUsed: new Set(attempts.map((a) => a.provider_id)).size,
    saved: attempts.reduce((s, a) => s + savedOn(a), 0),
    startedAt: started,
    finishAt: job.status === "done" ? job.finished_at : secs != null ? new Date(now() + secs * 1000).toISOString() : null,
    journey,
    remainingSeconds: job.status === "done" ? 0 : secs ?? 0,
    stops: [...byProvider.entries()].map(([p, v]) => ({
      provider: p, name: providerName(p, providers), color: PROVIDER_COLOR[p] ?? "var(--brand)",
      share: Math.min(1, v.share), seconds: v.seconds, why: story(v.whys),
    })),
    events: events.map((e) => ({ ts: e.ts, type: e.type, text: (EVENT_TEXT[e.type] ?? (() => e.type))(e.payload ?? {}) })),
    technical: attempts.map((a) => ({
      provider: a.provider_id, gpu: a.gpu_type, status: a.status, reason: a.end_reason, message: a.message,
      link: a.external_ref?.url ?? null,
    })),
    approval: job.status === "awaiting_approval"
      ? { reason: job.estimate?.approval?.reason ?? "", paid: !!job.estimate?.approval?.paid } : undefined,
    gpu: metrics[0]
      ? { util: metrics[0].gpu_util, vramUsed: metrics[0].vram_used_mb, vramTotal: metrics[0].vram_total_mb, ts: metrics[0].ts }
      : null,
    gpuHistory: metrics.map((m) => Number(m.gpu_util ?? 0)).reverse(),
    logTail: logs,
  };
}

export async function providerCards() {
  const db = dataClient();
  const rows = ((await db.from("providers").select("*").order("id")).data ?? []) as Row[];
  return rows.map((p) => ({
    id: p.id,
    name: p.name,
    kind: p.kind,
    status: p.status,
    signupUrl: p.signup_url,
    requirements: p.requirements ?? {},
    creditModel: p.credit_model ?? {},
    gpus: ((p.gpu_catalog ?? []) as Row[]).map((g) => g.type),
    color: PROVIDER_COLOR[p.id] ?? "var(--brand)",
  }));
}

export type Report = {
  days: number;
  totals: { gpuHours: number; saved: number; spent: number; done: number; stopped: number; switches: number };
  providers: { id: string; name: string; color: string; gpuHours: number; saved: number; spent: number; runs: number; errors: number; switches: number }[];
  daily: { day: string; label: string; total: number; parts: { id: string; color: string; hours: number }[] }[];
};

export async function report(days = 30): Promise<Report> {
  const db = dataClient();
  const since = new Date(Date.now() - days * 86400000);
  const [attemptsRes, providersRes, jobsRes] = await Promise.all([
    db.from("attempts").select("provider_id,gpu_seconds,cost_usd,market_cost_usd,started_at,end_reason").gte("started_at", since.toISOString()).limit(5000),
    db.from("providers").select("id,name"),
    db.from("jobs").select("status,created_at,name").gte("created_at", since.toISOString()).limit(2000),
  ]);
  const attempts = (attemptsRes.data ?? []) as Row[];
  const names = new Map(((providersRes.data ?? []) as Row[]).map((p) => [p.id, p.name as string]));
  const jobs = ((jobsRes.data ?? []) as Row[]).filter((j) => !String(j.name).startsWith("smoke-"));
  const moved = new Set(["session_limit", "preempted", "credit_exhausted", "error"]);
  const per = new Map<string, Report["providers"][number]>();
  const byDay = new Map<string, Map<string, number>>();
  for (const a of attempts) {
    const id = a.provider_id as string;
    const row = per.get(id) ?? { id, name: names.get(id) ?? id, color: PROVIDER_COLOR[id] ?? "var(--brand)", gpuHours: 0, saved: 0, spent: 0, runs: 0, errors: 0, switches: 0 };
    const hours = Number(a.gpu_seconds || 0) / 3600;
    row.gpuHours += hours;
    row.spent += Number(a.cost_usd || 0);
    row.saved += savedOn(a);
    row.runs += 1;
    row.errors += a.end_reason === "error" ? 1 : 0;
    row.switches += moved.has(a.end_reason) ? 1 : 0;
    per.set(id, row);
    const day = String(a.started_at).slice(0, 10);
    const d = byDay.get(day) ?? new Map<string, number>();
    d.set(id, (d.get(id) ?? 0) + hours);
    byDay.set(day, d);
  }
  const shown = Math.min(days, 14);
  const daily = Array.from({ length: shown }, (_, i) => {
    const date = new Date(Date.now() - (shown - 1 - i) * 86400000);
    const day = date.toISOString().slice(0, 10);
    const parts = [...(byDay.get(day) ?? new Map<string, number>()).entries()].map(([id, hours]) => ({ id, color: PROVIDER_COLOR[id] ?? "var(--brand)", hours }));
    return { day, label: date.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" }), total: parts.reduce((s, p) => s + p.hours, 0), parts };
  });
  const providers = [...per.values()].sort((a, b) => b.gpuHours - a.gpuHours);
  return {
    days,
    totals: {
      gpuHours: providers.reduce((s, p) => s + p.gpuHours, 0),
      saved: providers.reduce((s, p) => s + p.saved, 0),
      spent: providers.reduce((s, p) => s + p.spent, 0),
      done: jobs.filter((j) => j.status === "done").length,
      stopped: jobs.filter((j) => j.status === "failed").length,
      switches: providers.reduce((s, p) => s + p.switches, 0),
    },
    providers,
    daily,
  };
}

export async function suggestions() {
  const db = dataClient();
  const res = await db.from("discovery_findings").select("name,url,summary,confidence,status,reason,run_at").order("run_at", { ascending: false }).limit(30);
  if (res.error) return []; // table not created yet
  const seen = new Set<string>();
  return ((res.data ?? []) as Row[]).filter((f) => (seen.has(f.name) ? false : (seen.add(f.name), true)));
}
