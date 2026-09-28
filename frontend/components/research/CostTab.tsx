"use client";

import React from "react";
import { MetricsV1 } from "@/lib/api/research";
import { L } from "@/lib/labels";

interface CostTabProps {
  metrics: MetricsV1;
}

export function CostTab({ metrics }: CostTabProps) {
  const { cost } = metrics;
  const models = Object.entries(cost.by_model);

  return (
    <div className="flex flex-col gap-6 text-xs">
      {/* Overview Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <span className="text-slate-400 font-medium block mb-1">{"Total Run Cost"}</span>
          <span className="text-2xl font-bold text-slate-900">${cost.usd_total.toFixed(2)}</span>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <span className="text-slate-400 font-medium block mb-1">{"Cost per Slide"}</span>
          <span className="text-2xl font-bold text-slate-900">${cost.usd_per_slide.toFixed(2)}</span>
        </div>
      </div>

      {/* Model Breakdown Table */}
      <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
        <div className="p-4 border-b border-slate-100 font-bold text-slate-900">
          {L.heading.costAndLatency}
        </div>
        <table className="w-full text-left text-xs border-collapse">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
              <th className="py-3 px-4">{"Model / Task"}</th>
              <th className="py-3 px-4">{"Total Invocations"}</th>
              <th className="py-3 px-4">{L.field.costUsd}</th>
              <th className="py-3 px-4">{"p50 Latency (ms)"}</th>
              <th className="py-3 px-4">{"p95 Latency (ms)"}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {models.map(([modelName, stats]) => (
              <tr key={modelName} className="hover:bg-slate-50 transition">
                <td className="py-3 px-4 font-mono font-bold text-slate-800">
                  {modelName}
                </td>
                <td className="py-3 px-4 text-slate-700">
                  {stats.calls}
                </td>
                <td className="py-3 px-4 font-mono text-slate-900 font-semibold">
                  ${stats.usd.toFixed(2)}
                </td>
                <td className="py-3 px-4 text-slate-600 font-mono">
                  {stats.p50_ms}{"ms"}
                </td>
                <td className="py-3 px-4 text-slate-600 font-mono">
                  {stats.p95_ms}{"ms"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
