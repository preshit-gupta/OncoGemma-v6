"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { ItemRow } from "@/lib/api/research";
import { L } from "@/lib/labels";

interface ItemsTabProps {
  runId: string;
  items: ItemRow[];
}

export function ItemsTab({ runId, items }: ItemsTabProps) {
  const router = useRouter();
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [filterErrorOnly, setFilterErrorOnly] = useState<boolean>(false);

  const filtered = items.filter((item) => {
    if (filterStatus && item.status !== filterStatus) return false;
    if (filterErrorOnly && item.sum_error === 0) return false;
    return true;
  });

  const handleExportCsv = () => {
    const headers = [
      "slide_id",
      "patient_id",
      "status",
      "failed_stage",
      "error_class",
      "gt_grade",
      "pred_grade",
      "gt_total",
      "pred_total",
      "sum_error",
      "runtime_s",
      "cost_usd",
    ];

    const rows = filtered.map((i) => [
      i.slide_id,
      i.patient_id,
      i.status,
      i.failed_stage || "",
      i.error_class || "",
      i.gt.grade ?? "",
      i.pred.grade ?? "",
      i.gt.total ?? "",
      i.pred.total ?? "",
      i.sum_error ?? "",
      i.runtime_s ?? "",
      i.cost_usd ?? "",
    ]);

    const csvContent =
      "data:text/csv;charset=utf-8," +
      [headers.join(","), ...rows.map((e) => e.join(","))].join("\n");
    const encodedUri = encodeURI(csvContent);
    const link = document.createElement("a");
    link.setAttribute("href", encodedUri);
    link.setAttribute("download", `run_${runId}_items.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className="flex flex-col gap-4 text-xs">
      {/* Filters and CSV export button */}
      <div className="flex flex-wrap items-center justify-between gap-4 bg-white p-3 rounded-xl border border-slate-200">
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-slate-600">{L.field.status}{":"}</span>
            <select
              value={filterStatus}
              onChange={(e) => setFilterStatus(e.target.value)}
              className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800"
            >
              <option value="">{L.action.filterAll}</option>
              <option value="succeeded">{L.status.done}</option>
              <option value="failed">{L.status.failed}</option>
            </select>
          </div>

          <label className="flex items-center gap-2 cursor-pointer font-medium text-slate-700">
            <input
              type="checkbox"
              checked={filterErrorOnly}
              onChange={(e) => setFilterErrorOnly(e.target.checked)}
              className="rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
            />
            <span>{"Misgrades only (|error| > 0)"}</span>
          </label>
        </div>

        <button
          onClick={handleExportCsv}
          className="rounded-lg border border-slate-300 px-3 py-1.5 font-semibold text-slate-700 hover:bg-slate-50 transition"
        >
          {L.action.exportCsv}
        </button>
      </div>

      {/* Items table */}
      <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs border-collapse">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
                <th className="py-3 px-4">{L.field.slideFile}</th>
                <th className="py-3 px-4">{L.field.patientId}</th>
                <th className="py-3 px-4">{L.field.status}</th>
                <th className="py-3 px-4">{"GT (T/P/M/G)"}</th>
                <th className="py-3 px-4">{"Pred (T/P/M/G)"}</th>
                <th className="py-3 px-4">{L.field.sumError}</th>
                <th className="py-3 px-4">{"Runtime"}</th>
                <th className="py-3 px-4">{L.field.costUsd}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {filtered.map((item) => {
                const isMisgrade = item.sum_error !== 0 && item.sum_error !== null;

                return (
                  <tr
                    key={item.slide_id}
                    onClick={() =>
                      router.push(`/research/runs/${runId}/items/${item.slide_id}`)
                    }
                    className="hover:bg-slate-50 cursor-pointer transition"
                  >
                    <td className="py-3.5 px-4 font-mono font-bold text-slate-900">
                      {item.slide_id}
                    </td>
                    <td className="py-3.5 px-4 text-slate-600 font-mono text-[11px]">
                      {item.patient_id}
                    </td>
                    <td className="py-3.5 px-4">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                          item.status === "succeeded"
                            ? "bg-emerald-100 text-emerald-800"
                            : "bg-rose-100 text-rose-800"
                        }`}
                      >
                        {item.status}
                      </span>
                    </td>
                    <td className="py-3.5 px-4 font-mono text-slate-700">
                      {item.gt.tubule ?? "—"}
                      {" / "}
                      {item.gt.pleo ?? "—"}
                      {" / "}
                      {item.gt.mitoses ?? "—"}
                      {" · "}
                      <strong className="text-slate-900">
                        {"G"}{item.gt.grade ?? "—"}
                      </strong>
                    </td>
                    <td className="py-3.5 px-4 font-mono text-slate-700">
                      {item.pred.tubule ?? "—"}
                      {" / "}
                      {item.pred.pleo ?? "—"}
                      {" / "}
                      {item.pred.mitoses ?? "—"}
                      {" · "}
                      <strong className="text-slate-900">
                        {"G"}{item.pred.grade ?? "—"}
                      </strong>
                    </td>
                    <td className="py-3.5 px-4">
                      {item.sum_error !== null ? (
                        <span
                          className={`font-bold font-mono ${
                            isMisgrade ? "text-rose-600" : "text-emerald-600"
                          }`}
                        >
                          {item.sum_error > 0
                            ? `+${item.sum_error}`
                            : item.sum_error}
                        </span>
                      ) : (
                        <span className="text-slate-400">{"—"}</span>
                      )}
                    </td>
                    <td className="py-3.5 px-4 text-slate-600">
                      {item.runtime_s ? `${item.runtime_s.toFixed(1)}s` : "—"}
                    </td>
                    <td className="py-3.5 px-4 font-mono text-slate-600">
                      {item.cost_usd ? `$${item.cost_usd.toFixed(2)}` : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
