"use client";

import React, { useEffect, useState, useRef } from "react";
import Link from "next/link";
import { ArrowLeft, RefreshCcw, Info, X, Microscope, AlertTriangle, CheckCircle2, RotateCcw, Clock } from "lucide-react";
import { fetchCaseDetail, CaseDetail, retryStage, approveStage, confirmTriageStage, updateSlideMpp, updateCaseSpecimenType, SpecimenType } from "@/lib/api";
import { formatISTDateTime } from "@/lib/utils";
import dynamic from "next/dynamic";
import { StageRail } from "@/components/viewer/StageRail";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { Provenance } from "@/components/Provenance";
import { L } from "@/lib/labels";

const OpenSeadragonViewer = dynamic(
  () => import("@/components/viewer/OpenSeadragonViewer").then((mod) => mod.OpenSeadragonViewer),
  { ssr: false }
);

const TriageViewer = dynamic(
  () => import("@/components/viewer/TriageViewer").then((mod) => mod.TriageViewer),
  { ssr: false }
);

const MitosisViewer = dynamic(
  () => import("@/components/viewer/MitosisViewer").then((mod) => mod.MitosisViewer),
  { ssr: false }
);

const GradingReviewWorkspace = dynamic(
  () => import("@/components/viewer/GradingReviewWorkspace").then((mod) => mod.GradingReviewWorkspace),
  { ssr: false }
);

