import { NextResponse } from "next/server";
import { siteUrl } from "@/lib/site";
import { authClient } from "@/lib/supabase";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const origin = siteUrl(request);
  if (code) {
    const supabase = await authClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (error) return NextResponse.redirect(`${origin}/login?error=link`);
  }
  return NextResponse.redirect(`${origin}/`);
}
