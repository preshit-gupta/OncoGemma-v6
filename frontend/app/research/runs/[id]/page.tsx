"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  RunDetail,
  MetricsV1,
  ItemRow,
  MitosisErrorCard,
  getRun,
  getRunMetrics,
  getRunItems,
  getRunMitosisErrors,
  createIssue,
} from "@/lib/api/research";
import { L } from "@/lib/labels";
import { useAuth } from "@/lib/auth/AuthProvider";
import { StagesTab } from "@/components/research/StagesTab";
import { ConfusionTab } from "@/components/research/ConfusionTab";
import { MitosisCurvesTab } from "@/components/research/MitosisCurvesTab";
import { CalibrationTab } from "@/components/research/CalibrationTab";
import { SlicesTab } from "@/components/research/SlicesTab";
import { ItemsTab } from "@/components/research/ItemsTab";
import { ErrorsTab } from "@/components/research/ErrorsTab";
import { CostTab } from "@/components/research/CostTab";

export default function RunDetailPage() {
  const params = useParams();
  const router = useRouter();
  const runId = params.id as string;
  const { can } = useAuth();

  const [run, setRun] = useState<RunDetail | null>(null);
  const [metrics, setMetrics] = useState<MetricsV1 | null>(null);
  const [items, setItems] = useState<ItemRow[]>([]);
  const [errors, setErrors] = useState<MitosisErrorCard[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<
    "stages" | "confusion" | "curves" | "calibration" | "slices" | "items" | "errors" | "cost"
  >("stages");

  // Issue modal for "Log issue"
  const [issueModalOpen, setIssueModalOpen] = useState<boolean>(false);
  const [issueTitle, setIssueTitle] = useState<string>("");
  const [issueCategory, setIssueCategory] = useState<"biological" | "model" | "staging" | "technical">("model");
  const [issueSeverity, setIssueSeverity] = useState<"critical" | "high" | "medium" | "low">("medium");
  const [issueEvidence, setIssueEvidence] = useState<any[]>([]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);

    Promise.all([
      getRun(runId),
      getRunMetrics(runId),
      getRunItems(runId),
      getRunMitosisErrors(runId),
    ])
      .then(([r, m, it, errs]) => {
        if (!cancelled) {
          setRun(r);
          setMetrics(m);
          setItems(it.items);
          setErrors(errs.items);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setErrorMsg(err.message || String(err));
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [runId]);

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

  if (errorMsg || !run || !metrics) {
    return (
      <div className="p-6">
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-4 text-xs font-medium text-rose-700">
          {errorMsg || "Run data not found"}
        </div>
      </div>
    );
  }

  const isResearchOnly = run.license_scopes.includes("research");
  const isGateValid = run.gate.valid;
  const nsm = metrics.headline.ns_m;
  const nsg = metrics.headline.ns_g;

  const handleLogIssue = (card: MitosisErrorCard) => {
    setIssueTitle(`Mitosis ${card.kind.toUpperCase()} on ${card.slide_id}`);
    setIssueCategory("model");
    setIssueSeverity("medium");
    setIssueEvidence([
      {
        run_id: runId,
        slide_id: card.slide_id,
        decision_record_id: card.decision_record_id,
      },
    ]);
    setIssueModalOpen(true);
  };

  const handleCreateIssueSubmit = async () => {
    if (!issueTitle.trim()) return;
    await createIssue({
      title: issueTitle,
      category: issueCategory,
      severity: issueSeverity,
      evidence: issueEvidence,
      spec_ref: "SPEC-06 §5",
    });
    setIssueModalOpen(false);
    router.push("/research/issues");
  };

  return (
    <div className="flex flex-col flex-1 overflow-y-auto bg-slate-50 p-6 gap-6">
      {/* Header Card */}
      <div className="flex flex-col gap-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div className="flex flex-col">
            <div className="flex items-center gap-2 mb-1">
              <Link href="/research" className="text-xs text-indigo-600 hover:underline">
                {"← "}{L.heading.runs}
              </Link>
              <span className="text-slate-300">{"/"}</span>
              <span className="font-mono text-xs text-slate-500">{run.id}</span>
              {isResearchOnly ? (
                <span className="rounded bg-rose-100 px-2 py-0.5 text-[10px] font-bold text-rose-800 uppercase">
                  {"research-only"}
                </span>
              ) : (
                <span className="rounded bg-emerald-100 px-2 py-0.5 text-[10px] font-bold text-emerald-800 uppercase">
                  {"commercial-ok"}
                </span>
              )}
            </div>
            <h1 className="text-xl font-bold tracking-tight text-slate-900">
              {run.name}
            </h1>
            <div className="flex items-center gap-2 mt-1 text-xs text-slate-500">
              <span className="font-mono">{run.dataset}</span>
              <span>{"·"}</span>
              <span className="uppercase font-semibold">{run.split}</span>
              <span>{"·"}</span>
              <span className="font-mono">{run.arm || "baseline"}</span>
              <span>{"·"}</span>
              <span>{run.created_at.slice(0, 10)}</span>
            </div>
          </div>

          {/* Gate status badge */}
          <div className="flex flex-col items-end">
            <span
              className={`rounded-lg px-3 py-1 text-xs font-bold ${
                isGateValid
                  ? "bg-emerald-100 text-emerald-900 border border-emerald-300"
                  : "bg-rose-100 text-rose-900 border border-rose-300"
              }`}
            >
              {isGateValid ? "GATE: VALID" : "GATE: INVALID"}
            </span>
            <div className="flex items-center gap-2 mt-1.5 text-[11px] font-mono text-slate-500">
              <span>{L.field.intProv}{": "}{run.gate.int_prov.toFixed(2)}</span>
              <span>{"·"}</span>
              <span>{L.field.intFall}{": "}{run.gate.int_fall}</span>
              <span>{"·"}</span>
              <span>{L.field.disjoint}{": "}{run.gate.disjoint ? "yes" : "no"}</span>
            </div>
          </div>
        </div>

        {/* Hashes bar */}
        <div className="flex flex-wrap items-center gap-4 pt-3 border-t border-slate-100 text-[11px] font-mono text-slate-600">
          <div
            onClick={() => navigator.clipboard.writeText(run.config_hash)}
            className="flex items-center gap-1 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded cursor-pointer hover:bg-slate-100"
          >
            <span className="text-slate-400">{"config:"}</span>
            <span>{run.config_hash.slice(0, 12)}</span>
          </div>
          <div
            onClick={() => navigator.clipboard.writeText(run.registry_sha256)}
            className="flex items-center gap-1 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded cursor-pointer hover:bg-slate-100"
          >
            <span className="text-slate-400">{"registry:"}</span>
            <span>{run.registry_sha256.slice(0, 12)}</span>
          </div>
          <div
            onClick={() => navigator.clipboard.writeText(run.manifest_sha256)}
            className="flex items-center gap-1 bg-slate-50 border border-slate-200 px-2 py-0.5 rounded cursor-pointer hover:bg-slate-100"
          >
            <span className="text-slate-400">{"manifest:"}</span>
            <span>{run.manifest_sha256.slice(0, 12)}</span>
          </div>
        </div>
      </div>

      {/* Headline Tiles */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {/* NS-M Headline Tile */}
        <div className="flex flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              {"NS-M Mitosis F1"}
            </span>
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
                !isGateValid
                  ? "bg-rose-100 text-rose-800"
                  : nsm?.status === "final"
                  ? "bg-emerald-100 text-emerald-800"
                  : "bg-amber-100 text-amber-800"
              }`}
            >
              {!isGateValid ? "invalid" : nsm?.status}
            </span>
          </div>

          {!isGateValid ? (
            <div className="my-2">
              <span className="text-xl font-bold text-rose-600 uppercase">
                {"invalid"}
              </span>
              <p className="text-[11px] text-slate-400 mt-1">
                {"Measurement gate failed"}
              </p>
            </div>
          ) : (
            <div className="my-1">
              <span className="text-3xl font-black text-slate-900 tracking-tight">
                {nsm && nsm.value !== null ? nsm.value.toFixed(2) : "—"}
              </span>
              <div className="text-xs text-slate-500 font-mono mt-1">
                {"["}{nsm?.ci_low?.toFixed(2)}{", "}{nsm?.ci_high?.toFixed(2)}{"]"}
                <span className="ml-2 font-sans font-medium text-slate-400">
                  {"(n="}{nsm?.n}{")"}
                </span>
              </div>
            </div>
          )}
        </div>

        {/* NS-G Headline Tile */}
        <div className="flex flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold uppercase tracking-wider text-slate-500">
              {"NS-G Grade macro-F1"}
            </span>
            <span
              className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
                !isGateValid
                  ? "bg-rose-100 text-rose-800"
                  : nsg?.status === "final"
                  ? "bg-emerald-100 text-emerald-800"
                  : "bg-amber-100 text-amber-800"
              }`}
            >
              {!isGateValid ? "invalid" : nsg?.status}
            </span>
          </div>

          {!isGateValid ? (
            <div className="my-2">
              <span className="text-xl font-bold text-rose-600 uppercase">
                {"invalid"}
              </span>
              <p className="text-[11px] text-slate-400 mt-1">
                {"Measurement gate failed"}
              </p>
            </div>
          ) : (
            <div className="my-1">
              <span className="text-3xl font-black text-slate-900 tracking-tight">
                {nsg && nsg.value !== null ? nsg.value.toFixed(2) : "—"}
              </span>
              <div className="text-xs text-slate-500 font-mono mt-1">
                {"["}{nsg?.ci_low?.toFixed(2)}{", "}{nsg?.ci_high?.toFixed(2)}{"]"}
                <span className="ml-2 font-sans font-medium text-slate-400">
                  {"(n="}{nsg?.n}{")"}
                </span>
              </div>
            </div>
          )}
        </div>

        {/* Supporting Headline: F1_high and macroF1_LM */}
        <div className="flex flex-col justify-between rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs">
          <div>
            <span className="text-[11px] font-semibold uppercase text-slate-400 block mb-1">
              {"Band Metrics"}
            </span>
            <div className="flex justify-between items-center py-1 border-b border-slate-100">
              <span className="text-slate-600">{"F1_high (Band 8-9)"}</span>
              <strong className="text-slate-900 font-mono">
                {metrics.stages.s5?.f1_high.value?.toFixed(2) ?? "—"}
              </strong>
            </div>
            <div className="flex justify-between items-center py-1">
              <span className="text-slate-600">{"macroF1_LM (Band 3-6)"}</span>
              <strong className="text-slate-900 font-mono">
                {metrics.stages.s5?.macro_f1_lm.value?.toFixed(2) ?? "—"}
              </strong>
            </div>
          </div>
          <span className="text-[10px] text-slate-400 italic">
            {"Pathologist requested extreme/concordance bands"}
          </span>
        </div>

        {/* Supporting Headline: QWK and Sum MAE */}
        <div className="flex flex-col justify-between rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs">
          <div>
            <span className="text-[11px] font-semibold uppercase text-slate-400 block mb-1">
              {"Agreement & Error"}
            </span>
            <div className="flex justify-between items-center py-1 border-b border-slate-100">
              <span className="text-slate-600">{"QWK (Ordinal κ)"}</span>
              <strong className="text-slate-900 font-mono">
                {metrics.stages.s5?.qwk.value?.toFixed(2) ?? "—"}
              </strong>
            </div>
            <div className="flex justify-between items-center py-1">
              <span className="text-slate-600">{"Sum MAE (Points)"}</span>
              <strong className="text-slate-900 font-mono">
                {metrics.stages.s5?.sum_mae.value?.toFixed(2) ?? "—"}
              </strong>
            </div>
          </div>
          <span className="text-[10px] text-slate-400 italic">
            {"Standard Nottingham sum absolute deviation"}
          </span>
        </div>
      </div>

      {/* Tabs Bar */}
      <div className="flex border-b border-slate-200 bg-white px-4 pt-2 rounded-t-xl overflow-x-auto">
        {(
          [
            { id: "stages", label: L.heading.stages },
            { id: "confusion", label: L.heading.confusion },
            { id: "curves", label: L.heading.mitosisCurves },
            { id: "calibration", label: L.heading.calibration },
            { id: "slices", label: L.heading.slices },
            { id: "items", label: L.heading.items },
            { id: "errors", label: L.heading.errors },
            { id: "cost", label: L.heading.costAndLatency },
          ] as const
        ).map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`pb-3 px-4 text-xs font-semibold transition border-b-2 whitespace-nowrap ${
              activeTab === tab.id
                ? "border-indigo-600 text-indigo-600"
                : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab Panels */}
      {activeTab === "stages" && <StagesTab metrics={metrics} />}
      {activeTab === "confusion" && (
        <ConfusionTab
          metrics={metrics}
          onSelectCell={(_comp, _t, _p) => {
            setActiveTab("items");
          }}
        />
      )}
      {activeTab === "curves" && <MitosisCurvesTab runId={runId} metrics={metrics} />}
      {activeTab === "calibration" && <CalibrationTab metrics={metrics} />}
      {activeTab === "slices" && <SlicesTab slices={metrics.slices} />}
      {activeTab === "items" && <ItemsTab runId={runId} items={items} />}
      {activeTab === "errors" && (
        <ErrorsTab
          runId={runId}
          errors={errors}
          onLogIssue={handleLogIssue}
        />
      )}
      {activeTab === "cost" && <CostTab metrics={metrics} />}

      {/* Log Issue Modal */}
      {issueModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4">
          <div className="flex w-full max-w-md flex-col rounded-2xl bg-white p-6 shadow-xl border border-slate-200">
            <h3 className="text-sm font-bold text-slate-900 mb-4 pb-2 border-b border-slate-100">
              {L.action.logIssue}
            </h3>

            <div className="flex flex-col gap-3 text-xs mb-4">
              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{"Title"}</label>
                <input
                  type="text"
                  value={issueTitle}
                  onChange={(e) => setIssueTitle(e.target.value)}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800"
                />
              </div>

              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{L.field.category}</label>
                <select
                  value={issueCategory}
                  onChange={(e) => setIssueCategory(e.target.value as any)}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800"
                >
                  <option value="biological">{"biological"}</option>
                  <option value="model">{"model"}</option>
                  <option value="staging">{"staging"}</option>
                  <option value="technical">{"technical"}</option>
                </select>
                <span className="text-[10px] text-slate-400">
                  {L.help.categoryRemedyHelp}
                </span>
              </div>

              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{L.field.severity}</label>
                <select
                  value={issueSeverity}
                  onChange={(e) => setIssueSeverity(e.target.value as any)}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800"
                >
                  <option value="critical">{"critical"}</option>
                  <option value="high">{"high"}</option>
                  <option value="medium">{"medium"}</option>
                  <option value="low">{"low"}</option>
                </select>
              </div>
            </div>

            <div className="flex items-center justify-end gap-2 pt-3 border-t border-slate-100 text-xs">
              <button
                onClick={() => setIssueModalOpen(false)}
                className="rounded-lg px-3 py-1.5 font-semibold text-slate-600 hover:bg-slate-100"
              >
                {L.action.cancel}
              </button>
              <button
                onClick={handleCreateIssueSubmit}
                className="rounded-lg bg-indigo-600 px-4 py-1.5 font-semibold text-white hover:bg-indigo-700"
              >
                {L.action.save}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
