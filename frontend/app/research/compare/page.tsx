"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { CompareV1, RunSummary, getRuns, getCompare } from "@/lib/api/research";
import { L } from "@/lib/labels";
import { useAuth } from "@/lib/auth/AuthProvider";

export default function ComparePage() {
  const { can } = useAuth();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [runAId, setRunAId] = useState<string>("run_01");
  const [runBId, setRunBId] = useState<string>("run_02");
  const [compareData, setCompareData] = useState<CompareV1 | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getRuns().then((res) => {
      setRuns(res.items);
      if (res.items.length >= 2) {
        setRunAId(res.items[0].id);
        setRunBId(res.items[1].id);
      }
    });
  }, []);

  useEffect(() => {
    if (!runAId || !runBId || runAId === runBId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);

    getCompare(runAId, runBId)
      .then((res) => {
        if (!cancelled) {
          setCompareData(res);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message || String(err));
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [runAId, runBId]);

  if (!can("research:read")) {
    return (
      <div className="flex h-96 items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-rose-200 bg-rose-50 p-6 text-center text-xs text-rose-800">
          <h2 className="text-sm font-bold mb-2">{L.error.forbidden}</h2>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col flex-1 overflow-y-auto bg-slate-50 p-6 gap-6">
      {/* Header bar */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Link href="/research" className="text-xs text-indigo-600 hover:underline">
              {"← Back to Research"}
            </Link>
          </div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900">
            {L.heading.compareRuns}
          </h1>
          <p className="text-xs text-slate-500 mt-1">
            {"Paired comparison of validation runs with bootstrap delta CIs"}
          </p>
        </div>

        {/* Run selectors */}
        <div className="flex items-center gap-3 text-xs">
          <div className="flex items-center gap-2">
            <span className="font-bold text-slate-700">{"Run A:"}</span>
            <select
              value={runAId}
              onChange={(e) => setRunAId(e.target.value)}
              className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800 font-mono"
            >
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name} ({r.id})
                </option>
              ))}
            </select>
          </div>

          <span className="font-bold text-slate-400">{"vs"}</span>

          <div className="flex items-center gap-2">
            <span className="font-bold text-slate-700">{"Run B:"}</span>
            <select
              value={runBId}
              onChange={(e) => setRunBId(e.target.value)}
              className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800 font-mono"
            >
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name} ({r.id})
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {error ? (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-xs font-medium text-rose-700">
          {error}
        </div>
      ) : loading || !compareData ? (
        <div className="p-8 text-center text-xs text-slate-500 font-medium animate-pulse">
          {L.status.processing}
        </div>
      ) : (
        <>
          {/* Main Δ Metrics Table */}
          <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden text-xs">
            <div className="p-4 border-b border-slate-100 font-bold text-slate-900 flex justify-between items-center">
              <span>{"Paired Metric Deltas (Δ = A − B)"}</span>
              <span className="text-[11px] font-mono text-slate-500">
                {"manifest:"}{compareData.manifest_sha256.slice(0, 10)}
              </span>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
                    <th className="py-3 px-4">{L.field.scoreKind}</th>
                    <th className="py-3 px-4">{"Run A (Value)"}</th>
                    <th className="py-3 px-4">{"Run B (Value)"}</th>
                    <th className="py-3 px-4">{L.field.delta}</th>
                    <th className="py-3 px-4">{"Paired 95% CI"}</th>
                    <th className="py-3 px-4">{L.field.mcnemarP}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {compareData.metrics.map((m) => {
                    // Green only when delta_low > 0, red only when delta_high < 0, otherwise neutral
                    const isSignificantPositive = m.delta_low > 0;
                    const isSignificantNegative = m.delta_high < 0;

                    const deltaColor = isSignificantPositive
                      ? "text-emerald-700 bg-emerald-50 border border-emerald-200"
                      : isSignificantNegative
                      ? "text-rose-700 bg-rose-50 border border-rose-200"
                      : "text-slate-700 bg-slate-100";

                    return (
                      <tr key={m.metric} className="hover:bg-slate-50 transition">
                        <td className="py-3.5 px-4 font-bold text-slate-900">
                          {m.metric}
                        </td>
                        <td className="py-3.5 px-4 font-mono font-medium">
                          {m.a.value !== null ? m.a.value.toFixed(2) : "—"}
                        </td>
                        <td className="py-3.5 px-4 font-mono font-medium">
                          {m.b.value !== null ? m.b.value.toFixed(2) : "—"}
                        </td>
                        <td className="py-3.5 px-4">
                          <span className={`rounded px-2 py-0.5 font-bold font-mono text-[11px] ${deltaColor}`}>
                            {m.delta > 0 ? `+${m.delta.toFixed(2)}` : m.delta.toFixed(2)}
                          </span>
                        </td>
                        <td className="py-3.5 px-4 font-mono text-slate-500 text-[11px]">
                          {"["}{m.delta_low.toFixed(2)}{", "}{m.delta_high.toFixed(2)}{"]"}
                        </td>
                        <td className="py-3.5 px-4 font-mono text-slate-600">
                          {m.mcnemar_p !== undefined ? m.mcnemar_p.toFixed(3) : "—"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Slices Δ Table */}
          <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden text-xs">
            <div className="p-4 border-b border-slate-100 font-bold text-slate-900">
              {"Slice Stratified Deltas"}
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs border-collapse">
                <thead>
                  <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
                    <th className="py-3 px-4">{L.field.category}</th>
                    <th className="py-3 px-4">{"Slice Key"}</th>
                    <th className="py-3 px-4">{L.field.scoreKind}</th>
                    <th className="py-3 px-4">{L.field.delta}</th>
                    <th className="py-3 px-4">{"95% CI"}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {compareData.slices.map((sl, idx) => (
                    <tr key={`${sl.slice}_${sl.key}_${idx}`} className="hover:bg-slate-50 transition">
                      <td className="py-3 px-4 font-semibold text-slate-700 capitalize">
                        {sl.slice.replace("_", " ")}
                      </td>
                      <td className="py-3 px-4 font-mono font-bold text-slate-900">
                        {sl.key}
                      </td>
                      <td className="py-3 px-4 text-slate-600 font-mono">
                        {sl.metric}
                      </td>
                      <td className="py-3 px-4">
                        <span
                          className={`font-mono font-bold ${
                            sl.delta_low > 0
                              ? "text-emerald-600"
                              : sl.delta_high < 0
                              ? "text-rose-600"
                              : "text-slate-600"
                          }`}
                        >
                          {sl.delta > 0 ? `+${sl.delta.toFixed(2)}` : sl.delta.toFixed(2)}
                        </span>
                      </td>
                      <td className="py-3 px-4 font-mono text-slate-500">
                        {"["}{sl.delta_low.toFixed(2)}{", "}{sl.delta_high.toFixed(2)}{"]"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Flips List */}
          <div className="rounded-xl border border-slate-200 bg-white shadow-sm p-5 text-xs">
            <h3 className="font-bold text-slate-900 mb-3 border-b border-slate-100 pb-2">
              {"Discordant Case Flips"}
            </h3>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {compareData.flips.map((f, i) => (
                <div
                  key={`${f.slide_id}_${i}`}
                  className="rounded-lg border border-slate-200 bg-slate-50 p-3 flex flex-col gap-1"
                >
                  <div className="flex justify-between items-center">
                    <span className="font-mono font-bold text-slate-900">{f.slide_id}</span>
                    <span className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-semibold text-indigo-700 uppercase">
                      {f.component}
                    </span>
                  </div>
                  <div className="flex justify-between items-center text-[11px] pt-1">
                    <span className={f.a_correct ? "text-emerald-700 font-semibold" : "text-rose-600"}>
                      {"Run A: "}{f.a_correct ? "Correct ✓" : "Wrong ✕"}
                    </span>
                    <span className={f.b_correct ? "text-emerald-700 font-semibold" : "text-rose-600"}>
                      {"Run B: "}{f.b_correct ? "Correct ✓" : "Wrong ✕"}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
