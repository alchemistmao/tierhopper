import { NextResponse } from "next/server";
import { overview } from "@/lib/data";
import { currentUser } from "@/lib/supabase";

export const dynamic = "force-dynamic";

export async function GET() {
  if (!(await currentUser())) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  return NextResponse.json(await overview(), { headers: { "Cache-Control": "no-store" } });
}
