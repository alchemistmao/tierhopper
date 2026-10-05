"use client";
import { createBrowserClient } from "@supabase/ssr";
import { useEffect, useRef } from "react";

const TABLES = ["jobs", "attempts", "events", "metrics"] as const;

/** Calls `onChange` (debounced) whenever TierHopper state changes in Supabase. Returns nothing; best effort. */
export function useRealtime(onChange: () => void, enabled = true) {
  const cb = useRef(onChange);
  cb.current = onChange;
  useEffect(() => {
    if (!enabled) return;
    const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
    const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
    if (!url || !key) return;
    const supabase = createBrowserClient(url, key);
    let timer: ReturnType<typeof setTimeout> | null = null;
    const fire = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => cb.current(), 400);
    };
    const channel = supabase.channel("tierhopper-live");
    for (const table of TABLES) {
      channel.on("postgres_changes", { event: "*", schema: "public", table }, fire);
    }
    channel.subscribe();
    return () => {
      if (timer) clearTimeout(timer);
      supabase.removeChannel(channel);
    };
  }, [enabled]);
}
