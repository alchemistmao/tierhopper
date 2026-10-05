"use client";
import { useState } from "react";

export default function Login() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "sent" | "error">("idle");
  const send = async (e: React.FormEvent) => {
    e.preventDefault();
    const res = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    setState(res.ok ? "sent" : "error");
  };
  return (
    <main>
      <form className="card login" onSubmit={send}>
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/frog.svg" alt="A frog hopping from a paid chip to a free one" style={{ width: "100%", borderRadius: 10, marginBottom: 16 }} />
        <div className="h">TierHopper · hop till the credits drop</div>
        <h1 style={{ fontSize: 22, margin: "8px 0 4px" }}>Sign in</h1>
        <p className="muted">We send a one-time link to your e-mail.</p>
        <input type="email" required autoComplete="email" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} />
        <button className="btn brand" type="submit">Send link</button>
        {state === "sent" && <p className="muted" style={{ marginTop: 12 }}>Check your inbox.</p>}
        {state === "error" && <p style={{ marginTop: 12, color: "var(--stop)" }}>Could not send the link.</p>}
        {state === "idle" && typeof window !== "undefined" && window.location.search.includes("error=link") && (
          <p style={{ marginTop: 12, color: "var(--stop)" }}>That link expired or was opened in another browser. Send a new one and open it here.</p>
        )}
      </form>
    </main>
  );
}
