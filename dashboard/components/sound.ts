"use client";

let ctx: AudioContext | null = null;

export function soundsOn(): boolean {
  try {
    return localStorage.getItem("th-sounds") === "on";
  } catch {
    return false;
  }
}

/** Soft tones: tick (navigation), chime (done), low (needs you). Silent unless enabled. */
export function play(kind: "tick" | "chime" | "low" | "boing" = "tick") {
  if (!soundsOn()) return;
  try {
    ctx = ctx ?? new AudioContext();
    if (kind === "boing") {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      const t = ctx.currentTime;
      o.type = "sine";
      o.frequency.setValueAtTime(180, t);
      o.frequency.exponentialRampToValueAtTime(520, t + 0.12);
      o.frequency.exponentialRampToValueAtTime(240, t + 0.3);
      g.gain.setValueAtTime(0.05, t);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.32);
      o.connect(g).connect(ctx.destination);
      o.start(t);
      o.stop(t + 0.32);
      return;
    }
    const tones = { tick: [720], chime: [660, 990], low: [220] }[kind as "tick" | "chime" | "low"];
    tones.forEach((f, i) => {
      const o = ctx!.createOscillator();
      const g = ctx!.createGain();
      const t = ctx!.currentTime + i * 0.09;
      o.frequency.value = f;
      g.gain.setValueAtTime(0.04, t);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
      o.connect(g).connect(ctx!.destination);
      o.start(t);
      o.stop(t + 0.12);
    });
  } catch {
    // audio unavailable
  }
}
