"use client";
import Link from "next/link";
import { play } from "./sound";

/** Frog logo. On hover two chips appear and the frog hops between them. */
export function Logo() {
  return (
    <Link href="/" className="logo" onMouseEnter={() => play("boing")} aria-label="TierHopper home">
      <svg className="logo-mark" width="64" height="38" viewBox="0 0 116 68" aria-hidden>
        <defs>
          <linearGradient id="frog-skin" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#5ff0b0" />
            <stop offset="1" stopColor="#22b37a" />
          </linearGradient>
        </defs>
        <g className="logo-chips">
          <g className="chip chip-paid" transform="translate(22 58)">
            <rect x="-17" y="-6" width="34" height="12" rx="3" fill="#151b25" stroke="#3a4352" strokeWidth="2.5" />
            <path d="M-10 6v4M0 6v4M10 6v4" stroke="#3a4352" strokeWidth="2.5" strokeLinecap="round" />
            <text x="0" y="3.5" textAnchor="middle" fontSize="9" fontWeight="700" fill="#5b6576" fontFamily="var(--mono)">$$</text>
          </g>
          <g className="chip chip-free" transform="translate(94 58)">
            <rect x="-17" y="-6" width="34" height="12" rx="3" fill="#0b2a26" stroke="#2dd4bf" strokeWidth="2.5" />
            <path d="M-10 6v4M0 6v4M10 6v4" stroke="#2dd4bf" strokeWidth="2.5" strokeLinecap="round" />
            <text x="0" y="3.5" textAnchor="middle" fontSize="8" fontWeight="700" fill="#5effd8" fontFamily="var(--mono)">FREE</text>
          </g>
          <path className="logo-trail" d="M24 44 Q58 2 92 44" fill="none" stroke="#5ee0ff" strokeWidth="2.5" strokeDasharray="1 6" strokeLinecap="round" />
        </g>
        <g className="logo-frog">
          <g className="logo-frog-body">
            <g transform="scale(1.3)">
            <ellipse cx="0" cy="6" rx="20" ry="15" fill="url(#frog-skin)" />
            <ellipse cx="1" cy="11" rx="11" ry="7" fill="#c9fbe0" />
            <circle cx="-9" cy="-8" r="8.5" fill="url(#frog-skin)" />
            <circle cx="9" cy="-8" r="8.5" fill="url(#frog-skin)" />
            <g className="logo-eyes">
              <circle cx="-9" cy="-8" r="5.6" fill="#fff" />
              <circle cx="9" cy="-8" r="5.6" fill="#fff" />
              <circle cx="-7.5" cy="-7.5" r="3" fill="#07090d" />
              <circle cx="10.5" cy="-7.5" r="3" fill="#07090d" />
            </g>
            <path d="M-8 4 Q0 12 8 4" stroke="#0b3d2a" strokeWidth="2.6" fill="none" strokeLinecap="round" />
            </g>
          </g>
        </g>
      </svg>
      <span className="wordmark">Tier<b>Hopper</b></span>
    </Link>
  );
}
