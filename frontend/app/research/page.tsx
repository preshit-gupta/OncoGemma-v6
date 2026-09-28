"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { L } from "@/lib/labels";
import { useAuth } from "@/lib/auth/AuthProvider";
import { RunSummary, getRuns } from "@/lib/api/research";
import { BatchModal } from "@/components/research/BatchModal";

export default function ResearchDashboardPage() {
  const router = useRouter();
  const { can } = useAuth();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [filterDataset, setFilterDataset] = useState<string>("");
  const [filterSplit, setFilterSplit] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");

  // Batch modal
  const [batchModalOpen, setBatchModalOpen] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getRuns({
      dataset: filterDataset || undefined,
      split: filterSplit || undefined,
      status: filterStatus || undefined,
    })
      .then((res) => {
        if (!cancelled) {
          setRuns(res.items);
          setLoading(false);
        }
      })
      .catch((err: any) => {
        if (!cancelled) {
          setError(err.message || String(err));
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [filterDataset, filterSplit, filterStatus]);

  if (!can("research:read")) {
    return (
      <div className="flex h-96 items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-rose-200 bg-rose-50 p-6 text-center text-xs text-rose-800">
          <h2 className="text-sm font-bold mb-2">{L.error.forbidden}</h2>
          <p>{L.field.role}{": "}{L.field.status}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col flex-1 overflow-y-auto bg-slate-50 p-6 gap-6">
      {/* Top Header & Navigation Sub-rail */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900">
            {L.heading.research}
          </h1>
          <p className="text-xs text-slate-500 mt-1">
            {L.heading.workflowPipeline}
          </p>
        </div>

        {/* Research section navigation */}
        <div className="flex flex-wrap items-center gap-2">
          <Link
            href="/research/compare"
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition"
          >
            {L.action.compare}
          </Link>
          <Link
            href="/research/issues"
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition"
          >
            {L.heading.issueRegister}
          </Link>
          {can("research:annotate") && (
            <Link
              href="/research/annotate"
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition"
            >
              {L.heading.annotationTasks}
            </Link>
          )}
          {can("labels:qa") && (
            <Link
              href="/research/labels-qa"
              className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition"
            >
              {L.heading.labelQaQueue}
            </Link>
          )}

          {can("batch:create") && (
            <button
              onClick={() => setBatchModalOpen(true)}
              className="rounded-lg bg-indigo-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 shadow-sm transition"
            >
              {L.action.newBatch}
            </button>
          )}
        </div>
      </div>

      {/* Filter Bar */}
      <div className="flex flex-wrap items-center gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm text-xs">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-slate-500">{L.field.dataset}{":"}</span>
          <select
            value={filterDataset}
            onChange={(e) => setFilterDataset(e.target.value)}
            className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800"
          >
            <option value="">{L.action.filterAll}</option>
            <option value="tcga_brca_dx">{"tcga_brca_dx"}</option>
            <option value="midogpp_breast">{"midogpp_breast"}</option>
            <option value="bcnb">{"bcnb"}</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <span className="font-semibold text-slate-500">{L.field.split}{":"}</span>
          <select
            value={filterSplit}
            onChange={(e) => setFilterSplit(e.target.value)}
            className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800"
          >
            <option value="">{L.action.filterAll}</option>
            <option value="val">{"val"}</option>
            <option value="test">{"test"}</option>
            <option value="train">{"train"}</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <span className="font-semibold text-slate-500">{L.field.status}{":"}</span>
          <select
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value)}
            className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800"
          >
            <option value="">{L.action.filterAll}</option>
            <option value="completed">{L.status.done}</option>
            <option value="running">{L.status.running}</option>
            <option value="failed">{L.status.failed}</option>
          </select>
        </div>
      </div>

      {/* Runs Table */}
      <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
        {loading ? (
          <div className="p-8 text-center text-xs text-slate-500 font-medium animate-pulse">
            {L.status.processing}
          </div>
        ) : error ? (
          <div className="p-6 text-xs text-rose-700 bg-rose-50 font-medium">
            {error}
          </div>
        ) : runs.length === 0 ? (
          <div className="p-8 text-center text-xs text-slate-500">
            {L.help.noUsersFound}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
                  <th className="py-3 px-4">{L.field.runName}</th>
                  <th className="py-3 px-4">{L.field.dataset}</th>
                  <th className="py-3 px-4">{L.field.split}</th>
                  <th className="py-3 px-4">{L.field.arm}</th>
                  <th className="py-3 px-4">{L.field.gate}</th>
                  <th className="py-3 px-4">{"NS-M F1"}</th>
                  <th className="py-3 px-4">{"NS-G macro-F1"}</th>
                  <th className="py-3 px-4">{L.field.status}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {runs.map((r) => {
                  const nsm = r.headline.ns_m;
                  const nsg = r.headline.ns_g;

                  return (
                    <tr
                      key={r.id}
                      onClick={() => router.push(`/research/runs/${r.id}`)}
                      className="hover:bg-slate-50 cursor-pointer transition"
                    >
                      <td className="py-3.5 px-4 font-semibold text-slate-900">
                        <div className="flex items-center gap-2">
                          <span>{r.name}</span>
                          {r.is_locked_test && (
                            <span className="rounded bg-purple-100 px-1.5 py-0.5 text-[10px] font-bold text-purple-700">
                              {L.field.lockedTest}
                            </span>
                          )}
                        </div>
                        <span className="text-[10px] text-slate-400 font-mono block mt-0.5">
                          {r.id}
                        </span>
                      </td>
                      <td className="py-3.5 px-4 text-slate-700 font-mono text-[11px]">
                        {r.dataset}
                      </td>
                      <td className="py-3.5 px-4 text-slate-700 uppercase font-bold text-[10px]">
                        {r.split}
                      </td>
                      <td className="py-3.5 px-4 text-slate-600 font-mono text-[11px]">
                        {r.arm || "—"}
                      </td>
                      <td className="py-3.5 px-4">
                        <span
                          className={`rounded px-2 py-0.5 text-[10px] font-bold ${
                            r.gate.valid
                              ? "bg-emerald-100 text-emerald-800"
                              : "bg-rose-100 text-rose-800"
                          }`}
                        >
                          {r.gate.valid ? "VALID" : "INVALID"}
                        </span>
                      </td>
                      <td className="py-3.5 px-4">
                        {nsm && nsm.value !== null ? (
                          <div className="flex items-center gap-1.5">
                            <span className="font-bold text-slate-900">
                              {nsm.value.toFixed(2)}
                            </span>
                            <span className="text-slate-400 text-[10px]">
                              {"["}{nsm.ci_low?.toFixed(2)}{", "}{nsm.ci_high?.toFixed(2)}{"]"}
                            </span>
                            <span
                              className={`rounded px-1 text-[9px] font-semibold ${
                                nsm.status === "final"
                                  ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                                  : nsm.status === "provisional"
                                  ? "bg-amber-50 text-amber-700 border border-amber-200"
                                  : "bg-rose-50 text-rose-700 border border-rose-200"
                              }`}
                            >
                              {nsm.status}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400">{"—"}</span>
                        )}
                      </td>
                      <td className="py-3.5 px-4">
                        {nsg && nsg.value !== null ? (
                          <div className="flex items-center gap-1.5">
                            <span className="font-bold text-slate-900">
                              {nsg.value.toFixed(2)}
                            </span>
                            <span className="text-slate-400 text-[10px]">
                              {"["}{nsg.ci_low?.toFixed(2)}{", "}{nsg.ci_high?.toFixed(2)}{"]"}
                            </span>
                            <span
                              className={`rounded px-1 text-[9px] font-semibold ${
                                nsg.status === "final"
                                  ? "bg-emerald-50 text-emerald-700 border border-emerald-200"
                                  : nsg.status === "provisional"
                                  ? "bg-amber-50 text-amber-700 border border-amber-200"
                                  : "bg-rose-50 text-rose-700 border border-rose-200"
                              }`}
                            >
                              {nsg.status}
                            </span>
                          </div>
                        ) : (
                          <span className="text-slate-400">{"—"}</span>
                        )}
                      </td>
                      <td className="py-3.5 px-4">
                        <span
                          className={`rounded px-2 py-0.5 text-[10px] font-semibold ${
                            r.status === "completed"
                              ? "bg-emerald-100 text-emerald-800"
                              : r.status === "running"
                              ? "bg-sky-100 text-sky-800 animate-pulse"
                              : "bg-rose-100 text-rose-800"
                          }`}
                        >
                          {r.status === "completed"
                            ? L.status.done
                            : r.status === "running"
                            ? L.status.running
                            : L.status.failed}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <BatchModal
        isOpen={batchModalOpen}
        onClose={() => setBatchModalOpen(false)}
        onSuccess={() => {
          // reload runs
          getRuns().then((res) => setRuns(res.items));
        }}
      />
    </div>
  );
}
