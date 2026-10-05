import Link from "next/link";
import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { report } from "@/lib/data";
import { money } from "@/lib/labels";
import { currentUser } from "@/lib/supabase";

export const dynamic = "force-dynamic";

export default async function Reports({ searchParams }: { searchParams: Promise<{ days?: string }> }) {
  if (!(await currentUser())) redirect("/login");
  const days = (await searchParams).days === "7" ? 7 : 30;
  const r = await report(days);
  const peak = Math.max(...r.daily.map((d) => d.total), 0.01);
  return (
    <Shell>
      <div className="dtitle">
        <h1>Reports</h1>
        <div className="themes" role="group" aria-label="Period">
          <Link href="/reports?days=7" className={days === 7 ? "on" : ""}>7 days</Link>
          <Link href="/reports?days=30" className={days === 30 ? "on" : ""}>30 days</Link>
        </div>
      </div>

      <div className="tiles">
        <div className="card tile"><div className="h">GPU time</div><b>{r.totals.gpuHours.toFixed(1)} h</b></div>
        <div className="card tile save"><div className="h">Saved</div><b>{money(r.totals.saved)}</b></div>
        <div className="card tile"><div className="h">Spent</div><b>{money(r.totals.spent)}</b></div>
        <div className="card tile"><div className="h">Projects</div><b>{r.totals.done} done · {r.totals.stopped} stopped</b></div>
      </div>

      <section className="card section">
        <div className="h">GPU hours per day</div>
        <div className="chart" role="img" aria-label={`GPU hours per day for the last ${r.daily.length} days`}>
          {r.daily.map((d) => (
            <div className="chart-col" key={d.day} title={`${d.label}: ${d.total.toFixed(2)} h`}>
              <span className="chart-val mono">{d.total >= 0.05 ? d.total.toFixed(1) : ""}</span>
              <div className="chart-stack">
                {d.parts.map((p) => <div key={p.id} style={{ height: `${(p.hours / peak) * 100}%`, background: p.color }} />)}
              </div>
              <span className="chart-label mono">{d.label.split(" ")[1]}</span>
            </div>
          ))}
        </div>
        <div className="legend">
          {r.providers.map((p) => <span key={p.id}><i style={{ background: p.color }} />{p.name}</span>)}
          {!r.providers.length && <span>No GPU time in this period yet.</span>}
        </div>
      </section>

      <section className="card section">
        <div className="h" style={{ marginBottom: 6 }}>By provider</div>
        <div className="table">
          <div className="trow thead"><span>Provider</span><span>GPU time</span><span>Saved</span><span>Spent</span><span>Runs</span><span>Errors</span><span>Switches</span></div>
          {r.providers.map((p) => (
            <div className="trow" key={p.id}>
              <span className="pname"><i style={{ background: p.color }} />{p.name}</span>
              <span className="mono">{p.gpuHours.toFixed(2)} h</span>
              <span className="mono" style={{ color: "var(--done)" }}>{money(p.saved)}</span>
              <span className="mono">{money(p.spent)}</span>
              <span className="mono">{p.runs}</span>
              <span className="mono">{p.errors}</span>
              <span className="mono">{p.switches}</span>
            </div>
          ))}
        </div>
        <p className="muted" style={{ marginTop: 12, fontSize: 12.5 }}>
          “Saved” compares free GPU time with on-demand market prices. “Switches” counts the times a project moved to another provider.
        </p>
      </section>
    </Shell>
  );
}
