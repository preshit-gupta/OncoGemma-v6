"use client";

import React, { useEffect, useState, useCallback, useMemo } from "react";
import { 
  Microscope, 
  CheckCircle2, 
  RotateCcw, 
  ArrowRight, 
  Activity, 
  Loader2, 
  Info, 
  AlertTriangle,
  BookOpen,
  X,
  Crosshair,
  Check,
  ChevronDown,
  ChevronUp
} from "lucide-react";
import { 
  getMitosis, 
  reviewCandidate, 
  addCandidate, 
  replaceHpfs, 
  confirmMitosis, 
  Candidate, 
  MitosisStageV6, 
  Hpf, 
  MitosisSummary 
} from "@/lib/api/mitosis";
import { MITOTIC_FIGURE_DEFINITION } from "@/lib/definitions/mitotic_figure";
import { OpenSeadragonViewer, ViewerDetectionMarker } from "./OpenSeadragonViewer";
import { MitosisGallery } from "./MitosisGallery";
import { L } from "@/lib/labels";
import { Provenance } from "../Provenance";

interface MitosisViewerProps {
  caseId: string;
  mppX?: number;
  mppY?: number;
  imageWidthPx?: number;
  imageHeightPx?: number;
  onRefreshCase?: () => void;
  tileUrlTemplate?: string | null;
  onAdvanceToGrading?: () => void;
}

