"use client";

import React from "react";
import { MetricsV1, Metric } from "@/lib/api/research";
import { L } from "@/lib/labels";

interface StagesTabProps {
  metrics: MetricsV1;
}

function MetricRow({ name, metric }: { name: string; metric?: Metric }) {
  if (!metric || metric.value === null) {
    return (
      <tr className="border-b border-slate-100">
        <td className="py-2.5 px-4 font-medium text-slate-700">{name}</td>
        <td className="py-2.5 px-4 text-slate-400">{"—"}</td>
        <td className="py-2.5 px-4 text-slate-400">{"—"}</td>
        <td className="py-2.5 px-4 text-slate-400">{"—"}</td>
        <td className="py-2.5 px-4 text-slate-400">{"—"}</td>
      </tr>
    );
  }

  return (
    <tr className="border-b border-slate-100 hover:bg-slate-50 transition">
      <td className="py-2.5 px-4 font-semibold text-slate-800">{name}</td>
      <td className="py-2.5 px-4 font-bold text-slate-900">{metric.value.toFixed(2)}</td>
      <td className="py-2.5 px-4 text-slate-500 font-mono text-[11px]">
        {"["}{metric.ci_low?.toFixed(2)}{", "}{metric.ci_high?.toFixed(2)}{"]"}
      </td>
      <td className="py-2.5 px-4 text-slate-600">{metric.n}</td>
      <td className="py-2.5 px-4">
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
            metric.status === "final"
              ? "bg-emerald-100 text-emerald-800"
              : metric.status === "provisional"
              ? "bg-amber-100 text-amber-800"
              : "bg-rose-100 text-rose-800"
          }`}
        >
          {metric.status}
        </span>
      </td>
    </tr>
  );
}

export function StagesTab({ metrics }: StagesTabProps) {
  const { s3, s4, s5 } = metrics.stages;

  return (
    <div className="flex flex-col gap-6 text-xs">
      {/* Stage 3: Triage & Heatmap */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between mb-3 border-b border-slate-100 pb-2">
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-700">
            {"Stage 3: Triage & Tumor Mask"}
          </h3>
          <span className="text-slate-500">
            {"Coverage: "}
            <strong>{s3?.coverage !== undefined ? `${(s3.coverage * 100).toFixed(0)}%` : "—"}</strong>
          </span>
        </div>

        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-slate-400 font-semibold border-b border-slate-100 pb-1">
              <th className="py-2 px-4">{L.field.scoreKind}</th>
              <th className="py-2 px-4">{"Value"}</th>
              <th className="py-2 px-4">{"95% CI"}</th>
              <th className="py-2 px-4">{"n"}</th>
              <th className="py-2 px-4">{L.field.status}</th>
            </tr>
          </thead>
          <tbody>
            <MetricRow name="S3-F1 (Tumor-tile F1)" metric={s3?.f1} />
            <MetricRow name="S3-P@K (Hotspot Precision@K)" metric={s3?.p_at_k} />
          </tbody>
        </table>
      </div>

      {/* Stage 4: Mitosis Scoring */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between mb-3 border-b border-slate-100 pb-2">
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-700">
            {"Stage 4: Mitosis Detection & Verification"}
          </h3>
          {s4?.ap !== null && s4?.ap !== undefined && (
            <span className="text-slate-500">
              {"AP: "}
              <strong>{s4.ap.toFixed(2)}</strong>
            </span>
          )}
        </div>

        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-slate-400 font-semibold border-b border-slate-100 pb-1">
              <th className="py-2 px-4">{L.field.scoreKind}</th>
              <th className="py-2 px-4">{"Value"}</th>
              <th className="py-2 px-4">{"95% CI"}</th>
              <th className="py-2 px-4">{"n"}</th>
              <th className="py-2 px-4">{L.field.status}</th>
            </tr>
          </thead>
          <tbody>
            <MetricRow name="NS-M F1 (1:1 Object-level)" metric={s4?.f1} />
            <MetricRow name="Precision" metric={s4?.precision} />
            <MetricRow name="Recall" metric={s4?.recall} />
            <MetricRow name="Count MAE / 2mm²" metric={s4?.count_mae_per_2mm2} />
            <MetricRow name="Count Bias / 2mm²" metric={s4?.count_bias_per_2mm2} />
          </tbody>
        </table>
      </div>

      {/* Stage 5: Nottingham Grading */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between mb-3 border-b border-slate-100 pb-2">
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-700">
            {"Stage 5: Nottingham Grading & Architecture"}
          </h3>
        </div>

        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-slate-400 font-semibold border-b border-slate-100 pb-1">
              <th className="py-2 px-4">{L.field.scoreKind}</th>
              <th className="py-2 px-4">{"Value"}</th>
              <th className="py-2 px-4">{"95% CI"}</th>
              <th className="py-2 px-4">{"n"}</th>
              <th className="py-2 px-4">{L.field.status}</th>
            </tr>
          </thead>
          <tbody>
            <MetricRow name="F1_T (Tubules macro-F1)" metric={s5?.f1_t} />
            <MetricRow name="F1_P (Pleomorphism macro-F1)" metric={s5?.f1_p} />
            <MetricRow name="F1_M (Mitotic score macro-F1)" metric={s5?.f1_m} />
            <MetricRow name="F1_high (Band 8-9 F1)" metric={s5?.f1_high} />
            <MetricRow name="macroF1_LM (Band 3-6 macro-F1)" metric={s5?.macro_f1_lm} />
            <MetricRow name="Sum MAE (Nottingham sum error)" metric={s5?.sum_mae} />
            <MetricRow name="QWK (Quadratic Weighted Kappa)" metric={s5?.qwk} />
            <MetricRow name="Histotype F1" metric={s5?.histotype_f1} />
            <MetricRow name="ILC F1" metric={s5?.ilc_f1} />
          </tbody>
        </table>
      </div>
    </div>
  );
}
