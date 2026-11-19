import type { Metadata } from "next";
import { JetBrains_Mono, Inter } from "next/font/google";
import { AppNav } from "@/components/AppNav";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "STOCKIFY",
  description:
    "NSE simulation lab: record Kite market data, backtest and paper-trade " +
    "equity and F&O strategies.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${jetbrainsMono.variable}`}
      // Phone browsers (Chrome / the Google app) add their own attributes to <html> before React loads; ignore
      // those on this one tag only, so the dev overlay doesn't flag them. Children are still checked.
      suppressHydrationWarning
    >
      <body>
        <a href="#main" className="stk-skip">
          Skip to the page
        </a>
        <AppNav />
        <main id="main" tabIndex={-1} className="stk-main">{children}</main>
      </body>
    </html>
  );
}
