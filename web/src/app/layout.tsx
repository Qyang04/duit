import type { Metadata, Viewport } from "next";
import { Hanken_Grotesk, IBM_Plex_Mono, Young_Serif } from "next/font/google";
import type { ReactNode } from "react";

import "./globals.css";
import { Providers } from "./providers";

// next/font downloads the fonts at build time and serves them from our own origin:
// no request to Google from the browser, and the CSP can stay at font-src 'self'.
const display = Young_Serif({ weight: "400", subsets: ["latin"], variable: "--ff-display" });
const sans = Hanken_Grotesk({ subsets: ["latin"], variable: "--ff-sans" });
const mono = IBM_Plex_Mono({
  weight: ["400", "500", "600"],
  subsets: ["latin"],
  variable: "--ff-mono",
});

export const metadata: Metadata = {
  title: "Duit",
  description: "Buku akaun: your personal ledger",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f3eee3" },
    { media: "(prefers-color-scheme: dark)", color: "#12151c" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${sans.variable} ${mono.variable}`}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
