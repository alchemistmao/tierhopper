import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { providerCards, suggestions } from "@/lib/data";
import { currentUser } from "@/lib/supabase";

export const dynamic = "force-dynamic";

function gift(cm: Record<string, unknown>) {
  const amount = Number(cm.amount ?? 0);
  switch (cm.type) {
    case "monthly_usd": return `$${amount}/month free`;
    case "weekly_hours": return `~${amount} GPU-hours/week free`;
    case "one_time_usd": return `$${amount} free credit`;
    default: return "Pay as you go — only with your OK";
  }
}

export default async function Providers() {
  if (!(await currentUser())) redirect("/login");
  const [cards, found] = await Promise.all([providerCards(), suggestions()]);
  return (
    <Shell>
      <div className="h" style={{ marginBottom: 14 }}>GPU sources</div>
      <div className="cards">
        {cards.map((p) => {
          const active = p.status === "active";
          const steps = [
            { label: "Account created (by you)", ok: active },
            ...(p.requirements.phone ? [{ label: "Phone verified (by you)", ok: active }] : []),
            { label: "Key saved in your Keychain", ok: active },
            { label: "30-second GPU test passed", ok: active },
          ];
          return (
            <section key={p.id} className={`card pcard ${active ? "" : "locked"}`} style={{ borderColor: active ? p.color : undefined }}>
              <div className="dtitle" style={{ margin: 0 }}>
                <b style={{ fontSize: 16 }}>{p.name}</b>
                <span className={`pill ${active ? "s-done" : "s-wait"}`}>{active ? "Connected" : p.status === "pending_adapter" ? "Coming soon" : "Locked"}</span>
              </div>
              <div className="muted">{gift(p.creditModel)} · {p.gpus.join(", ")}</div>
              {steps.map((s) => (
                <div key={s.label} className={`check ${s.ok ? "ok" : ""}`}>{s.ok ? "✓" : "○"} {s.label}</div>
              ))}
              {!active && (
                <>
                  {p.signupUrl && <a className="btn" href={p.signupUrl} target="_blank" rel="noopener noreferrer">Open sign-up page ↗</a>}
                  <code className="cmd">tierhopper connect {p.id}</code>
                </>
              )}
            </section>
          );
        })}
      </div>

      <div className="h" style={{ margin: "28px 0 14px" }}>Found by weekly discovery</div>
      {!found.length && <p className="muted">Nothing yet. TierHopper looks for new free GPU tiers every Monday.</p>}
      <div className="cards">
        {found.map((f) => (
          <section key={f.name} className="card pcard locked">
            <div className="dtitle" style={{ margin: 0 }}>
              <b style={{ fontSize: 16 }}>{f.name}</b>
              <span className={`pill ${f.status === "qualified" ? "s-done" : f.status === "rejected" ? "s-stopped" : "s-need"}`}>
                {f.status === "qualified" ? "Looks good" : f.status === "rejected" ? "Not usable" : "Needs a look"}
              </span>
            </div>
            <div className="muted">{f.summary}</div>
            <div className="check">{Math.round(Number(f.confidence) * 100)}% confidence · {f.reason}</div>
            <a className="btn" href={f.url} target="_blank" rel="noopener noreferrer">Open page ↗</a>
          </section>
        ))}
      </div>
    </Shell>
  );
}
