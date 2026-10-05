"use client";
import { Shell } from "@/components/Shell";
import { usePoll } from "@/components/usePoll";
import type { Overview } from "@/lib/data";

const POS = [
  { x: 170, y: 120 }, { x: 690, y: 120 }, { x: 450, y: 300 }, { x: 820, y: 310 }, { x: 120, y: 300 },
];

/** Providers as nodes. A dot sits on the provider where a project is running; it travels along a
 *  line only for a few minutes after the project really switched providers. */
export default function MapPage() {
  const { data, live } = usePoll<Overview>("/api/overview", 8000);
  const nodes = (data?.credits ?? []).map((c, i) => ({ ...c, ...POS[i % POS.length] }));
  const at = new Map(nodes.map((n) => [n.id, n]));
  const moves = data?.moves ?? [];
  const edges = nodes.flatMap((a, i) => nodes.slice(i + 1).map((b) => ({ a, b })));
  const names = [...new Set(moves.map((m) => at.get(m.to)?.name ?? m.to))];
  const parked = new Map<string, number>();
  return (
    <Shell live={live}>
      <section className="card section map">
        <div className="h">Hop map · live</div>
        <p className="muted" style={{ margin: "8px 0 0" }}>
          {moves.length
            ? `${moves.length} project${moves.length === 1 ? "" : "s"} running on ${names.join(" and ")}. A dot travels a line only when a project switches providers.`
            : "Nothing running right now."}
        </p>
        <svg viewBox="0 0 900 380" role="img" aria-label="Providers as nodes with the projects running on each">
          {edges.map(({ a, b }, i) => (
            <path key={i} d={`M${a.x} ${a.y} Q ${(a.x + b.x) / 2} ${Math.min(a.y, b.y) - 70}, ${b.x} ${b.y}`}
              fill="none" stroke="var(--line)" strokeWidth="1.2" strokeDasharray="4 6" />
          ))}
          {nodes.map((n) => (
            <g key={n.id} opacity={n.paid ? 0.55 : 1}>
              <circle cx={n.x} cy={n.y} r={n.paid ? 32 : 44} fill="var(--panel-2)" stroke={n.color} strokeWidth="2" strokeDasharray={n.paid ? "3 4" : undefined} />
              <text x={n.x} y={n.y - 2} textAnchor="middle" fill="var(--text)" style={{ font: "500 14px var(--font)" }}>{n.name}</text>
              <text x={n.x} y={n.y + 16} textAnchor="middle" fill="var(--muted)" style={{ font: "12px var(--mono)" }}>{n.left.split(" / ")[0]} left</text>
            </g>
          ))}
          {moves.map((m, i) => {
            const to = at.get(m.to);
            const from = m.from ? at.get(m.from) : null;
            if (!to) return null;
            if (from) {
              const d = `M${from.x} ${from.y} Q ${(from.x + to.x) / 2} ${Math.min(from.y, to.y) - 70}, ${to.x} ${to.y}`;
              return (
                <g key={i}>
                  <path d={d} fill="none" stroke="var(--run)" strokeWidth="1.5" strokeDasharray="4 6" />
                  <circle r="6" fill="var(--run)"><animateMotion dur="2.5s" repeatCount="indefinite" path={d} /><title>{m.name}: moved here</title></circle>
                </g>
              );
            }
            const k = parked.get(m.to) ?? 0;
            parked.set(m.to, k + 1);
            const angle = -Math.PI / 2 + k * 0.7; // projects on the same provider sit side by side on its ring
            const r = to.paid ? 32 : 44;
            return (
              <circle key={i} cx={to.x + Math.cos(angle) * r} cy={to.y + Math.sin(angle) * r} r="7" fill="var(--run)" stroke="var(--bg)" strokeWidth="2">
                <animate attributeName="opacity" values="1;.45;1" dur="1.6s" repeatCount="indefinite" />
                <title>{m.name}</title>
              </circle>
            );
          })}
        </svg>
      </section>
    </Shell>
  );
}
