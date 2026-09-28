"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ItemRow, DecisionNode, getRunItemDetail } from "@/lib/api/research";
import { L } from "@/lib/labels";
import { useAuth } from "@/lib/auth/AuthProvider";

function DecisionNodeView({ node }: { node: DecisionNode }) {
  const [collapsed, setCollapsed] = useState<boolean>(false);
  const [showJson, setShowJson] = useState<boolean>(false);

  return (
    <div className="flex flex-col border border-slate-200 rounded-xl bg-white p-4 shadow-xs text-xs mb-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setCollapsed(!collapsed)}
            className="text-slate-400 hover:text-slate-600 font-bold font-mono text-sm"
          >
            {collapsed ? "▶" : "▼"}
          </button>
          <span className="font-bold text-slate-900 font-mono">{node.task}</span>
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
            {node.entity_type}{": "}{node.entity_id}
          </span>
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
              node.status === "ok"
                ? "bg-emerald-100 text-emerald-800"
                : "bg-rose-100 text-rose-800"
            }`}
          >
            {node.status}
          </span>
        </div>

        <div className="flex items-center gap-3 text-slate-500 font-mono text-[11px]">
          <span>
            {node.producer_id}@{node.producer_version}
          </span>
          <span>{node.latency_ms}{"ms"}</span>
          <button
            onClick={() => setShowJson(!showJson)}
            className="text-indigo-600 font-semibold hover:underline"
          >
            {showJson ? "Hide JSON" : "Show JSON"}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="mt-3 pl-4 border-l-2 border-slate-100 flex flex-col gap-3">
          {showJson && (
            <div className="rounded-lg bg-slate-900 p-3 text-slate-100 font-mono text-[11px] overflow-x-auto">
              <pre>{JSON.stringify({ input: node.input_spec, output: node.output }, null, 2)}</pre>
            </div>
          )}

          {node.children && node.children.length > 0 && (
            <div className="flex flex-col gap-2 pt-2">
              {node.children.map((child) => (
                <DecisionNodeView key={child.id} node={child} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function ItemDrillDownPage() {
  const params = useParams();
  const runId = params.id as string;
  const slideId = params.slideId as string;
  const { can } = useAuth();

  const [item, setItem] = useState<ItemRow | null>(null);
  const [caseId, setCaseId] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<DecisionNode[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    getRunItemDetail(runId, slideId)
      .then((res) => {
        if (!cancelled) {
          setItem(res.item);
          setCaseId(res.case_id);
          setDecisions(res.decisions);
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
  }, [runId, slideId]);

  if (!can("research:read")) {
    return (
      <div className="flex h-96 items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-rose-200 bg-rose-50 p-6 text-center text-xs text-rose-800">
          <h2 className="text-sm font-bold mb-2">{L.error.forbidden}</h2>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-96 items-center justify-center">
        <div className="text-xs text-slate-500 font-medium animate-pulse">
          {L.status.processing}
        </div>
      </div>
    );
  }

  if (error || !item) {
    return (
      <div className="p-6">
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-xs font-medium text-rose-700">
          {error || "Item detail not found"}
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
            <Link
              href={`/research/runs/${runId}`}
              className="text-xs text-indigo-600 hover:underline"
            >
              {"← Back to Run"}
            </Link>
            <span className="text-slate-300">{"/"}</span>
            <span className="font-mono text-xs text-slate-500">{slideId}</span>
          </div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900 font-mono">
            {slideId}
          </h1>
          <span className="text-xs text-slate-500 font-mono">
            {L.field.patientId}{": "}{item.patient_id}
          </span>
        </div>

        {caseId && (
          <Link
            href={`/cases/${caseId}`}
            className="rounded-lg border border-indigo-300 bg-indigo-50 px-3.5 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 transition shadow-xs"
          >
            {"Open in Clinical Viewer (Read-only)"}
          </Link>
        )}
      </div>

      {/* GT vs Prediction Card */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs">
        <h3 className="font-bold text-slate-900 mb-4 pb-2 border-b border-slate-100">
          {L.field.groundTruth}{" vs "}{L.field.prediction}
        </h3>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs">
          <div className="rounded-lg bg-slate-50 p-3 border border-slate-100">
            <span className="text-slate-400 block mb-1">{L.field.grade}</span>
            <div className="flex justify-between items-center text-sm font-bold">
              <span>{"GT: "}{item.gt.grade ?? "—"}</span>
              <span className={item.gt.grade === item.pred.grade ? "text-emerald-600" : "text-rose-600"}>
                {"Pred: "}{item.pred.grade ?? "—"}
              </span>
            </div>
          </div>

          <div className="rounded-lg bg-slate-50 p-3 border border-slate-100">
            <span className="text-slate-400 block mb-1">{L.field.tubuleScore}</span>
            <div className="flex justify-between items-center text-sm font-bold">
              <span>{"GT: "}{item.gt.tubule ?? "—"}</span>
              <span className={item.gt.tubule === item.pred.tubule ? "text-emerald-600" : "text-rose-600"}>
                {"Pred: "}{item.pred.tubule ?? "—"}
              </span>
            </div>
          </div>

          <div className="rounded-lg bg-slate-50 p-3 border border-slate-100">
            <span className="text-slate-400 block mb-1">{L.field.pleoScore}</span>
            <div className="flex justify-between items-center text-sm font-bold">
              <span>{"GT: "}{item.gt.pleo ?? "—"}</span>
              <span className={item.gt.pleo === item.pred.pleo ? "text-emerald-600" : "text-rose-600"}>
                {"Pred: "}{item.pred.pleo ?? "—"}
              </span>
            </div>
          </div>

          <div className="rounded-lg bg-slate-50 p-3 border border-slate-100">
            <span className="text-slate-400 block mb-1">{L.field.mitosisScore}</span>
            <div className="flex justify-between items-center text-sm font-bold">
              <span>{"GT: "}{item.gt.mitoses ?? "—"}</span>
              <span className={item.gt.mitoses === item.pred.mitoses ? "text-emerald-600" : "text-rose-600"}>
                {"Pred: "}{item.pred.mitoses ?? "—"}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* Decision Tree */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs">
        <h3 className="font-bold text-slate-900 mb-4 pb-2 border-b border-slate-100">
          {L.heading.decisionTree}
        </h3>

        {decisions.length === 0 ? (
          <p className="text-slate-400 italic">{"No decision records logged for this slide"}</p>
        ) : (
          <div className="flex flex-col gap-2">
            {decisions.map((dn) => (
              <DecisionNodeView key={dn.id} node={dn} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
