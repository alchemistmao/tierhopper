import "server-only";
import { createServerClient } from "@supabase/ssr";
import { createClient } from "@supabase/supabase-js";
import { cookies } from "next/headers";

const URL = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const PUBLISHABLE = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!;

/** Auth-only client bound to the request cookies (who is logged in). */
export async function authClient() {
  const store = await cookies();
  return createServerClient(URL, PUBLISHABLE, {
    cookies: {
      getAll: () => store.getAll(),
      setAll: (list) => {
        try {
          list.forEach(({ name, value, options }) => store.set(name, value, options));
        } catch {
          // called from a Server Component: the proxy refreshes the session instead
        }
      },
    },
  });
}

/** Data client with the secret key. Server only; never sent to the browser. */
export function dataClient() {
  return createClient(URL, process.env.SUPABASE_SECRET_KEY!, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

/** Local development only: lets the dev server render without a login (never in production builds). */
export const devBypass = () => process.env.NODE_ENV === "development" && process.env.DEV_AUTH_BYPASS === "1";

export async function currentUser() {
  if (devBypass()) return { email: process.env.ALLOWED_EMAIL } as { email?: string };
  const supabase = await authClient();
  const { data } = await supabase.auth.getUser();
  const email = data.user?.email?.toLowerCase();
  if (!email || email !== (process.env.ALLOWED_EMAIL ?? "").toLowerCase()) return null;
  return data.user;
}
