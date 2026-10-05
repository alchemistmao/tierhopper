import { NextResponse } from "next/server";
import { siteUrl } from "@/lib/site";
import { authClient } from "@/lib/supabase";

/** Sends the magic link only to the allowed address; answers the same way for any other. */
export async function POST(request: Request) {
  const { email } = (await request.json().catch(() => ({}))) as { email?: string };
  const allowed = (process.env.ALLOWED_EMAIL ?? "").toLowerCase();
  if (email && email.toLowerCase() === allowed) {
    const supabase = await authClient();
    const origin = siteUrl(request);
    await supabase.auth.signInWithOtp({
      email: allowed,
      options: { emailRedirectTo: `${origin}/auth/callback`, shouldCreateUser: true },
    });
  }
  return NextResponse.json({ ok: true });
}
