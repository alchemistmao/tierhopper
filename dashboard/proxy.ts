import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

/** Refreshes the Supabase session and keeps everyone but the allowed account on /login. */
export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });
  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      cookies: {
        getAll: () => request.cookies.getAll(),
        setAll: (list) => {
          list.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          list.forEach(({ name, value, options }) => response.cookies.set(name, value, options));
        },
      },
    },
  );
  const { data } = await supabase.auth.getUser();
  const email = data.user?.email?.toLowerCase();
  const devBypass = process.env.NODE_ENV === "development" && process.env.DEV_AUTH_BYPASS === "1";
  const allowed = devBypass || (!!email && email === (process.env.ALLOWED_EMAIL ?? "").toLowerCase());
  const path = request.nextUrl.pathname;
  // A sign-in link that lands on any page with ?code= finishes at the callback.
  const code = request.nextUrl.searchParams.get("code");
  if (code && !path.startsWith("/auth/")) {
    const target = request.nextUrl.clone();
    target.pathname = "/auth/callback";
    return NextResponse.redirect(target);
  }
  const open = path.startsWith("/login") || path.startsWith("/auth/");
  if (!allowed && !open) {
    if (path.startsWith("/api/")) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return response;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico|manifest.webmanifest|icon.svg|frog.svg|sw.js).*)"],
};