export default function CaseWorkspacePage({ params }: { params: { id: string } }) {
  const caseId = params.id;

  const [caseDetail, setCaseDetail] = useState<CaseDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeStage, setActiveStage] = useState<string>("ingest");
  const [showSlideDetails, setShowSlideDetails] = useState<boolean>(false);
  const [actionLoading, setActionLoading] = useState<boolean>(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [overrideModalOpen, setOverrideModalOpen] = useState<boolean>(false);
  const [overrideJustification, setOverrideJustification] = useState<string>("");

  const [hasUserNavigated, setHasUserNavigated] = useState<boolean>(false);

  const inFlightRef = useRef<boolean>(false);

  const loadData = async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const data = await fetchCaseDetail(caseId);
      setCaseDetail(data);
    } catch (err) {
      console.error(err);
    } finally {
      inFlightRef.current = false;
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 2500);
    return () => clearInterval(interval);
  }, [caseId]);

  const getLatestStage = (stages: any[] | undefined, name: string) => {
    if (!stages || !stages.length) return undefined;
    const matching = stages.filter((s) => s.stage === name);
    if (!matching.length) return undefined;
    return matching.reduce((prev, curr) => ((curr.attempt || 1) > (prev.attempt || 1) ? curr : prev), matching[0]);
  };

  // Controlled auto-advance: advances to highest active/ready stage
  useEffect(() => {
    if (!caseDetail?.stages) return;
    const stages = caseDetail.stages;

    const prepStage = getLatestStage(stages, "preprocess");
    const triageStage = getLatestStage(stages, "triage");
    const mitosisStage = getLatestStage(stages, "mitosis");
    const gradingStage = getLatestStage(stages, "grading");

    if (!hasUserNavigated) {
      if (gradingStage && (gradingStage.status === "running" || gradingStage.status === "done" || gradingStage.status === "confirmed" || gradingStage.status === "awaiting_review") && mitosisStage?.status === "confirmed") {
        setActiveStage("grading");
      } else if (mitosisStage && (mitosisStage.status === "running" || mitosisStage.status === "done" || mitosisStage.status === "confirmed" || mitosisStage.status === "awaiting_review") && triageStage?.status === "confirmed") {
        setActiveStage("mitosis");
      } else if (triageStage && (triageStage.status === "running" || triageStage.status === "done" || triageStage.status === "confirmed" || triageStage.status === "awaiting_review") && prepStage?.status === "confirmed") {
        setActiveStage("triage");
      } else if (prepStage && (prepStage.status === "running" || prepStage.status === "done" || prepStage.status === "confirmed" || prepStage.status === "awaiting_review" || prepStage.status === "queued")) {
        setActiveStage("preprocess");
      }
    }
  }, [caseDetail, hasUserNavigated]);

  const slide = caseDetail?.slides?.[0];
  const ingestStage = getLatestStage(caseDetail?.stages, "ingest");
  const preprocessStage = getLatestStage(caseDetail?.stages, "preprocess");
  const qcStage = getLatestStage(caseDetail?.stages, "qc");
  const triageStage = getLatestStage(caseDetail?.stages, "triage");
  const mitosisStage = getLatestStage(caseDetail?.stages, "mitosis");
  const gradingStage = getLatestStage(caseDetail?.stages, "grading");

  const hasSlide = Boolean(slide?.gcs_uri_original || slide?.id);
  const isIngestDone = ingestStage?.status === "completed" || ingestStage?.status === "done";
  const isIngestRunning = Boolean(hasSlide && ingestStage && (ingestStage.status === "running" || ingestStage.status === "queued"));
  const isIngestMissing = Boolean(!hasSlide || !ingestStage);
  const isIngestFailed = Boolean(ingestStage?.status === "failed");
  const isNeedsMpp = slide?.status === "needs_mpp" || caseDetail?.status === "needs_mpp" || (isIngestDone && (!slide?.mpp_x || slide?.mpp_x <= 0));
  const isQcFailed = qcStage?.status === "failed";
  const isQcComplete = ["done", "awaiting_review", "failed", "confirmed"].includes(qcStage?.status ?? "");
  const isQcRunning =qcStage?.status === "running" || qcStage?.status === "queued";

  const [mppInput, setMppInput] = useState<string>("0.25");
  const [mppYInput, setMppYInput] = useState<string>("");
  const [mppSubmitting, setMppSubmitting] = useState<boolean>(false);
  const [mppError, setMppError] = useState<string | null>(null);

  const handleMppSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!slide?.id) return;
    const valX = parseFloat(mppInput);
    if (isNaN(valX) || valX <= 0) {
      setMppError("Please enter a valid positive number for MPP X.");
      return;
    }
    const valY = mppYInput ? parseFloat(mppYInput) : undefined;
    if (valY !== undefined && (isNaN(valY) || valY <= 0)) {
      setMppError("Please enter a valid positive number for MPP Y.");
      return;
    }
    setMppSubmitting(true);
    setMppError(null);
    try {
      await updateSlideMpp(caseId, slide.id, valX, valY);
      await loadData();
    } catch (err: any) {
      setMppError(err?.message || "Failed to update MPP");
    } finally {
      setMppSubmitting(false);
    }
  };

  // Preprocess refuses an 'unknown' specimen (SPEC-04 §3.2): state it, then preprocess is retried.
  const isSpecimenUnknown = caseDetail?.specimen_type === "unknown";
  const [specimenInput, setSpecimenInput] = useState<SpecimenType | "">("");
  const [specimenSubmitting, setSpecimenSubmitting] = useState<boolean>(false);
  const [specimenError, setSpecimenError] = useState<string | null>(null);

  const handleSpecimenSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!specimenInput) return;
    setSpecimenSubmitting(true);
    setSpecimenError(null);
    try {
      await updateCaseSpecimenType(caseId, specimenInput);
      if (preprocessStage?.status === "failed") {
        await retryStage(caseId, "preprocess");
      }
      await loadData();
    } catch (err: any) {
      setSpecimenError(err?.message || L.error.genericError);
    } finally {
      setSpecimenSubmitting(false);
    }
  };

  const isPreprocessDone = preprocessStage?.status === "done" || preprocessStage?.status === "confirmed" || preprocessStage?.status === "awaiting_review";
  const isTriageDone = triageStage?.status === "done" || triageStage?.status === "confirmed" || triageStage?.status === "awaiting_review";
  const isMitosisDone = mitosisStage?.status === "done" || mitosisStage?.status === "confirmed" || mitosisStage?.status === "awaiting_review";

  const handleRetryIngest = async () => {
    setActionError(null);
    try {
      await retryStage(caseId, "ingest");
      await loadData();
    } catch (err: any) {
      console.error(err);
      setActionError(err?.message || "Failed to retry ingest stage");
    }
  };

  const handleApprovePreprocess = async (justification?: string) => {
    if (isQcFailed && !justification) {
      setOverrideModalOpen(true);
      return;
    }
    setActionLoading(true);
    setActionError(null);
    try {
      await approveStage(caseId, "preprocess", justification ? { override_justification: justification } : undefined);
      setOverrideModalOpen(false);
      setOverrideJustification("");
      setHasUserNavigated(true);
      setActiveStage("triage");
      await loadData();
    } catch (err: any) {
      console.error(err);
      setActionError(err?.message || "Failed to approve slide");
    } finally {
      setActionLoading(false);
    }
  };

  const handleReprocessPreprocess = async () => {
    setActionLoading(true);
    setActionError(null);
    try {
      await retryStage(caseId, "preprocess");
      setHasUserNavigated(true);
      setActiveStage("preprocess");
      await loadData();
    } catch (err: any) {
      console.error(err);
      setActionError(err?.message || "Failed to re-process slide");
    } finally {
      setActionLoading(false);
    }
  };

  const handleReprocessTriage = async () => {
    setActionLoading(true);
    setActionError(null);
    try {
      await retryStage(caseId, "triage");
      await loadData();
    } catch (err: any) {
      console.error(err);
      setActionError(err?.message || "Failed to re-assess hotspots");
    } finally {
      setActionLoading(false);
    }
  };

  const handleApproveTriage = async () => {
    setActionLoading(true);
    setActionError(null);
    try {
      try {
        await confirmTriageStage(caseId, false);
      } catch (confirmErr) {
        console.warn("confirmTriageStage returned error, falling back to approveStage:", confirmErr);
        await approveStage(caseId, "triage");
      }
      setHasUserNavigated(true);
      setActiveStage("mitosis");
      await loadData();
    } catch (err: any) {
      console.error(err);
      setActionError(err?.message || "Failed to confirm hotspots");
    } finally {
      setActionLoading(false);
    }
  };

  const handleApproveMitosis = async () => {
    setActionLoading(true);
    setActionError(null);
    try {
      await approveStage(caseId, "mitosis");
      setHasUserNavigated(true);
      setActiveStage("grading");
      await loadData();
    } catch (err: any) {
      console.error(err);
      setActionError(err?.message || "Failed to confirm mitoses");
    } finally {
      setActionLoading(false);
    }
  };

  const handleReprocessMitosis = async () => {
    setActionLoading(true);
    setActionError(null);
    try {
      await retryStage(caseId, "mitosis");
      await loadData();
    } catch (err: any) {
      console.error(err);
      setActionError(err?.message || "Failed to re-count mitoses");
    } finally {
      setActionLoading(false);
    }
  };

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden bg-slate-900">
      {/* Top Workspace Bar */}
      <div className="bg-slate-900 border-b border-slate-800 text-white px-4 py-2 flex items-center justify-between z-20">
        <div className="flex items-center space-x-3">
          <Link
            href="/cases"
            className="p-1.5 hover:bg-slate-800 rounded-lg text-slate-400 hover:text-white transition"
            title={L.heading.cases}
            aria-label={L.heading.cases}
          >
            <ArrowLeft className="w-4 h-4" />
          </Link>
          <div className="h-4 w-[1px] bg-slate-700" />
          <div>
            <div className="flex items-center space-x-2">
              <h1 className="text-sm font-semibold tracking-tight">
                {L.field.caseId}: {caseId.substring(0, 8)}
              </h1>
              <span className="text-[10px] bg-sky-900/60 border border-sky-700 text-sky-300 px-2 py-0.5 rounded font-mono font-medium">
                {L.stage.grading}
              </span>
            </div>
            <div className="text-[11px] text-slate-400 flex items-center space-x-3 mt-0.5">
              <span>{L.field.mpp}: {slide?.mpp_x ? `${slide.mpp_x} ${L.unit.umPerPx}` : L.status.calibrating}</span>
              <span>•</span>
              <span>{L.field.created}: {formatISTDateTime(caseDetail?.created_at)}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center space-x-2">
          {/* Pathologist Action Buttons for Preprocess */}
          {isPreprocessDone && activeStage === "preprocess" && (
            <div className="flex items-center space-x-2 border-r border-slate-700 pr-3 mr-1">
              <button
                onClick={() => {
                  const hasDownstream = triageStage || mitosisStage || gradingStage;
                  if (hasDownstream && !window.confirm(L.action.retryStage)) {
                    return;
                  }
                  handleReprocessPreprocess();
                }}
                disabled={actionLoading}
                className="px-3 py-1.5 bg-amber-600/90 hover:bg-amber-600 text-white rounded-lg transition text-xs font-semibold flex items-center space-x-1.5 shadow border border-amber-500/50"
                title={L.action.retryStage}
              >
                <RotateCcw className={`w-3.5 h-3.5 ${actionLoading ? "animate-spin" : ""}`} />
                <span>{L.action.retryStage}</span>
              </button>

              {preprocessStage?.status === "confirmed" ? (
                <span className="px-3 py-1.5 bg-emerald-950/80 text-emerald-400 border border-emerald-500/40 rounded-lg text-xs font-semibold flex items-center space-x-1.5 shadow">
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>{L.status.confirmed}</span>
                </span>
              ) : isQcFailed ? (
                <button
                  onClick={() => setOverrideModalOpen(true)}
                  disabled={actionLoading}
                  className="px-3 py-1.5 bg-rose-600 hover:bg-rose-500 text-white rounded-lg transition text-xs font-semibold flex items-center space-x-1.5 shadow border border-rose-400/50"
                  title={L.error.qcFailed}
                >
                  <AlertTriangle className="w-3.5 h-3.5" />
                  <span>{L.action.overrideQc}</span>
                </button>
              ) : !isQcComplete ? (
                <span
                  className="px-3 py-1.5 bg-slate-800 text-slate-400 border border-slate-700 rounded-lg text-xs font-semibold flex items-center space-x-1.5"
                  title={L.status.qcPending}
                >
                  <Clock className="w-3.5 h-3.5 animate-pulse" />
                  <span>{L.status.qcPending}</span>
                </span>
              ) : (
                <button
                  onClick={() => handleApprovePreprocess()}
                  disabled={actionLoading}
                  className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg transition text-xs font-semibold flex items-center space-x-1.5 shadow border border-emerald-400/50"
                  title={L.action.confirm}
                >
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>{L.action.confirm}</span>
                </button>
              )}
            </div>
          )}

          {/* Pathologist Action Buttons for Triage */}
          {isTriageDone && activeStage === "triage" && (
            <div className="flex items-center space-x-2 border-r border-slate-700 pr-3 mr-1">
              <button
                onClick={() => {
                  const hasDownstream = mitosisStage || gradingStage;
                  if (hasDownstream && !window.confirm(L.action.rerunHotspots)) {
                    return;
                  }
                  handleReprocessTriage();
                }}
                disabled={actionLoading}
                className="px-3 py-1.5 bg-amber-600/90 hover:bg-amber-600 text-white rounded-lg transition text-xs font-semibold flex items-center space-x-1.5 shadow border border-amber-500/50"
                title={L.action.rerunHotspots}
              >
                <RotateCcw className={`w-3.5 h-3.5 ${actionLoading ? "animate-spin" : ""}`} />
                <span>{L.action.rerunHotspots}</span>
              </button>

              {triageStage?.status === "confirmed" ? (
                <span className="px-3 py-1.5 bg-emerald-950/80 text-emerald-400 border border-emerald-500/40 rounded-lg text-xs font-semibold flex items-center space-x-1.5 shadow">
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>{L.status.confirmed}</span>
                </span>
              ) : (
                <button
                  onClick={handleApproveTriage}
                  disabled={actionLoading}
                  className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg transition text-xs font-semibold flex items-center space-x-1.5 shadow border border-emerald-400/50"
                  title={L.action.confirmHotspots}
                >
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>{L.action.confirmHotspots}</span>
                </button>
              )}
            </div>
          )}

          {/* Pathologist Action Buttons for Mitosis */}
          {isMitosisDone && activeStage === "mitosis" && (
            <div className="flex items-center space-x-2 border-r border-slate-700 pr-3 mr-1">
              <button
                onClick={() => {
                  const hasDownstream = gradingStage;
                  if (hasDownstream && !window.confirm(L.action.replaceHpfs)) {
                    return;
                  }
                  handleReprocessMitosis();
                }}
                disabled={actionLoading}
                className="px-3 py-1.5 bg-amber-600/90 hover:bg-amber-600 text-white rounded-lg transition text-xs font-semibold flex items-center space-x-1.5 shadow border border-amber-500/50"
                title={L.action.replaceHpfs}
              >
                <RotateCcw className={`w-3.5 h-3.5 ${actionLoading ? "animate-spin" : ""}`} />
                <span>{L.action.replaceHpfs}</span>
              </button>

              {mitosisStage?.status === "confirmed" ? (
                <span className="px-3 py-1.5 bg-emerald-950/80 text-emerald-400 border border-emerald-500/40 rounded-lg text-xs font-semibold flex items-center space-x-1.5 shadow">
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>{L.status.confirmed}</span>
                </span>
              ) : (
                <button
                  onClick={handleApproveMitosis}
                  disabled={actionLoading}
                  className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg transition text-xs font-semibold flex items-center space-x-1.5 shadow border border-emerald-400/50"
                  title={L.action.confirmMitoses}
                >
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  <span>{L.action.confirmMitoses}</span>
                </button>
              )}
            </div>
          )}

          {/* Provenance Popover per Task 4 */}
          <Provenance
            provenance={
              (activeStage === "grading" ? gradingStage :
               activeStage === "mitosis" ? mitosisStage :
               activeStage === "triage" ? triageStage :
               activeStage === "preprocess" ? preprocessStage :
               ingestStage)?.provenance
            }
            model_versions={
              (activeStage === "grading" ? gradingStage :
               activeStage === "mitosis" ? mitosisStage :
               activeStage === "triage" ? triageStage :
               activeStage === "preprocess" ? preprocessStage :
               ingestStage)?.model_versions
            }
            config_hash={(caseDetail as any)?.config_hash}
            run_mode={(caseDetail as any)?.run_mode}
          />

          <button
            onClick={() => setShowSlideDetails(!showSlideDetails)}
            className="p-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg transition text-xs flex items-center space-x-1.5 border border-slate-700"
            title={L.heading.specimenProperties}
          >
            <Info className="w-3.5 h-3.5 text-sky-400" />
            <span>{L.heading.specimenProperties}</span>
          </button>

          <button
            onClick={loadData}
            className="p-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg transition text-xs flex items-center space-x-1 border border-slate-700"
            title={L.action.refresh}
          >
            <RefreshCcw className="w-3.5 h-3.5" />
            <span>{L.action.refresh}</span>
          </button>
        </div>
      </div>

      {/* Dismissible Error Notification */}
      {actionError && (
        <div className="bg-rose-900/90 border-b border-rose-700 text-white px-4 py-2 flex items-center justify-between text-xs z-30 animate-in fade-in">
          <div className="flex items-center space-x-2">
            <AlertTriangle className="w-4 h-4 text-rose-300 shrink-0" />
            <span>{actionError}</span>
          </div>
          <button onClick={() => setActionError(null)} className="p-0.5 hover:bg-rose-800 rounded">
            <X className="w-4 h-4 text-rose-200" />
          </button>
        </div>
      )}

      {/* QC Hard Failure Diagnostic Banner */}
      {activeStage === "preprocess" && isQcFailed && (
        <div className="bg-rose-950/90 border-b border-rose-800 text-rose-200 px-4 py-2.5 flex items-center justify-between text-xs z-20 backdrop-blur">
          <div className="flex items-center space-x-2.5">
            <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" />
            <div>
              <span className="font-bold text-white uppercase tracking-wider">{L.error.qcFailed} </span>
              <span className="text-[10px] bg-rose-900 border border-rose-700 text-rose-300 px-2 py-0.5 rounded font-mono font-medium ml-1">
                {L.action.rescan}
              </span>
            </div>
          </div>
          <div className="flex items-center space-x-2">
            <button
              onClick={handleReprocessPreprocess}
              disabled={actionLoading}
              className="px-3 py-1 bg-amber-600 hover:bg-amber-500 text-white rounded font-medium transition text-xs flex items-center space-x-1"
            >
              <RotateCcw className="w-3 h-3" />
              <span>{L.action.retryStage}</span>
            </button>
            <button
              onClick={() => setOverrideModalOpen(true)}
              disabled={actionLoading}
              className="px-3 py-1 bg-rose-600 hover:bg-rose-500 text-white rounded font-medium transition text-xs flex items-center space-x-1 font-semibold"
            >
              <span>{L.action.overrideQc}</span>
            </button>
          </div>
        </div>
      )}

      {/* Main Workspace Body */}
      <div className="flex-1 flex overflow-hidden relative">
        {/* Left Stage Rail */}
        <StageRail
          caseId={caseId}
          stages={caseDetail?.stages || []}
          activeStage={activeStage}
          onSelectStage={(stage) => {
            setHasUserNavigated(true);
            setActiveStage(stage);
          }}
          onRefresh={loadData}
        />

        {/* Center Digital Slide Viewer / Stage View */}
        <div className="flex-1 relative overflow-hidden bg-slate-950">
          {loading ? (
            <div className="flex items-center justify-center h-full text-slate-400 text-sm">
              {L.status.running}
            </div>
          ) : isNeedsMpp ? (
            <div className="flex flex-col items-center justify-center h-full text-slate-300 space-y-4 bg-slate-950 p-8">
              <div className="relative w-16 h-16 flex items-center justify-center bg-amber-500/10 rounded-full border border-amber-500/30">
                <AlertTriangle className="w-8 h-8 text-amber-400" />
              </div>
              <div className="text-center max-w-lg">
                <h3 className="text-base font-bold text-white tracking-tight">{L.heading.calibration}</h3>
                <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                  {L.help.noSlideYet}
                </p>
                <form onSubmit={handleMppSubmit} className="mt-5 bg-slate-900 border border-slate-800 rounded-xl p-4 text-left space-y-3">
                  <div>
                    <label className="block text-[11px] font-medium text-slate-300 mb-1">
                      {L.field.mpp} {L.field.xCoord} ({L.unit.umPerPx}) <span className="text-rose-400">*</span>
                    </label>
                    <input
                      type="number"
                      step="0.000001"
                      min="0.001"
                      required
                      value={mppInput}
                      onChange={(e) => setMppInput(e.target.value)}
                      placeholder={L.field.mpp}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500 font-mono"
                    />
                  </div>
                  <div>
                    <label className="block text-[11px] font-medium text-slate-300 mb-1">
                      {L.field.mpp} {L.field.yCoord} ({L.unit.umPerPx})
                    </label>
                    <input
                      type="number"
                      step="0.000001"
                      min="0.001"
                      value={mppYInput}
                      onChange={(e) => setMppYInput(e.target.value)}
                      placeholder={L.field.mpp}
                      className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500 font-mono"
                    />
                  </div>
                  {mppError && (
                    <p className="text-[11px] text-rose-400 font-medium">{mppError}</p>
                  )}
                  <button
                    type="submit"
                    disabled={mppSubmitting}
                    className="w-full bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white text-xs font-semibold py-2 px-4 rounded-lg transition shadow flex items-center justify-center space-x-2"
                  >
                    {mppSubmitting ? (
                      <span>{L.status.processing}</span>
                    ) : (
                      <span>{L.action.saveMpp}</span>
                    )}
                  </button>
                </form>
              </div>
            </div>
          ) : isSpecimenUnknown ? (
            <div className="flex flex-col items-center justify-center h-full text-slate-300 space-y-4 bg-slate-950 p-8">
              <div className="relative w-16 h-16 flex items-center justify-center bg-amber-500/10 rounded-full border border-amber-500/30">
                <AlertTriangle className="w-8 h-8 text-amber-400" />
              </div>
              <div className="text-center max-w-lg">
                <h3 className="text-base font-bold text-white tracking-tight">{L.field.specimenType}</h3>
                <p className="text-xs text-slate-400 mt-2 leading-relaxed">
                  {L.help.specimenTypeRequired}
                </p>
                <form onSubmit={handleSpecimenSubmit} className="mt-5 bg-slate-900 border border-slate-800 rounded-xl p-4 text-left space-y-3">
                  <select
                    required
                    value={specimenInput}
                    onChange={(e) => setSpecimenInput(e.target.value as SpecimenType | "")}
                    aria-label={L.field.specimenType}
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-xs text-white focus:outline-none focus:border-sky-500"
                  >
                    <option value="" disabled>{L.field.specimenType}</option>
                    <option value="resection">{L.field.specimenResection}</option>
                    <option value="core_biopsy">{L.field.specimenCoreBiopsy}</option>
                  </select>
                  {specimenError && (
                    <p className="text-[11px] text-rose-400 font-medium">{specimenError}</p>
                  )}
                  <button
                    type="submit"
                    disabled={specimenSubmitting || !specimenInput}
                    className="w-full bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white text-xs font-semibold py-2 px-4 rounded-lg transition shadow flex items-center justify-center space-x-2"
                  >
                    <span>{specimenSubmitting ? L.status.processing : L.action.saveSpecimenType}</span>
                  </button>
                </form>
              </div>
            </div>
          ) : isIngestMissing && !isIngestDone ? (
            <div className="flex flex-col items-center justify-center h-full text-slate-300 space-y-4 bg-slate-950 p-8">
              <div className="relative w-16 h-16 flex items-center justify-center bg-slate-900 rounded-full border border-slate-800">
                <Microscope className="w-8 h-8 text-slate-400" />
              </div>
              <div className="text-center max-w-md">
                <h3 className="text-base font-bold text-white tracking-tight">{L.error.noSlideUploaded}</h3>
                <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
                  {L.help.noSlideYet}
                </p>
                <div className="mt-5 flex items-center justify-center space-x-3">
                  <Link
                    href="/cases"
                    className="px-4 py-2 bg-sky-600 hover:bg-sky-500 text-white text-xs font-semibold rounded-lg transition shadow flex items-center space-x-1.5"
                  >
                    <ArrowLeft className="w-3.5 h-3.5" />
                    <span>{L.action.uploadSlide}</span>
                  </Link>
                </div>
              </div>
            </div>
          ) : isIngestRunning ? (
            <div className="flex flex-col items-center justify-center h-full text-slate-300 space-y-4 bg-slate-950 p-8">
              <div className="relative w-16 h-16 flex items-center justify-center">
                <div className="absolute inset-0 rounded-full border-4 border-sky-500/20 border-t-sky-500 animate-spin" />
                <Microscope className="w-8 h-8 text-sky-400" />
              </div>
              <div className="text-center max-w-md">
                <h3 className="text-base font-bold text-white tracking-tight">{L.status.processing}</h3>
                <p className="text-xs text-slate-400 mt-1.5 leading-relaxed">
                  {L.status.running}
                </p>
              </div>
            </div>
          ) : isIngestFailed ? (
            <div className="flex flex-col items-center justify-center h-full text-slate-300 space-y-4 bg-slate-950 p-8">
              <AlertTriangle className="w-12 h-12 text-rose-500" />
              <div className="text-center max-w-md">
                <h3 className="text-base font-bold text-white">{L.error.stageExecutionFailed}</h3>
                <p className="text-xs text-slate-400 mt-1">
                  {ingestStage?.error || L.error.genericError}
                </p>
                <button
                  onClick={handleRetryIngest}
                  className="mt-4 bg-rose-600 hover:bg-rose-700 text-white text-xs font-semibold px-4 py-2 rounded-lg transition shadow"
                >
                  {L.action.retryStage}
                </button>
              </div>
            </div>
          ) : activeStage === "grading" ? (
            <ErrorBoundary>
              <GradingReviewWorkspace
                caseId={caseId}
                onReopenMitosis={() => {
                  setHasUserNavigated(true);
                  setActiveStage("mitosis");
                }}
                onRefreshCase={loadData}
              />
            </ErrorBoundary>
          ) : activeStage === "mitosis" ? (
            <ErrorBoundary>
              <MitosisViewer
                caseId={caseId}
                mppX={slide?.mpp_x || 0.25}
                mppY={slide?.mpp_y || slide?.mpp_x || 0.25}
                imageWidthPx={slide?.width_px || 2048}
                imageHeightPx={slide?.height_px || 2048}
                onRefreshCase={loadData}
                tileUrlTemplate={caseDetail?.tile_url_template}
              />
            </ErrorBoundary>
          ) : activeStage === "triage" ? (
            <ErrorBoundary>
              <TriageViewer
                caseId={caseId}
                mppX={slide?.mpp_x || 0.25}
                mppY={slide?.mpp_y || slide?.mpp_x || 0.25}
                imageWidthPx={slide?.width_px || 2048}
                imageHeightPx={slide?.height_px || 2048}
                onRefreshCase={loadData}
                onAdvanceToMitosis={() => {
                  setHasUserNavigated(true);
                  setActiveStage("mitosis");
                  loadData();
                }}
                tileUrlTemplate={caseDetail?.tile_url_template}
              />
            </ErrorBoundary>
          ) : (
            <ErrorBoundary>
              <OpenSeadragonViewer
                caseId={caseId}
                mppX={slide?.mpp_x || 0.25}
                mppY={slide?.mpp_y || slide?.mpp_x || 0.25}
                imageWidthPx={slide?.width_px || 2048}
                imageHeightPx={slide?.height_px || 2048}
                tileUrlTemplate={caseDetail?.tile_url_template}
              />
            </ErrorBoundary>
          )}
        </div>

        {/* Slide Technical Details Popover/Modal */}
        {showSlideDetails && (
          <div className="absolute top-4 left-72 bg-slate-900/95 backdrop-blur border border-slate-700 text-white rounded-xl shadow-2xl p-4 w-96 z-30 space-y-3">
            <div className="flex items-center justify-between border-b border-slate-800 pb-2">
              <h3 className="text-xs font-bold text-sky-400 uppercase tracking-wider flex items-center space-x-1.5">
                <Info className="w-4 h-4" />
                <span>{L.heading.specimenProperties}</span>
              </h3>
              <button
                onClick={() => setShowSlideDetails(false)}
                className="p-1 hover:bg-slate-800 text-slate-400 hover:text-white rounded transition"
                title={L.action.close}
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="space-y-2 text-xs">
              <div className="flex justify-between border-b border-slate-800/50 py-1">
                <span className="text-slate-400">{L.field.slideFile}:</span>
                <span className="font-mono text-slate-200">{slide?.id || "-"}</span>
              </div>
              <div className="flex justify-between border-b border-slate-800/50 py-1">
                <span className="text-slate-400">{L.field.specimenType}:</span>
                <span className="font-mono text-slate-200">
                  {caseDetail?.specimen_type === "resection"
                    ? L.field.specimenResection
                    : caseDetail?.specimen_type === "core_biopsy"
                      ? L.field.specimenCoreBiopsy
                      : L.field.specimenUnknown}
                </span>
              </div>
              <div className="flex justify-between border-b border-slate-800/50 py-1">
                <span className="text-slate-400">{L.field.scale}:</span>
                <span className="font-mono text-slate-200">{slide?.width_px || 0} × {slide?.height_px || 0}</span>
              </div>
              <div className="flex justify-between border-b border-slate-800/50 py-1">
                <span className="text-slate-400">{L.field.magnification}:</span>
                <span className="font-mono text-slate-200 capitalize">{slide?.scanner || "-"}</span>
              </div>
              <div className="flex justify-between border-b border-slate-800/50 py-1">
                <span className="text-slate-400">{L.field.configHash}:</span>
                <span className="font-mono text-[10px] text-slate-300 truncate max-w-[180px]" title={slide?.checksum_sha256}>
                  {slide?.checksum_sha256 || "-"}
                </span>
              </div>
              <div className="flex justify-between py-1">
                <span className="text-slate-400">{L.field.created}:</span>
                <span className="font-mono text-slate-200">{formatISTDateTime(slide?.label_stripped_at)}</span>
              </div>
            </div>
          </div>
        )}

        {/* QC Failure Clinical Override Modal */}
        {overrideModalOpen && (
          <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50 p-4">
            <div className="bg-slate-900 border border-slate-700 rounded-xl max-w-md w-full p-5 text-white shadow-2xl space-y-4">
              <div className="flex items-center justify-between border-b border-slate-800 pb-3">
                <div className="flex items-center space-x-2 text-rose-400 font-bold text-sm">
                  <AlertTriangle className="w-4 h-4" />
                  <span>{L.action.overrideQc}</span>
                </div>
                <button
                  onClick={() => setOverrideModalOpen(false)}
                  className="text-slate-400 hover:text-white p-1 rounded hover:bg-slate-800 transition"
                  title={L.action.close}
                >
                  <X className="w-4 h-4" />
                </button>
              </div>

              <div className="text-xs text-slate-300 space-y-2">
                <p className="bg-rose-950/60 border border-rose-800/80 rounded-lg p-3 text-rose-200 font-mono text-[11px]">
                  {qcStage?.error || L.error.qcFailed}
                </p>
                <p className="text-slate-400 text-[11px] leading-relaxed">
                  {L.help.overrideReasonMinLength}
                </p>
              </div>

              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  if (overrideJustification.trim().length >= 10) {
                    handleApprovePreprocess(overrideJustification.trim());
                  }
                }}
                className="space-y-3"
              >
                <div>
                  <label className="block text-[11px] font-medium text-slate-300 mb-1">
                    {L.field.overrideReason} <span className="text-rose-400">*</span>
                  </label>
                  <textarea
                    rows={3}
                    required
                    minLength={10}
                    value={overrideJustification}
                    onChange={(e) => setOverrideJustification(e.target.value)}
                    placeholder={L.field.overrideReason}
                    className="w-full bg-slate-950 border border-slate-700 rounded-lg p-2.5 text-xs text-white focus:outline-none focus:border-rose-500 font-sans resize-none"
                  />
                  <div className="flex justify-between text-[10px] text-slate-500 mt-1">
                    <span>{overrideJustification.trim().length}/10</span>
                  </div>
                </div>

                <div className="flex items-center justify-end space-x-2 pt-2 border-t border-slate-800">
                  <button
                    type="button"
                    onClick={() => setOverrideModalOpen(false)}
                    className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 rounded-lg text-xs font-medium transition"
                  >
                    {L.action.cancel}
                  </button>
                  <button
                    type="submit"
                    disabled={actionLoading || overrideJustification.trim().length < 10}
                    className="px-3 py-1.5 bg-rose-600 hover:bg-rose-500 disabled:opacity-50 text-white rounded-lg text-xs font-semibold transition flex items-center space-x-1.5 shadow"
                  >
                    {actionLoading ? (
                      <span>{L.status.processing}</span>
                    ) : (
                      <span>{L.action.overrideQc}</span>
                    )}
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
