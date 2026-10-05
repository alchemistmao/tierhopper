import { NextResponse } from "next/server";
import { currentUser, dataClient } from "@/lib/supabase";

/** Stores (or refreshes) this browser's push subscription for the dashboard owner. */
export async function POST(request: Request) {
  if (!(await currentUser())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const sub = (await request.json().catch(() => null)) as { endpoint?: string; keys?: { p256dh?: string; auth?: string } } | null;
  if (!sub?.endpoint?.startsWith("https://") || !sub.keys?.p256dh || !sub.keys?.auth) {
    return NextResponse.json({ error: "bad subscription" }, { status: 400 });
  }
  const { error } = await dataClient()
    .from("push_subscriptions")
    .upsert({ endpoint: sub.endpoint, keys: { p256dh: sub.keys.p256dh, auth: sub.keys.auth } }, { onConflict: "endpoint" });
  if (error) return NextResponse.json({ error: "could not save" }, { status: 500 });
  return NextResponse.json({ ok: true });
}
