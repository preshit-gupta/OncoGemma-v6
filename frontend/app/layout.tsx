import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "OncoGemma v4 — Breast Cancer Diagnostic Copilot",
  description: "Same-Day Breast Cancer Examination & Nottingham Grading Copilot",
};

import { L } from "@/lib/labels";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className="h-full">
      <body className="h-full bg-slate-50 text-slate-900 flex flex-col antialiased">
        <header className="bg-slate-900 border-b border-slate-800 text-white px-6 py-3.5 flex items-center justify-between shadow-md">
          <div className="flex items-center space-x-3">
            <div className="w-7 h-7 bg-sky-500 rounded-lg flex items-center justify-center font-bold text-slate-900 text-sm">
              {L.heading.appLogo}
            </div>
            <div>
              <span className="font-bold tracking-tight text-lg">{L.heading.appTitle}</span>
              <span className="ml-2 text-xs bg-sky-950 text-sky-400 border border-sky-800 px-2 py-0.5 rounded font-mono">
                {L.heading.appSubtitle}
              </span>
            </div>
          </div>
          <div className="flex items-center space-x-4 text-sm text-slate-400">
            <span className="flex items-center space-x-1.5">
              <span className="w-2 h-2 rounded-full bg-emerald-400"></span>
              <span>{L.status.done}</span>
            </span>
          </div>
        </header>
        <main className="flex-1 overflow-hidden relative flex flex-col">{children}</main>
      </body>
    </html>
  );
}
