"use client";

import React, { useState } from "react";
import { SliceRow } from "@/lib/api/research";
import { L } from "@/lib/labels";

interface SlicesTabProps {
  slices: SliceRow[];
}

export function SlicesTab({ slices }: SlicesTabProps) {
  const [filterSlice, setFilterSlice] = useState<string>("");

  const sliceTypes = Array.from(new Set(slices.map((s) => s.slice)));
  const filtered = filterSlice
    ? slices.filter((s) => s.slice === filterSlice)
    : slices;

  return (
    <div className="flex flex-col gap-4 text-xs">
      {/* Slice category filter */}
      <div className="flex items-center gap-3 bg-white p-3 rounded-xl border border-slate-200">
        <span className="font-semibold text-slate-600">{L.field.category}{":"}</span>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => setFilterSlice("")}
            className={`rounded-lg px-2.5 py-1 font-semibold transition ${
              filterSlice === ""
                ? "bg-indigo-600 text-white"
                : "bg-slate-100 text-slate-600 hover:bg-slate-200"
            }`}
          >
            {L.action.filterAll}
          </button>
          {sliceTypes.map((st) => (
            <button
              key={st}
              onClick={() => setFilterSlice(st)}
              className={`rounded-lg px-2.5 py-1 font-semibold capitalize transition ${
                filterSlice === st
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-100 text-slate-600 hover:bg-slate-200"
              }`}
            >
              {st.replace("_", " ")}
            </button>
          ))}
        </div>
      </div>

      {/* Slices Table */}
      <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
        <table className="w-full text-left text-xs border-collapse">
          <thead>
            <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
              <th className="py-3 px-4">{"Slice Category"}</th>
              <th className="py-3 px-4">{"Slice Key"}</th>
              <th className="py-3 px-4">{"Target Metric"}</th>
              <th className="py-3 px-4">{"Value"}</th>
              <th className="py-3 px-4">{"95% CI"}</th>
              <th className="py-3 px-4">{"Sample Size (n)"}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {filtered.map((s, idx) => (
              <tr key={`${s.slice}_${s.key}_${idx}`} className="hover:bg-slate-50 transition">
                <td className="py-3 px-4 font-semibold text-slate-700 capitalize">
                  {s.slice.replace("_", " ")}
                </td>
                <td className="py-3 px-4 font-bold text-slate-900 font-mono text-[11px]">
                  {s.key}
                </td>
                <td className="py-3 px-4 text-slate-600 font-mono text-[11px]">
                  {s.metric}
                </td>
                <td className="py-3 px-4 font-bold text-slate-900">
                  {s.value !== null ? s.value.toFixed(2) : "—"}
                </td>
                <td className="py-3 px-4 font-mono text-slate-500 text-[11px]">
                  {s.ci_low !== null && s.ci_high !== null
                    ? `[${s.ci_low.toFixed(2)}, ${s.ci_high.toFixed(2)}]`
                    : "—"}
                </td>
                <td className="py-3 px-4 text-slate-700 font-medium">
                  {s.n}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
