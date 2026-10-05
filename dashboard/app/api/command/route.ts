import { NextResponse } from "next/server";
import { currentUser } from "@/lib/supabase";

/** Natural-language palette: asks the control plane (Claude) for a proposed action. Never acts. */
export async function POST(request: Request) {
  if (!(await currentUser())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const { text } = (await request.json().catch(() => ({}))) as { text?: string };
  if (!text?.trim()) return NextResponse.json({ kind: "answer", text: "Type what you want to do." });
  const base = process.env.CONTROL_API_URL;
  if (!base) return NextResponse.json({ kind: "answer", text: "The control plane is not deployed yet." });
  const res = await fetch(`${base}/api/command`, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.CONTROL_API_SECRET}`, "Content-Type": "application/json" },
    body: JSON.stringify({ text: text.slice(0, 500) }),
    cache: "no-store",
  });
  if (!res.ok) return NextResponse.json({ kind: "answer", text: "Could not reach TierHopper. Try again." });
  return NextResponse.json(await res.json());
}
