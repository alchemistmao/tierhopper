import { NextResponse } from "next/server";
import { currentUser } from "@/lib/supabase";

const ACTIONS = new Set(["approve", "deny", "pause", "resume", "results", "rerun"]);

/** Forwards a user action to the control plane, which owns all job state changes. */
export async function POST(request: Request, { params }: { params: Promise<{ id: string; action: string }> }) {
  if (!(await currentUser())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  const { id, action } = await params;
  if (!ACTIONS.has(action) || !/^[0-9a-f-]{36}$/.test(id)) {
    return NextResponse.json({ error: "bad request" }, { status: 400 });
  }
  const base = process.env.CONTROL_API_URL;
  if (!base) return NextResponse.json({ error: "control plane not deployed yet" }, { status: 503 });
  const args = await request.json().catch(() => ({}));
  const res = await fetch(`${base}/api/jobs/${id}/${action}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${process.env.CONTROL_API_SECRET}`, "Content-Type": "application/json" },
    body: JSON.stringify(args ?? {}),
    cache: "no-store",
  });
  return NextResponse.json(await res.json(), { status: res.status });
}
