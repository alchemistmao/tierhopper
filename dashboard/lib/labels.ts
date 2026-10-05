// Plain-language labels shared by server and client. No jargon on the main screens.

export type StatusKey = "running" | "done" | "need" | "wait" | "paused" | "stopped";

export const STATUS: Record<string, { key: StatusKey; label: string }> = {
  running: { key: "running", label: "Running" },
  done: { key: "done", label: "Done" },
  awaiting_approval: { key: "need", label: "Needs your OK" },
  queued: { key: "wait", label: "Waiting in line" },
  paused: { key: "paused", label: "Paused" },
  needs_attention: { key: "need", label: "Needs your OK" },
  failed: { key: "stopped", label: "Stopped" },
  cancelled: { key: "stopped", label: "Cancelled" },
};

export const WHY: Record<string, string> = {
  credit_exhausted: "the free credit ran out",
  session_limit: "it reached the session time limit",
  preempted: "the machine was taken back",
  error: "it hit an error",
  cancelled: "you paused it",
  completed: "finished here",
};

export const PROVIDER_COLOR: Record<string, string> = {
  modal: "var(--p-modal)",
  kaggle: "var(--p-kaggle)",
  lightning: "var(--p-lightning)",
  runpod: "var(--p-runpod)",
};

export function duration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

export function money(usd: number | null | undefined): string {
  return `$${(usd ?? 0).toFixed(2)}`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
