import type { Metadata, Viewport } from "next";
import { Fredoka, Geist, Geist_Mono, VT323 } from "next/font/google";
import "./globals.css";

const geist = Geist({ subsets: ["latin"], variable: "--f-geist" });
const geistMono = Geist_Mono({ subsets: ["latin"], variable: "--f-geist-mono" });
const vt323 = VT323({ subsets: ["latin"], weight: "400", variable: "--f-vt323" });
const fredoka = Fredoka({ subsets: ["latin"], weight: ["500", "600"], variable: "--f-fredoka" });

export const metadata: Metadata = {
  title: "TierHopper",
  description: "Free GPU tiers, in rotation.",
  manifest: "/manifest.webmanifest",
  icons: { icon: "/icon.svg" },
};

export const viewport: Viewport = { themeColor: "#07090d", width: "device-width", initialScale: 1 };

// Applies the saved theme before first paint (no flash).
const themeScript = `try{var t=localStorage.getItem("th-theme");if(t)document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="mission" className={`${geist.variable} ${geistMono.variable} ${vt323.variable} ${fredoka.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