export function MitosisViewer({
  caseId,
  mppX = 0.25,
  mppY = 0.25,
  imageWidthPx = 20000,
  imageHeightPx = 20000,
  onRefreshCase,
  tileUrlTemplate = null,
  onAdvanceToGrading,
}: MitosisViewerProps) {
  const [data, setData] = useState<MitosisStageV6 | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [hpfs, setHpfs] = useState<Hpf[]>([]);
  const [summary, setSummary] = useState<MitosisSummary>({
    count_total: 0,
    n_hpf: 0,
    area_mm2: 0,
    per_mm2: 0,
    mitotic_score: null,
    n_equivocal: 0,
    flags: [],
  });

  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null);
  const [filterMode, setFilterMode] = useState<"all" | "equivocal" | "mitosis" | "not_mitosis">("all");
  const [imageViewMode, setImageViewMode] = useState<"crop" | "context">("crop");
  const [showDefinitionPanel, setShowDefinitionPanel] = useState<boolean>(false);
  const [isReplacingHpfs, setIsReplacingHpfs] = useState<boolean>(false);

  // Load stage data
  const loadStageData = useCallback(async (silent: boolean = false) => {
    try {
      if (!silent && !data) setLoading(true);
      setError(null);
      const stageData = await getMitosis(caseId);
      setData(stageData);
      setCandidates(stageData.candidates || []);
      setHpfs(stageData.hpfs || []);
      setSummary(stageData.summary);

      if (stageData.candidates && stageData.candidates.length > 0 && !selectedCandidateId) {
        // Priority: select first equivocal, else first candidate
        const firstEquivocal = stageData.candidates.find(
          (c) => c.final_decision === "equivocal" && c.review_label === null
        );
        setSelectedCandidateId(firstEquivocal ? firstEquivocal.id : stageData.candidates[0].id);
      }
    } catch (err: any) {
      if (!silent) setError(err.message || L.error.genericError);
    } finally {
      if (!silent) setLoading(false);
    }
  }, [caseId, data, selectedCandidateId]);

  useEffect(() => {
    loadStageData();
  }, [loadStageData]);

  // Priority queue order per contract:
  // Show equivocal first, then mitosis, then not_mitosis.
  // Within each group, sort by p_b ?? p_a, descending.
  const sortedCandidates = useMemo(() => {
    const priority = (c: Candidate) => {
      if (c.final_decision === "equivocal" && c.review_label === null) return 0;
      if (c.final_decision === "equivocal") return 1;
      const verdict = c.review_label ?? c.final_decision;
      if (verdict === "mitosis") return 2;
      return 3;
    };

    return [...candidates].sort((a, b) => {
      const prioDiff = priority(a) - priority(b);
      if (prioDiff !== 0) return prioDiff;
      const scoreA = a.p_b ?? a.p_a ?? 0;
      const scoreB = b.p_b ?? b.p_a ?? 0;
      return scoreB - scoreA;
    });
  }, [candidates]);

  const selectedCandidate = useMemo(() => {
    return candidates.find((c) => c.id === selectedCandidateId) || null;
  }, [candidates, selectedCandidateId]);

  const handleReviewCandidate = async (
    candidateId: string,
    label: "mitosis" | "not_mitosis" | null
  ) => {
    try {
      setError(null);
      const updated = await reviewCandidate(caseId, candidateId, label);
      setData(updated);
      setCandidates(updated.candidates || []);
      setSummary(updated.summary);
      if (updated.hpfs) setHpfs(updated.hpfs);
    } catch (err: any) {
      setError(err.message || L.error.stageExecutionFailed);
    }
  };

  const handleReplaceHpfs = async () => {
    try {
      setIsReplacingHpfs(true);
      setError(null);
      const updated = await replaceHpfs(caseId);
      setData(updated);
      setHpfs(updated.hpfs || []);
      setSummary(updated.summary);
      if (updated.candidates) setCandidates(updated.candidates);
    } catch (err: any) {
      setError(err.message || L.error.stageExecutionFailed);
    } finally {
      setIsReplacingHpfs(false);
    }
  };

  const handleConfirmStage = async () => {
    try {
      setSubmitting(true);
      setError(null);
      await confirmMitosis(caseId);
      if (data) {
        setData({ ...data, status: "confirmed" });
      }
      if (onAdvanceToGrading) {
        onAdvanceToGrading();
      } else if (onRefreshCase) {
        onRefreshCase();
      }
    } catch (err: any) {
      if (err.data?.error === "equivocal_unreviewed") {
        const unreviewedIds: string[] = err.data.ids || [];
        if (unreviewedIds.length > 0) {
          setSelectedCandidateId(unreviewedIds[0]);
        }
        setError(L.error.equivocalUnreviewed);
      } else {
        setError(err.message || L.error.stageExecutionFailed);
      }
    } finally {
      setSubmitting(false);
    }
  };

  // Keyboard shortcut listener
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if user is inside an input or textarea
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement ||
        e.target instanceof HTMLSelectElement
      ) {
        return;
      }

      if (e.key === " " || e.code === "Space") {
        e.preventDefault();
        setImageViewMode((prev) => (prev === "crop" ? "context" : "crop"));
        return;
      }

      if (!selectedCandidateId) return;

      const currentIndex = sortedCandidates.findIndex((c) => c.id === selectedCandidateId);

      if (e.key === "m" || e.key === "M") {
        e.preventDefault();
        handleReviewCandidate(selectedCandidateId, "mitosis");
        if (currentIndex < sortedCandidates.length - 1) {
          setSelectedCandidateId(sortedCandidates[currentIndex + 1].id);
        }
      } else if (e.key === "x" || e.key === "X") {
        e.preventDefault();
        handleReviewCandidate(selectedCandidateId, "not_mitosis");
        if (currentIndex < sortedCandidates.length - 1) {
          setSelectedCandidateId(sortedCandidates[currentIndex + 1].id);
        }
      } else if (e.key === "u" || e.key === "U") {
        e.preventDefault();
        handleReviewCandidate(selectedCandidateId, null);
      } else if (e.key === "ArrowDown" || e.key === "j" || e.key === "J") {
        e.preventDefault();
        if (currentIndex < sortedCandidates.length - 1) {
          setSelectedCandidateId(sortedCandidates[currentIndex + 1].id);
        }
      } else if (e.key === "ArrowUp" || e.key === "k" || e.key === "K") {
        e.preventDefault();
        if (currentIndex > 0) {
          setSelectedCandidateId(sortedCandidates[currentIndex - 1].id);
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selectedCandidateId, sortedCandidates]);

  // Marker mapping for OpenSeadragonViewer
  const detectionMarkers: ViewerDetectionMarker[] = useMemo(() => {
    return candidates.map((c) => ({
      id: c.id,
      centroid_um: c.centroid_um,
      label: (c.review_label ?? c.final_decision) === "mitosis" ? "mitosis" : "not_mitosis",
      confidence: c.p_b ?? c.p_a ?? 0.5,
      in_hpf: c.counted,
    }));
  }, [candidates]);

  if (loading) {
    return (
      <div className="w-full h-full bg-slate-950 flex flex-col items-center justify-center text-slate-400">
        <Loader2 className="w-8 h-8 animate-spin text-sky-500 mb-2" />
        <p className="text-sm font-medium">{L.status.countingMitoses}</p>
      </div>
    );
  }

  return (
    <div className="w-full h-full flex flex-col bg-slate-950 text-slate-100 select-none overflow-hidden">
      {/* Top Clinical Summary Header */}
      <header className="px-6 py-3.5 bg-slate-900 border-b border-slate-800 flex items-center justify-between shadow-md shrink-0">
        <div className="flex items-center space-x-4">
          <div className="flex items-center space-x-2 text-sky-400 font-bold text-sm">
            <Microscope className="w-5 h-5" />
            <span>{L.heading.mitosisScoring}</span>
          </div>

          <div className="h-5 w-px bg-slate-800" />

          {/* Server-computed summary: count, area, mitotic score */}
          <div className="flex items-center space-x-3 text-xs">
            <span className="font-mono font-semibold text-white">
              {L.fmt.summaryMitosis(summary.count_total, summary.area_mm2, summary.mitotic_score)}
            </span>
            <span
              className={`px-2 py-0.5 rounded-full font-bold uppercase text-[10px] ${
                summary.mitotic_score === 3
                  ? "bg-rose-950 text-rose-300 border border-rose-800"
                  : summary.mitotic_score === 2
                  ? "bg-amber-950 text-amber-300 border border-amber-800"
                  : "bg-emerald-950 text-emerald-300 border border-emerald-800"
              }`}
            >
              {summary.mitotic_score === null ? L.field.noMitoticScore : `${L.field.grade} ${summary.mitotic_score}`}
            </span>
          </div>

          {summary.n_equivocal > 0 && (
            <span className="text-xs px-2.5 py-0.5 bg-amber-950 text-amber-300 border border-amber-700/60 rounded-full font-medium flex items-center gap-1">
              <AlertTriangle className="w-3 h-3 text-amber-400" />
              <span>
                {summary.n_equivocal} {L.field.equivocal}
              </span>
            </span>
          )}
        </div>

        <div className="flex items-center space-x-3">
          {data?.provenance && <Provenance provenance={data.provenance} />}

          <button
            type="button"
            onClick={() => setShowDefinitionPanel(!showDefinitionPanel)}
            className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition shadow-sm"
          >
            <BookOpen className="w-3.5 h-3.5 text-sky-400" />
            <span>{L.heading.mitosisDefinition}</span>
          </button>

          <button
            type="button"
            onClick={handleReplaceHpfs}
            disabled={isReplacingHpfs}
            className="px-3 py-1.5 bg-amber-600/20 hover:bg-amber-600/30 text-amber-300 border border-amber-500/40 rounded-lg text-xs font-semibold flex items-center space-x-1.5 transition shadow-sm"
          >
            <RotateCcw className={`w-3.5 h-3.5 ${isReplacingHpfs ? "animate-spin" : ""}`} />
            <span>{L.action.replaceHpfs}</span>
          </button>

          <button
            type="button"
            onClick={handleConfirmStage}
            disabled={submitting || data?.status === "confirmed"}
            className={`px-4 py-1.5 rounded-lg text-xs font-bold flex items-center space-x-1.5 transition shadow ${
              data?.status === "confirmed"
                ? "bg-emerald-950 text-emerald-300 border border-emerald-800 cursor-not-allowed"
                : "bg-sky-600 hover:bg-sky-500 text-white shadow-sky-600/20"
            }`}
          >
            {submitting ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : data?.status === "confirmed" ? (
              <>
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                <span>{L.status.confirmed}</span>
              </>
            ) : (
              <>
                <span>{L.action.confirmMitoses}</span>
                <ArrowRight className="w-3.5 h-3.5" />
              </>
            )}
          </button>
        </div>
      </header>

      {/* Flag / Error Notice Banners */}
      <div className="shrink-0 flex flex-col space-y-1">
        {summary.flags?.includes("hpf_count_lt_10") && (
          <div className="px-6 py-2 bg-amber-950/80 border-b border-amber-800 text-amber-200 text-xs flex items-center space-x-2">
            <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
            <span>{L.help.hpfCountWarning}</span>
          </div>
        )}

        {/* Placed HPFs: count, tissue coverage and tumour fraction (server values) */}
        {hpfs.length > 0 && (
          <div className="px-6 py-1.5 bg-slate-900/80 border-b border-slate-800 text-[11px] text-slate-300 flex flex-wrap gap-1.5">
            {hpfs.map((h) => (
              <span key={h.seq} className="px-2 py-0.5 rounded bg-slate-950 border border-slate-800 font-mono">
                {L.field.hpfLabel} {h.seq} · {h.count} · {L.field.hpfTissue} {L.fmt.percent(h.tissue_coverage)} ·{" "}
                {L.field.tumorFraction} {h.tumor_fraction === null ? "—" : L.fmt.percent(h.tumor_fraction)}
              </span>
            ))}
          </div>
        )}

        {error && (
          <div className="px-6 py-2 bg-rose-950/90 border-b border-rose-800 text-rose-200 text-xs flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <AlertTriangle className="w-4 h-4 text-rose-400 shrink-0" />
              <span>{error}</span>
            </div>
            <button
              type="button"
              onClick={() => setError(null)}
              className="text-rose-400 hover:text-rose-200"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>

      {/* Main Workspace Layout */}
      <div className="flex-1 flex overflow-hidden relative">
        {/* Left: OpenSeadragon Whole Slide Viewer */}
        <div className="flex-1 relative">
          <OpenSeadragonViewer
            caseId={caseId}
            mppX={mppX}
            mppY={mppY || mppX}
            imageWidthPx={imageWidthPx}
            imageHeightPx={imageHeightPx}
            detectionMarkers={detectionMarkers}
            showCandidateMarkers={true}
            selectedCandidateId={selectedCandidateId}
            onSelectCandidate={(id) => setSelectedCandidateId(id)}
            tileUrlTemplate={tileUrlTemplate}
          />
        </div>

        {/* Center: Selected Candidate Decision-Chain Inspector Panel */}
        {selectedCandidate && (
          <div className="w-80 bg-slate-900 border-l border-slate-800 flex flex-col h-full shadow-xl overflow-y-auto">
            {/* Header */}
            <div className="p-3.5 border-b border-slate-800 bg-slate-950/60 flex items-center justify-between">
              <div className="flex items-center space-x-2">
                <Activity className="w-4 h-4 text-sky-400" />
                <h3 className="font-bold text-xs uppercase tracking-wider text-slate-200">
                  {L.heading.decisionChain}
                </h3>
              </div>
              <span className="font-mono text-xs font-bold text-sky-400">
                {selectedCandidate.id}
              </span>
            </div>

            {/* Candidate Image (Crop or Context) */}
            <div className="p-4 flex flex-col items-center bg-slate-950 border-b border-slate-800">
              <div className="relative w-48 h-48 rounded-lg overflow-hidden bg-black border border-slate-700 shadow-inner">
                <img
                  src={
                    imageViewMode === "context"
                      ? selectedCandidate.context_url
                      : selectedCandidate.crop_url
                  }
                  alt={selectedCandidate.id}
                  className="w-full h-full object-cover"
                />
              </div>

              {/* Crop / Context Toggle */}
              <div className="mt-2.5 flex items-center bg-slate-900 border border-slate-700 rounded-lg p-0.5 text-xs font-semibold">
                <button
                  type="button"
                  onClick={() => setImageViewMode("crop")}
                  className={`px-3 py-1 rounded transition ${
                    imageViewMode === "crop"
                      ? "bg-sky-600 text-white shadow-sm"
                      : "text-slate-400 hover:text-white"
                  }`}
                >
                  {"64 " + L.unit.um}
                </button>
                <button
                  type="button"
                  onClick={() => setImageViewMode("context")}
                  className={`px-3 py-1 rounded transition ${
                    imageViewMode === "context"
                      ? "bg-sky-600 text-white shadow-sm"
                      : "text-slate-400 hover:text-white"
                  }`}
                >
                  {"256 " + L.unit.um}
                </button>
              </div>
            </div>

            {/* Probabilities & Status Badges */}
            <div className="p-4 border-b border-slate-800 space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-400">{L.field.status}:</span>
                <div className="flex items-center space-x-1.5">
                  {selectedCandidate.counted && (
                    <span className="px-2 py-0.5 rounded bg-emerald-950 border border-emerald-700 text-emerald-300 font-bold text-[10px] uppercase font-mono">
                      {L.field.counted}
                    </span>
                  )}
                  <span
                    className={`px-2 py-0.5 rounded text-xs font-bold uppercase font-mono ${
                      selectedCandidate.final_decision === "mitosis"
                        ? "bg-emerald-950 text-emerald-400 border border-emerald-800"
                        : selectedCandidate.final_decision === "equivocal"
                        ? "bg-amber-950 text-amber-400 border border-amber-800"
                        : "bg-slate-800 text-slate-400 border border-slate-700"
                    }`}
                  >
                    {selectedCandidate.final_decision}
                  </span>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-2 text-xs font-mono">
                <div className="bg-slate-950 p-2 rounded border border-slate-800">
                  <div className="text-[10px] text-slate-400">{L.field.detectorProb}</div>
                  <div className="text-sm font-bold text-slate-200">
                    {selectedCandidate.p_a != null ? selectedCandidate.p_a.toFixed(2) : "—"}
                  </div>
                </div>
                <div className="bg-slate-950 p-2 rounded border border-slate-800">
                  <div className="text-[10px] text-slate-400">{L.field.classifierProb}</div>
                  <div className="text-sm font-bold text-slate-200">
                    {selectedCandidate.p_b != null ? selectedCandidate.p_b.toFixed(2) : "—"}
                  </div>
                </div>
              </div>

              <div className="flex items-center justify-between text-xs text-slate-400">
                <span>{L.field.tumorGate}:</span>
                <span className="font-mono text-slate-200 font-bold">
                  {selectedCandidate.in_tumor === null
                    ? L.field.tumorGateNotApplied
                    : selectedCandidate.in_tumor
                    ? L.field.inTumor
                    : L.field.outsideTumor}
                </span>
              </div>

              <div className="flex items-center justify-between text-xs text-slate-400">
                <span>{L.field.decisionPath}:</span>
                <span className="font-mono text-slate-200 font-bold">
                  {selectedCandidate.decision_path}
                </span>
              </div>
            </div>

            {/* VLM Verdict & Criteria Checklist */}
            {selectedCandidate.vlm && (
              <div className="p-4 border-b border-slate-800 space-y-2.5">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-sky-300 uppercase tracking-wider">
                    {L.field.vlmVerdict}
                  </span>
                  {selectedCandidate.vlm.rule_override && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-rose-950 text-rose-300 border border-rose-800 font-bold">
                      {L.field.ruleOverride}
                    </span>
                  )}
                </div>

                <div className="text-xs font-bold text-slate-200 font-mono">
                  {selectedCandidate.vlm.verdict}
                </div>

                <div className="bg-slate-950 p-2.5 rounded border border-slate-800 space-y-1.5 text-xs">
                  <div className="flex items-center justify-between text-slate-300">
                    <span>{L.field.membraneAbsent}</span>
                    <span
                      className={
                        selectedCandidate.vlm.criteria.membrane_absent
                          ? "text-emerald-400 font-bold"
                          : "text-rose-400 font-bold"
                      }
                    >
                      {selectedCandidate.vlm.criteria.membrane_absent ? "✓" : "✗"}
                    </span>
                  </div>

                  <div className="flex items-center justify-between text-slate-300">
                    <span>{L.field.condensedProjections}</span>
                    <span
                      className={
                        selectedCandidate.vlm.criteria.condensed_chromosome_projections
                          ? "text-emerald-400 font-bold"
                          : "text-rose-400 font-bold"
                      }
                    >
                      {selectedCandidate.vlm.criteria.condensed_chromosome_projections ? "✓" : "✗"}
                    </span>
                  </div>

                  <div className="flex items-center justify-between text-slate-300">
                    <span>{L.field.neoplasticCell}</span>
                    <span
                      className={
                        selectedCandidate.vlm.criteria.neoplastic_cell
                          ? "text-emerald-400 font-bold"
                          : "text-rose-400 font-bold"
                      }
                    >
                      {selectedCandidate.vlm.criteria.neoplastic_cell ? "✓" : "✗"}
                    </span>
                  </div>

                  <div className="flex items-center justify-between text-slate-300 pt-1 border-t border-slate-800">
                    <span>{L.field.phase}:</span>
                    <span className="font-mono text-sky-400 capitalize">
                      {selectedCandidate.vlm.criteria.phase}
                    </span>
                  </div>

                  <div className="flex items-center justify-between text-slate-300">
                    <span>{L.field.mimic}:</span>
                    <span className="font-mono text-amber-400 capitalize">
                      {selectedCandidate.vlm.mimic !== "none"
                        ? selectedCandidate.vlm.mimic
                        : "—"}
                    </span>
                  </div>
                </div>

                {selectedCandidate.vlm.rationale && (
                  <p className="text-[11px] text-slate-400 italic bg-slate-950/60 p-2 rounded border border-slate-800">
                    {selectedCandidate.vlm.rationale}
                  </p>
                )}
              </div>
            )}

            {/* Pathologist Review Actions */}
            <div className="p-4 space-y-2 mt-auto">
              <span className="text-xs font-semibold text-slate-400">
                {L.field.overrideReason}:
              </span>
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  onClick={() => handleReviewCandidate(selectedCandidate.id, "mitosis")}
                  className={`py-2 px-3 rounded-lg text-xs font-bold flex items-center justify-center space-x-1.5 transition ${
                    selectedCandidate.review_label === "mitosis"
                      ? "bg-emerald-600 text-white shadow-md shadow-emerald-600/30"
                      : "bg-slate-800 hover:bg-emerald-900/60 text-slate-200 hover:text-emerald-300 border border-slate-700"
                  }`}
                >
                  <Check className="w-3.5 h-3.5" />
                  <span>{L.action.markMitosis}</span>
                </button>

                <button
                  type="button"
                  onClick={() => handleReviewCandidate(selectedCandidate.id, "not_mitosis")}
                  className={`py-2 px-3 rounded-lg text-xs font-bold flex items-center justify-center space-x-1.5 transition ${
                    selectedCandidate.review_label === "not_mitosis"
                      ? "bg-rose-600 text-white shadow-md shadow-rose-600/30"
                      : "bg-slate-800 hover:bg-rose-900/60 text-slate-200 hover:text-rose-300 border border-slate-700"
                  }`}
                >
                  <X className="w-3.5 h-3.5" />
                  <span>{L.action.markNotMitosis}</span>
                </button>
              </div>

              {selectedCandidate.review_label !== null && (
                <button
                  type="button"
                  onClick={() => handleReviewCandidate(selectedCandidate.id, null)}
                  className="w-full py-1.5 text-xs text-slate-400 hover:text-slate-200 hover:bg-slate-800 rounded transition"
                >
                  {L.action.resetView}
                </button>
              )}
            </div>
          </div>
        )}

        {/* Right: Mitosis Candidates Gallery / Queue */}
        <div className="w-96 flex flex-col h-full shrink-0">
          <MitosisGallery
            caseId={caseId}
            candidates={sortedCandidates}
            selectedCandidateId={selectedCandidateId}
            onSelectCandidate={(cand) => setSelectedCandidateId(cand.id)}
            onToggleCandidate={handleReviewCandidate}
            filterMode={filterMode}
            onSetFilterMode={setFilterMode}
            viewMode={imageViewMode}
          />
        </div>

        {/* Collapsible Definition Side Drawer / Panel */}
        {showDefinitionPanel && (
          <div className="fixed inset-y-0 right-0 z-50 w-[440px] bg-slate-900 border-l border-slate-700 shadow-2xl flex flex-col animate-in slide-in-from-right duration-200">
            <div className="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-950">
              <div className="flex items-center space-x-2 text-sky-400 font-bold text-sm">
                <BookOpen className="w-4 h-4" />
                <span>{L.heading.mitosisDefinition}</span>
              </div>
              <button
                type="button"
                onClick={() => setShowDefinitionPanel(false)}
                className="text-slate-400 hover:text-white"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <div className="flex-1 p-5 overflow-y-auto font-mono text-xs text-slate-300 whitespace-pre-wrap leading-relaxed">
              {MITOTIC_FIGURE_DEFINITION}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
