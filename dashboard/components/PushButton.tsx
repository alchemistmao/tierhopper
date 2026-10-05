"use client";
import { useEffect, useState } from "react";

const KEY = process.env.NEXT_PUBLIC_VAPID_PUBLIC_KEY ?? "";

function keyBytes(base64url: string) {
  const pad = "=".repeat((4 - (base64url.length % 4)) % 4);
  const raw = atob((base64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

/** Bell: turns on push notifications for "Needs your OK", "Done" and "Stopped". */
export function PushButton() {
  const [state, setState] = useState<"unsupported" | "off" | "on" | "blocked">("unsupported");

  useEffect(() => {
    if (!KEY || !("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return;
    if (Notification.permission === "denied") return setState("blocked");
    navigator.serviceWorker.getRegistration().then(async (reg) => {
      const sub = await reg?.pushManager.getSubscription();
      setState(sub ? "on" : "off");
    });
  }, []);

  const enable = async () => {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return setState(permission === "denied" ? "blocked" : "off");
    const reg = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    const sub =
      (await reg.pushManager.getSubscription()) ??
      (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(KEY) }));
    const res = await fetch("/api/push/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub.toJSON()),
    });
    setState(res.ok ? "on" : "off");
  };

  if (state === "unsupported") return null;
  const title = { off: "Turn on notifications", on: "Notifications on", blocked: "Notifications blocked in the browser" }[state];
  return (
    <button className="iconbtn" onClick={state === "off" ? enable : undefined} title={title} aria-label={title}
      style={state === "on" ? { color: "var(--done)", borderColor: "var(--done)" } : undefined} disabled={state === "blocked"}>
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
        <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0" />
      </svg>
    </button>
  );
}
