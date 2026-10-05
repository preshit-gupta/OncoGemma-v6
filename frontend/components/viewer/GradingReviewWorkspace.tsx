"use client";

import React, { useState, useEffect } from "react";
import {
  GradingStageV6,
  TubuleSample,
  PleoField,
  getGrading,
  reviewSample,
  overrideComponent,
  confirmHistotype,
  confirmGrading,
} from "@/lib/api/grading";
import { L } from "@/lib/labels";
import { Provenance } from "@/components/Provenance";

interface GradingReviewWorkspaceProps {
  caseId: string;
  onReopenMitosis?: () => void;
  onRefreshCase?: () => void;
}

export const HISTOLOGIC_TYPES = [
  "IDC-NST",
  "ILC",
  "Mixed Ductal and Lobular",
  "Mucinous Carcinoma",
  "Tubular Carcinoma",
  "Cribriform Carcinoma",
  "Papillary Carcinoma",
  "Micropapillary Carcinoma",
  "Metaplastic Carcinoma",
  "Apocrine Carcinoma",
  "Other",
] as const;

export function GradingReviewWorkspace({
  caseId,
  onReopenMitosis,
  onRefreshCase,
}: GradingReviewWorkspaceProps) {
  const [data, setData] = useState<GradingStageV6 | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<boolean>(false);
  const [activeTab, setActiveTab] = useState<"tubule" | "pleo">("tubule");

  // Selected histotype in dropdown
  const [selectedHistotype, setSelectedHistotype] = useState<string>("");

  // Override modal state
  const [overrideModalComponent, setOverrideModalComponent] = useState<
    "tubule" | "pleo" | "histotype" | null
  >(null);
  const [overrideScore, setOverrideScore] = useState<1 | 2 | 3>(2);
  const [overrideHistotypeVal, setOverrideHistotypeVal] = useState<string>("IDC-NST");
  const [overrideReason, setOverrideReason] = useState<string>("");
  const [overrideError, setOverrideError] = useState<string | null>(null);

  // Tubule review state per sample
  const [editingTubuleId, setEditingTubuleId] = useState<string | null>(null);
  const [tubuleTumorPresent, setTubuleTumorPresent] = useState<boolean>(true);
  const [tubulePercentVal, setTubulePercentVal] = useState<number>(30);

  // Pleo review state per field
  const [editingPleoId, setEditingPleoId] = useState<string | null>(null);
  const [pleoScoreVal, setPleoScoreVal] = useState<1 | 2 | 3>(2);

  // Case confirmation status
  const [confirmSuccess, setConfirmSuccess] = useState<boolean>(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    getGrading(caseId)
      .then((res) => {
        if (!cancelled) {
          setData(res);
          setSelectedHistotype(res.histotype.type || "IDC-NST");
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
  }, [caseId]);

  const handleReviewTubule = async (sample: TubuleSample) => {
    setActionLoading(true);
    setError(null);
    try {
      const updated = await reviewSample({
        case_id: caseId,
        kind: "tubule",
        sample_id: sample.id,
        value: {
          tumor_present: tubuleTumorPresent,
          tubule_percent: tubuleTumorPresent ? tubulePercentVal : 0,
        },
      });
      setData(updated);
      setEditingTubuleId(null);
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setActionLoading(false);
    }
  };

  const handleReviewPleo = async (field: PleoField) => {
    setActionLoading(true);
    setError(null);
    try {
      const updated = await reviewSample({
        case_id: caseId,
        kind: "pleo",
        sample_id: field.id,
        value: {
          pleomorphism_score: pleoScoreVal,
        },
      });
      setData(updated);
      setEditingPleoId(null);
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setActionLoading(false);
    }
  };

  const handleConfirmHistotype = async () => {
    if (!selectedHistotype) return;
    setActionLoading(true);
    setError(null);
    try {
      const updated = await confirmHistotype({
        case_id: caseId,
        type: selectedHistotype,
      });
      setData(updated);
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setActionLoading(false);
    }
  };

  const openOverrideModal = (component: "tubule" | "pleo" | "histotype") => {
    setOverrideModalComponent(component);
    setOverrideReason(data?.overrides.reasons[component] || "");
    setOverrideError(null);
    if (component === "tubule") {
      setOverrideScore(data?.overrides.tubule_score ?? data?.tubule.score ?? 2);
    } else if (component === "pleo") {
      setOverrideScore(data?.overrides.pleo_score ?? data?.pleomorphism.score ?? 2);
    } else {
      setOverrideHistotypeVal(data?.overrides.histotype ?? data?.histotype.type ?? "IDC-NST");
    }
  };

  const handleApplyOverride = async () => {
    if (!overrideModalComponent) return;
    if (overrideReason.trim().length < 10) {
      setOverrideError(L.error.reasonTooShort);
      return;
    }

    setActionLoading(true);
    setOverrideError(null);
    try {
      const val =
        overrideModalComponent === "histotype"
          ? overrideHistotypeVal
          : overrideScore;
      const updated = await overrideComponent({
        case_id: caseId,
        component: overrideModalComponent,
        value: val,
        reason: overrideReason.trim(),
      });
      setData(updated);
      setOverrideModalComponent(null);
    } catch (err: any) {
      setOverrideError(err.message || String(err));
    } finally {
      setActionLoading(false);
    }
  };

  const handleClearOverride = async () => {
    if (!overrideModalComponent) return;
    setActionLoading(true);
    setOverrideError(null);
    try {
      const updated = await overrideComponent({
        case_id: caseId,
        component: overrideModalComponent,
        value: null,
        reason: "",
      });
      setData(updated);
      setOverrideModalComponent(null);
    } catch (err: any) {
      setOverrideError(err.message || String(err));
    } finally {
      setActionLoading(false);
    }
  };

  const handleConfirmGrading = async () => {
    setActionLoading(true);
    setError(null);
    try {
      await confirmGrading({ case_id: caseId });
      setConfirmSuccess(true);
      if (data) {
        setData({ ...data, status: "confirmed" });
      }
      onRefreshCase?.();
    } catch (err: any) {
      if (err.data?.error === "histotype_unconfirmed" || err.message === "histotype_unconfirmed") {
        setError(L.error.histotypeUnconfirmed);
      } else if (err.data?.error === "missing_component" || err.message === "missing_component") {
        setError(L.error.missingComponent);
      } else if (err.data?.error === "not_awaiting_review" || err.message === "not_awaiting_review") {
        setError(L.error.notAwaitingReview);
      } else {
        setError(err.message || String(err));
      }
    } finally {
      setActionLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex h-96 items-center justify-center">
        <div className="text-sm font-medium text-slate-500 animate-pulse">
          {L.status.grading}
        </div>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div className="p-6">
        <div className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
          {error}
        </div>
      </div>
    );
  }

  if (!data) return null;

  const effTubuleScore = data.overrides.tubule_score ?? data.tubule.score;
  const effPleoScore = data.overrides.pleo_score ?? data.pleomorphism.score;
  const effMitoticScore = data.mitotic.score;
  const isTubuleOverridden = data.overrides.tubule_score !== undefined;
  const isPleoOverridden = data.overrides.pleo_score !== undefined;
  const isHistotypeOverridden = data.overrides.histotype !== undefined;

  return (
    <div className="flex h-full flex-col gap-6 overflow-y-auto p-6">
      {/* Header Bar */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <div className="text-xs font-semibold text-slate-400 uppercase tracking-wider">
              {L.heading.gradingReview}
            </div>
            <div className="flex items-center gap-2 mt-0.5">
              <span className="text-lg font-bold text-slate-900">
                {L.field.caseId}{": "}{data.case_id}
              </span>
              <span
                className={`rounded px-2 py-0.5 text-xs font-medium ${
                  data.status === "confirmed"
                    ? "bg-emerald-100 text-emerald-800"
                    : "bg-amber-100 text-amber-800"
                }`}
              >
                {data.status === "confirmed" ? L.status.confirmed : L.status.awaitingReview}
              </span>
            </div>
          </div>

          <div className="h-8 w-px bg-slate-200" />

          {/* Grade and Total Display */}
          <div className="flex items-center gap-3">
            <div className="flex flex-col">
              <span className="text-xs text-slate-400 font-medium">{L.field.grade}</span>
              <span className="text-2xl font-black text-slate-900">
                {data.grade !== null ? data.grade : "—"}
              </span>
            </div>
            <div className="flex flex-col">
              <span className="text-xs text-slate-400 font-medium">{L.field.totalScore}</span>
              <span className="text-2xl font-bold text-slate-700">
                {data.total !== null ? data.total : "—"}
              </span>
            </div>
          </div>

          <div className="h-8 w-px bg-slate-200" />

          {/* Component Chips (T, P, M) */}
          <div className="flex items-center gap-2">
            {/* Tubule Chip */}
            <div
              className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition cursor-pointer ${
                isTubuleOverridden
                  ? "border-purple-300 bg-purple-50 text-purple-900"
                  : "border-slate-200 bg-slate-50 text-slate-800 hover:bg-slate-100"
              }`}
              title={data.tubule.estimator}
              onClick={() => openOverrideModal("tubule")}
            >
              <span className="font-bold">{"T:"}</span>
              <span>{effTubuleScore !== null ? effTubuleScore : "—"}</span>
              {data.tubule.percent !== null && (
                <span className="text-slate-400">
                  {"(" + data.tubule.percent + L.unit.percent + ")"}
                </span>
              )}
              {isTubuleOverridden && (
                <span className="rounded bg-purple-200 px-1 py-0.2 text-[10px] font-semibold text-purple-800">
                  {L.field.overridden}
                </span>
              )}
            </div>

            {/* Pleomorphism Chip */}
            <div
              className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition cursor-pointer ${
                isPleoOverridden
                  ? "border-purple-300 bg-purple-50 text-purple-900"
                  : "border-slate-200 bg-slate-50 text-slate-800 hover:bg-slate-100"
              }`}
              title={data.pleomorphism.estimator}
              onClick={() => openOverrideModal("pleo")}
            >
              <span className="font-bold">{"P:"}</span>
              <span>{effPleoScore !== null ? effPleoScore : "—"}</span>
              {isPleoOverridden && (
                <span className="rounded bg-purple-200 px-1 py-0.2 text-[10px] font-semibold text-purple-800">
                  {L.field.overridden}
                </span>
              )}
            </div>

            {/* Mitotic Chip */}
            <div
              className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-slate-50 px-3 py-1.5 text-xs font-medium text-slate-800 transition hover:bg-slate-100 cursor-pointer"
              title={`${data.mitotic.per_mm2} / mm²`}
              onClick={onReopenMitosis}
            >
              <span className="font-bold">{"M:"}</span>
              <span>{effMitoticScore !== null ? effMitoticScore : "—"}</span>
              <span className="text-slate-400">
                {"(" + data.mitotic.per_mm2 + " / " + L.unit.mm2 + ")"}
              </span>
            </div>
          </div>
        </div>

        {/* Action Controls & Provenance */}
        <div className="flex items-center gap-3">
          <Provenance
            model_versions={data.provenance.model_versions}
            config_hash={data.provenance.config_hash}
            run_mode={data.provenance.run_mode}
          />

          <button
            onClick={handleConfirmGrading}
            disabled={actionLoading || data.status === "confirmed"}
            className={`rounded-lg px-4 py-2 text-xs font-semibold shadow-sm transition ${
              data.status === "confirmed"
                ? "bg-slate-100 text-slate-400 cursor-not-allowed"
                : "bg-emerald-600 text-white hover:bg-emerald-700 active:scale-95"
            }`}
          >
            {data.status === "confirmed" ? L.status.confirmed : L.action.confirmGrade}
          </button>
        </div>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 p-4 text-xs font-medium text-rose-700">
          {error}
        </div>
      )}

      {/* Boundary and Human Review Banners */}
      {data.flags.includes("near_grade_boundary") && (
        <div className="flex items-center gap-3 rounded-lg border border-amber-300 bg-amber-50 p-4 text-amber-900 shadow-sm">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-amber-200 font-bold text-amber-900">
            {"!"}
          </div>
          <div className="flex flex-col">
            <span className="text-xs font-bold">{L.field.nearBoundary}</span>
            <span className="text-xs text-amber-800">{L.help.nearGradeBoundary}</span>
          </div>
        </div>
      )}

      {data.flags.includes("needs_human") && (
        <div className="flex items-center gap-3 rounded-lg border border-blue-200 bg-blue-50 p-4 text-blue-900 shadow-sm">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-blue-200 font-bold text-blue-900">
            {"i"}
          </div>
          <div className="flex flex-col">
            <span className="text-xs font-bold">{L.status.needsHuman}</span>
            <span className="text-xs text-blue-800">{L.help.needsHumanGrading}</span>
          </div>
        </div>
      )}

      {data.flags.includes("insufficient_nuclei") && (
        <div className="flex items-center gap-3 rounded-lg border border-rose-200 bg-rose-50 p-4 text-rose-900 shadow-sm">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-rose-200 font-bold text-rose-900">
            {"!"}
          </div>
          <div className="flex flex-col">
            <span className="text-xs font-bold">{L.status.failed}</span>
            <span className="text-xs text-rose-800">{L.help.insufficientNuclei}</span>
          </div>
        </div>
      )}

      {confirmSuccess && (
        <div className="flex items-center gap-3 rounded-lg border border-emerald-300 bg-emerald-50 p-4 text-emerald-900 shadow-sm">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-200 font-bold text-emerald-900">
            {"✓"}
          </div>
          <div className="flex flex-col">
            <span className="text-xs font-bold">{L.heading.caseComplete}</span>
            <span className="text-xs text-emerald-800">{L.help.gradingConfirmed}</span>
          </div>
        </div>
      )}

      {/* Main Grid: Left Tabs (Tubules / Pleo), Right Sidebar (Histotype & Mitoses) */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column: Sample Grids */}
        <div className="lg:col-span-2 flex flex-col gap-4">
          {/* Tab Navigation */}
          <div className="flex border-b border-slate-200 bg-white px-4 pt-2 rounded-t-xl">
            <button
              onClick={() => setActiveTab("tubule")}
              className={`pb-3 px-4 text-xs font-semibold transition border-b-2 ${
                activeTab === "tubule"
                  ? "border-indigo-600 text-indigo-600"
                  : "border-transparent text-slate-500 hover:text-slate-800"
              }`}
            >
              {L.heading.tubuleFormation}
              {" ("}
              {data.tubule.samples.length}
              {")"}
            </button>
            <button
              onClick={() => setActiveTab("pleo")}
              className={`pb-3 px-4 text-xs font-semibold transition border-b-2 ${
                activeTab === "pleo"
                  ? "border-indigo-600 text-indigo-600"
                  : "border-transparent text-slate-500 hover:text-slate-800"
              }`}
            >
              {L.heading.nuclearPleomorphism}
              {" ("}
              {data.pleomorphism.fields.length}
              {")"}
            </button>
          </div>

          {/* Tubule Tab Content */}
          {activeTab === "tubule" && (
            <div className="flex flex-col gap-4">
              <div className="flex items-center justify-between text-xs text-slate-500 px-1">
                <span>
                  {L.field.samplesUsed}{": "}
                  <strong className="text-slate-800">{data.tubule.n_used}</strong>
                  {" / "}
                  {data.tubule.samples.length}
                </span>
                <span>
                  {L.field.estimator}{": "}
                  <code className="text-slate-700 bg-slate-100 px-1.5 py-0.5 rounded">
                    {data.tubule.estimator}
                  </code>
                </span>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {data.tubule.samples.map((sample) => {
                  const isEditing = editingTubuleId === sample.id;
                  const isFailed = sample.estimate === null;
                  const hasReview = sample.review !== null;
                  const currentTumor = hasReview
                    ? sample.review?.tumor_present ?? false
                    : sample.estimate?.tumor_present ?? false;
                  const currentPercent = hasReview
                    ? sample.review?.tubule_percent ?? 0
                    : sample.estimate?.tubule_percent ?? 0;

                  return (
                    <div
                      key={sample.id}
                      className={`flex flex-col rounded-xl border bg-white p-4 shadow-sm transition ${
                        isFailed && !hasReview
                          ? "border-rose-300 ring-1 ring-rose-200"
                          : "border-slate-200"
                      }`}
                    >
                      <div className="flex items-center justify-between mb-3">
                        <div className="flex items-center gap-2">
                          <span className="font-bold text-xs text-slate-900">{sample.id}</span>
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                            {"#" + sample.stratum}
                          </span>
                        </div>
                        {isFailed && !hasReview ? (
                          <span className="rounded bg-rose-100 px-2 py-0.5 text-[10px] font-bold text-rose-700">
                            {L.status.needsHuman}
                          </span>
                        ) : hasReview ? (
                          <span className="rounded bg-indigo-100 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
                            {L.status.approved}
                          </span>
                        ) : (
                          <span className="rounded bg-slate-100 px-2 py-0.5 text-[10px] text-slate-600">
                            {L.status.done}
                          </span>
                        )}
                      </div>

                      {/* Image Thumbnail */}
                      <div className="relative aspect-square w-full overflow-hidden rounded-lg border border-slate-100 bg-slate-50 mb-3">
                        <img
                          src={sample.image_url}
                          alt={sample.id}
                          className="h-full w-full object-cover"
                        />
                        <div className="absolute bottom-2 left-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                          {L.unit.mag10x}{" · 512 "}{L.unit.um}
                        </div>
                      </div>

                      {/* Sample Info */}
                      <div className="flex flex-col gap-1 text-xs text-slate-600 mb-3">
                        <div className="flex justify-between">
                          <span>{L.field.tumorArea}</span>
                          <span className="font-semibold text-slate-800">
                            {(sample.tumor_area_um2 / 1000).toFixed(0)}
                            {"k "}{L.unit.um2}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span>{L.field.tumorPresent}</span>
                          <span className="font-semibold text-slate-800">
                            {isFailed && !hasReview
                              ? "—"
                              : currentTumor
                              ? L.field.tumorPresent
                              : "No"}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span>{L.field.tubulePercent}</span>
                          <span className="font-semibold text-slate-800">
                            {isFailed && !hasReview ? "—" : `${currentPercent}%`}
                          </span>
                        </div>
                      </div>

                      {/* Review Section */}
                      {isEditing ? (
                        <div className="mt-auto flex flex-col gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs">
                          <label className="flex items-center gap-2 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={tubuleTumorPresent}
                              onChange={(e) => setTubuleTumorPresent(e.target.checked)}
                              className="rounded border-slate-300 text-indigo-600 focus:ring-indigo-500"
                            />
                            <span className="font-medium text-slate-800">
                              {L.field.tumorPresent}
                            </span>
                          </label>

                          {tubuleTumorPresent && (
                            <div className="flex flex-col gap-1">
                              <div className="flex justify-between text-slate-600 font-medium">
                                <span>{L.field.tubulePercent}</span>
                                <span>{tubulePercentVal}{L.unit.percent}</span>
                              </div>
                              <input
                                type="range"
                                min={0}
                                max={100}
                                value={tubulePercentVal}
                                onChange={(e) => setTubulePercentVal(Number(e.target.value))}
                                className="w-full accent-indigo-600"
                              />
                            </div>
                          )}

                          <div className="flex items-center justify-end gap-2 pt-1">
                            <button
                              onClick={() => setEditingTubuleId(null)}
                              className="rounded px-2.5 py-1 text-slate-600 hover:bg-slate-200 transition"
                            >
                              {L.action.cancel}
                            </button>
                            <button
                              onClick={() => handleReviewTubule(sample)}
                              disabled={actionLoading}
                              className="rounded bg-indigo-600 px-3 py-1 font-semibold text-white hover:bg-indigo-700 transition"
                            >
                              {L.action.save}
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="mt-auto pt-2 border-t border-slate-100 flex items-center justify-end">
                          <button
                            onClick={() => {
                              setEditingTubuleId(sample.id);
                              setTubuleTumorPresent(currentTumor);
                              setTubulePercentVal(currentPercent);
                            }}
                            className="rounded px-2.5 py-1 text-xs font-semibold text-indigo-600 hover:bg-indigo-50 transition"
                          >
                            {L.action.reviewSample}
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Pleomorphism Tab Content */}
          {activeTab === "pleo" && (
            <div className="flex flex-col gap-4">
              <div className="flex items-center justify-between text-xs text-slate-500 px-1">
                <span>
                  {L.field.aggregation}{": "}
                  <strong className="text-slate-800">{data.pleomorphism.aggregation}</strong>
                </span>
                <span>
                  {L.field.estimator}{": "}
                  <code className="text-slate-700 bg-slate-100 px-1.5 py-0.5 rounded">
                    {data.pleomorphism.estimator}
                  </code>
                </span>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {data.pleomorphism.fields.map((field) => {
                  const isEditing = editingPleoId === field.id;
                  const isFailed = field.estimate === null;
                  const hasReview = field.review !== null;
                  const currentScore = hasReview
                    ? field.review?.pleomorphism_score ?? 2
                    : field.estimate?.pleomorphism_score ?? 2;

                  return (
                    <div
                      key={field.id}
                      className={`flex flex-col rounded-xl border bg-white p-4 shadow-sm transition ${
                        isFailed && !hasReview
                          ? "border-rose-300 ring-1 ring-rose-200"
                          : "border-slate-200"
                      }`}
                    >
                      <div className="flex items-center justify-between mb-3">
                        <div className="flex items-center gap-2">
                          <span className="font-bold text-xs text-slate-900">{field.id}</span>
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                            {"#" + field.stratum}
                          </span>
                        </div>
                        {isFailed && !hasReview ? (
                          <span className="rounded bg-rose-100 px-2 py-0.5 text-[10px] font-bold text-rose-700">
                            {L.status.needsHuman}
                          </span>
                        ) : hasReview ? (
                          <span className="rounded bg-indigo-100 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
                            {L.status.approved}
                          </span>
                        ) : (
                          <span className="rounded bg-slate-100 px-2 py-0.5 text-[10px] text-slate-600">
                            {L.status.done}
                          </span>
                        )}
                      </div>

                      {/* Image Thumbnail */}
                      <div className="relative aspect-square w-full overflow-hidden rounded-lg border border-slate-100 bg-slate-50 mb-3">
                        <img
                          src={field.image_url}
                          alt={field.id}
                          className="h-full w-full object-cover"
                        />
                        <div className="absolute bottom-2 left-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                          {L.unit.mag40x}{" · 128 "}{L.unit.um}
                        </div>
                      </div>

                      {/* Nuclei Info */}
                      <div className="flex flex-col gap-1 text-xs text-slate-600 mb-3">
                        <div className="flex justify-between">
                          <span>{L.field.pleoScore}</span>
                          <span className="font-bold text-slate-900">
                            {isFailed && !hasReview ? "—" : currentScore}
                          </span>
                        </div>
                        {field.nuclei ? (
                          <>
                            <div className="flex justify-between">
                              <span>{L.field.nucleiCount}</span>
                              <span className="font-semibold text-slate-800">
                                {field.nuclei.n}
                              </span>
                            </div>
                            <div className="flex justify-between">
                              <span>{L.field.medianArea}</span>
                              <span className="font-semibold text-slate-800">
                                {field.nuclei.area_p50_um2.toFixed(1)}
                                {" "}{L.unit.um2}
                              </span>
                            </div>
                            <div className="flex justify-between">
                              <span>{L.field.areaCv}</span>
                              <span className="font-semibold text-slate-800">
                                {field.nuclei.area_cv.toFixed(2)}
                              </span>
                            </div>
                          </>
                        ) : (
                          <div className="text-slate-400 italic">
                            {L.status.pending}
                          </div>
                        )}
                      </div>

                      {/* Review Section */}
                      {isEditing ? (
                        <div className="mt-auto flex flex-col gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-xs">
                          <div className="flex items-center justify-between">
                            <span className="font-medium text-slate-800">{L.field.pleoScore}</span>
                            <div className="flex gap-2">
                              {([1, 2, 3] as const).map((s) => (
                                <button
                                  key={s}
                                  onClick={() => setPleoScoreVal(s)}
                                  className={`h-7 w-7 rounded font-bold text-xs transition ${
                                    pleoScoreVal === s
                                      ? "bg-indigo-600 text-white"
                                      : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-100"
                                  }`}
                                >
                                  {s}
                                </button>
                              ))}
                            </div>
                          </div>

                          <div className="flex items-center justify-end gap-2 pt-1">
                            <button
                              onClick={() => setEditingPleoId(null)}
                              className="rounded px-2.5 py-1 text-slate-600 hover:bg-slate-200 transition"
                            >
                              {L.action.cancel}
                            </button>
                            <button
                              onClick={() => handleReviewPleo(field)}
                              disabled={actionLoading}
                              className="rounded bg-indigo-600 px-3 py-1 font-semibold text-white hover:bg-indigo-700 transition"
                            >
                              {L.action.save}
                            </button>
                          </div>
                        </div>
                      ) : (
                        <div className="mt-auto pt-2 border-t border-slate-100 flex items-center justify-end">
                          <button
                            onClick={() => {
                              setEditingPleoId(field.id);
                              setPleoScoreVal(currentScore);
                            }}
                            className="rounded px-2.5 py-1 text-xs font-semibold text-indigo-600 hover:bg-indigo-50 transition"
                          >
                            {L.action.reviewSample}
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* Right Column: Histologic Type Card & Mitoses Panel */}
        <div className="flex flex-col gap-6">
          {/* Histologic Type Card */}
          <div className="flex flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <span className="text-xs font-bold uppercase tracking-wider text-slate-500">
                {L.heading.histologicType}
              </span>
              {data.histotype.confirmed ? (
                <span className="rounded bg-emerald-100 px-2 py-0.5 text-[10px] font-semibold text-emerald-800">
                  {L.status.confirmed}
                </span>
              ) : (
                <span className="rounded bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                  {L.status.awaitingReview}
                </span>
              )}
            </div>

            <div className="flex flex-col gap-3 text-xs text-slate-700 mb-4">
              <div>
                <span className="text-slate-400 font-medium block mb-1">
                  {L.field.proposedType}
                </span>
                <span className="font-bold text-sm text-slate-900">
                  {data.histotype.type || "—"}
                </span>
              </div>

              <div>
                <span className="text-slate-400 font-medium block mb-1">
                  {L.field.rationale}
                </span>
                <p className="rounded-lg bg-slate-50 p-2.5 text-slate-600 border border-slate-100 leading-relaxed">
                  {data.histotype.rationale || "—"}
                </p>
              </div>

              <div>
                <span className="text-slate-400 font-medium block mb-1">
                  {L.field.estimator}
                </span>
                <code className="text-slate-600 bg-slate-100 px-1.5 py-0.5 rounded text-[11px]">
                  {data.histotype.estimator}
                </code>
              </div>
            </div>

            {/* Selection & Confirmation */}
            <div className="flex flex-col gap-2 pt-3 border-t border-slate-100">
              <label className="text-xs text-slate-500 font-medium">
                {L.field.specimenType}
              </label>
              <select
                value={selectedHistotype}
                onChange={(e) => setSelectedHistotype(e.target.value)}
                className="rounded-lg border border-slate-300 p-2 text-xs font-medium text-slate-800 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
              >
                {HISTOLOGIC_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>

              <div className="flex items-center gap-2 mt-2">
                <button
                  onClick={handleConfirmHistotype}
                  disabled={actionLoading || data.histotype.confirmed}
                  className={`flex-1 rounded-lg px-3 py-2 text-xs font-semibold shadow-sm transition ${
                    data.histotype.confirmed
                      ? "bg-slate-100 text-slate-400 cursor-not-allowed"
                      : "bg-indigo-600 text-white hover:bg-indigo-700"
                  }`}
                >
                  {L.action.confirmType}
                </button>
                <button
                  onClick={() => openOverrideModal("histotype")}
                  className="rounded-lg border border-slate-300 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition"
                >
                  {L.action.override}
                </button>
              </div>
            </div>
          </div>

          {/* Mitoses Panel (Read-only from Stage 4) */}
          <div className="flex flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <span className="text-xs font-bold uppercase tracking-wider text-slate-500">
                {L.heading.mitoticSummary}
              </span>
              <span className="rounded bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-600">
                {L.stage.mitosis}
              </span>
            </div>

            <div className="grid grid-cols-2 gap-3 text-xs mb-4">
              <div className="rounded-lg bg-slate-50 p-2.5 border border-slate-100">
                <span className="text-slate-400 font-medium block">
                  {L.field.mitosisScore}
                </span>
                <span className="text-lg font-bold text-slate-900">
                  {data.mitotic.score !== null ? data.mitotic.score : "—"}
                </span>
              </div>
              <div className="rounded-lg bg-slate-50 p-2.5 border border-slate-100">
                <span className="text-slate-400 font-medium block">
                  {L.field.mitosisCount}
                </span>
                <span className="text-lg font-bold text-slate-900">
                  {data.mitotic.count_total}
                </span>
              </div>
              <div className="rounded-lg bg-slate-50 p-2.5 border border-slate-100">
                <span className="text-slate-400 font-medium block">
                  {L.heading.topHpfs}
                </span>
                <span className="text-lg font-bold text-slate-900">
                  {data.mitotic.n_hpf}
                </span>
              </div>
              <div className="rounded-lg bg-slate-50 p-2.5 border border-slate-100">
                <span className="text-slate-400 font-medium block">
                  {L.field.density}
                </span>
                <span className="text-sm font-bold text-slate-900">
                  {data.mitotic.per_mm2}
                  {" / "}{L.unit.mm2}
                </span>
              </div>
            </div>

            <div className="pt-2 border-t border-slate-100">
              <button
                onClick={onReopenMitosis}
                className="w-full rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2 text-xs font-semibold text-indigo-700 hover:bg-indigo-100 transition"
              >
                {L.action.viewMitoses}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Component Override Modal */}
      {overrideModalComponent && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-xs p-4">
          <div className="flex w-full max-w-md flex-col rounded-2xl bg-white p-6 shadow-xl border border-slate-200 animate-in fade-in zoom-in-95 duration-150">
            <div className="flex items-center justify-between mb-4 border-b border-slate-100 pb-3">
              <h3 className="text-sm font-bold text-slate-900">
                {L.heading.overrideComponent}
                {": "}
                <span className="text-indigo-600 capitalize">
                  {overrideModalComponent}
                </span>
              </h3>
              <button
                onClick={() => setOverrideModalComponent(null)}
                className="text-slate-400 hover:text-slate-600 text-sm font-bold"
              >
                {"✕"}
              </button>
            </div>

            {overrideError && (
              <div className="mb-4 rounded-lg bg-rose-50 p-3 text-xs text-rose-700 font-medium">
                {overrideError}
              </div>
            )}

            <div className="flex flex-col gap-4 text-xs">
              {/* Value Selector */}
              {overrideModalComponent === "histotype" ? (
                <div className="flex flex-col gap-1.5">
                  <label className="font-semibold text-slate-700">
                    {L.heading.histologicType}
                  </label>
                  <select
                    value={overrideHistotypeVal}
                    onChange={(e) => setOverrideHistotypeVal(e.target.value)}
                    className="rounded-lg border border-slate-300 p-2 text-xs font-medium text-slate-800 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
                  >
                    {HISTOLOGIC_TYPES.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  <label className="font-semibold text-slate-700">
                    {L.field.grade}
                  </label>
                  <div className="flex gap-3">
                    {([1, 2, 3] as const).map((s) => (
                      <button
                        key={s}
                        onClick={() => setOverrideScore(s)}
                        className={`flex-1 rounded-lg py-2 text-xs font-bold transition ${
                          overrideScore === s
                            ? "bg-indigo-600 text-white shadow-sm"
                            : "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50"
                        }`}
                      >
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
              )}

              {/* Reason Textarea (>= 10 chars) */}
              <div className="flex flex-col gap-1.5">
                <div className="flex justify-between items-center">
                  <label className="font-semibold text-slate-700">
                    {L.field.overrideReason}
                  </label>
                  <span
                    className={`text-[10px] ${
                      overrideReason.trim().length >= 10
                        ? "text-emerald-600 font-bold"
                        : "text-slate-400"
                    }`}
                  >
                    {overrideReason.trim().length}
                    {" / 10"}
                  </span>
                </div>
                <textarea
                  rows={3}
                  value={overrideReason}
                  onChange={(e) => setOverrideReason(e.target.value)}
                  className="rounded-lg border border-slate-300 p-2.5 text-xs text-slate-800 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
                />
                <span className="text-[10px] text-slate-400">
                  {L.help.reasonMinChars}
                </span>
              </div>
            </div>

            {/* Modal Actions */}
            <div className="flex items-center justify-between gap-3 pt-5 mt-4 border-t border-slate-100">
              {data.overrides.reasons[overrideModalComponent] ? (
                <button
                  onClick={handleClearOverride}
                  disabled={actionLoading}
                  className="rounded-lg border border-rose-300 px-3 py-1.5 text-xs font-semibold text-rose-700 hover:bg-rose-50 transition"
                >
                  {L.action.clearOverride}
                </button>
              ) : (
                <div />
              )}

              <div className="flex items-center gap-2">
                <button
                  onClick={() => setOverrideModalComponent(null)}
                  className="rounded-lg px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-100 transition"
                >
                  {L.action.cancel}
                </button>
                <button
                  onClick={handleApplyOverride}
                  disabled={actionLoading || overrideReason.trim().length < 10}
                  className={`rounded-lg px-4 py-1.5 text-xs font-semibold shadow-sm transition ${
                    overrideReason.trim().length < 10
                      ? "bg-slate-200 text-slate-400 cursor-not-allowed"
                      : "bg-indigo-600 text-white hover:bg-indigo-700"
                  }`}
                >
                  {L.action.applyOverride}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
