"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRealtime } from "./useRealtime";

/** Loads a JSON endpoint on every Supabase Realtime change, with slow polling as a fallback. */
export function usePoll<T>(url: string, everyMs = 30000) {
  const [data, setData] = useState<T | null>(null);
  const [live, setLive] = useState(true);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (res.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      setData(await res.json());
      setLive(true);
    } catch {
      setLive(false);
    }
  }, [url]);

  useEffect(() => {
    let stopped = false;
    let first = true;
    const tick = async () => {
      if (first || !document.hidden) await load();  // always load once, even in a background tab
      first = false;
      if (!stopped) timer.current = setTimeout(tick, everyMs);
    };
    tick();
    return () => {
      stopped = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load, everyMs]);

  useRealtime(load);
  return { data, live, reload: load };
}

export async function jobAction(id: string, action: string, args: Record<string, unknown> = {}) {
  const res = await fetch(`/api/jobs/${id}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  const body = await res.json().catch(() => ({}));
  return { ok: res.ok && !body.error, body };
}
