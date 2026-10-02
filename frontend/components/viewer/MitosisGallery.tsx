"use client";

import React, { useEffect, useRef } from "react";
import { 
  CheckCircle2, 
  XCircle, 
  HelpCircle, 
  Layers, 
  Filter,
  Check,
  X,
  RotateCcw
} from "lucide-react";
import { Candidate } from "@/lib/api/mitosis";
import { L } from "@/lib/labels";

interface MitosisGalleryProps {
  caseId: string;
  candidates: Candidate[];
  selectedCandidateId: string | null;
  onSelectCandidate: (candidate: Candidate) => void;
  onToggleCandidate: (id: string, newLabel: "mitosis" | "not_mitosis" | null) => void;
  filterMode: "all" | "equivocal" | "mitosis" | "not_mitosis";
  onSetFilterMode: (mode: "all" | "equivocal" | "mitosis" | "not_mitosis") => void;
  fieldSeq?: number;
  totalFields?: number;
  onApproveFieldAndNext?: () => void;
  viewMode?: "crop" | "context";
}

export function MitosisGallery({
  candidates,
  selectedCandidateId,
  onSelectCandidate,
  onToggleCandidate,
  filterMode,
  onSetFilterMode,
  fieldSeq = 1,
  viewMode = "crop",
}: MitosisGalleryProps) {
  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({});

  const filteredCandidates = candidates.filter((c) => {
    if (filterMode === "all") return true;
    if (filterMode === "equivocal") {
      return c.final_decision === "equivocal" && c.review_label === null;
    }
    const effective = c.review_label ?? c.final_decision;
    return effective === filterMode;
  });

  const equivocalCount = candidates.filter(
    (c) => c.final_decision === "equivocal" && c.review_label === null
  ).length;
  const mitosisCount = candidates.filter(
    (c) => (c.review_label ?? c.final_decision) === "mitosis"
  ).length;
  const rejectedCount = candidates.filter(
    (c) => (c.review_label ?? c.final_decision) === "not_mitosis"
  ).length;

  // Auto-scroll selected card into view
  useEffect(() => {
    if (selectedCandidateId && cardRefs.current[selectedCandidateId]) {
      cardRefs.current[selectedCandidateId]?.scrollIntoView({
        behavior: "smooth",
        block: "nearest",
      });
    }
  }, [selectedCandidateId]);

  return (
    <div className="flex flex-col h-full bg-slate-900 border-l border-slate-800 text-slate-100 select-none">
      {/* Header & Filter Tabs */}
      <div className="p-3 border-b border-slate-800 bg-slate-950/60 shrink-0">
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-2">
            <Layers className="w-4 h-4 text-emerald-400" />
            <span className="font-semibold text-xs tracking-wider uppercase text-slate-300">
              {L.field.fieldNumber} #{fieldSeq} {L.heading.mitoticCandidates}
            </span>
          </div>
          <span className="text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-300 font-mono">
            {filteredCandidates.length} / {candidates.length}
          </span>
        </div>

        {/* Filter Pills */}
        <div className="grid grid-cols-4 gap-1 p-1 bg-slate-900/90 rounded-lg text-[11px] font-medium">
          <button
            type="button"
            onClick={() => onSetFilterMode("all")}
            className={`py-1 rounded px-1.5 transition-all text-center ${
              filterMode === "all"
                ? "bg-slate-700 text-white font-semibold shadow-sm"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {L.action.filterAll} ({candidates.length})
          </button>
          <button
            type="button"
            onClick={() => onSetFilterMode("equivocal")}
            className={`py-1 rounded px-1.5 transition-all text-center ${
              filterMode === "equivocal"
                ? "bg-amber-600/40 text-amber-300 font-semibold border border-amber-500/30"
                : "text-slate-400 hover:text-amber-300"
            }`}
          >
            {L.field.equivocal} ({equivocalCount})
          </button>
          <button
            type="button"
            onClick={() => onSetFilterMode("mitosis")}
            className={`py-1 rounded px-1.5 transition-all text-center ${
              filterMode === "mitosis"
                ? "bg-emerald-600/40 text-emerald-300 font-semibold border border-emerald-500/30"
                : "text-slate-400 hover:text-emerald-300"
            }`}
          >
            {L.action.markMitosis} ({mitosisCount})
          </button>
          <button
            type="button"
            onClick={() => onSetFilterMode("not_mitosis")}
            className={`py-1 rounded px-1.5 transition-all text-center ${
              filterMode === "not_mitosis"
                ? "bg-slate-700/40 text-slate-300 font-semibold border border-slate-600/30"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {L.action.filterRejected} ({rejectedCount})
          </button>
        </div>

        {/* Keyboard Shortcut Tips */}
        <div className="mt-2 flex items-center justify-between text-[10px] text-slate-400 bg-slate-900/50 px-2 py-1 rounded border border-slate-800/80">
          <span>
            <kbd className="px-1 py-0.5 bg-emerald-950 text-emerald-300 border border-emerald-700/50 rounded font-mono">
              {"M"}
            </kbd>{" "}
            {L.action.markMitosis}
          </span>
          <span>
            <kbd className="px-1 py-0.5 bg-rose-950 text-rose-300 border border-rose-700/50 rounded font-mono">
              {"X"}
            </kbd>{" "}
            {L.action.markNotMitosis}
          </span>
          <span>
            <kbd className="px-1 py-0.5 bg-slate-800 text-slate-300 rounded font-mono">
              {"U"}
            </kbd>{" "}
            {L.action.resetView}
          </span>
          <span>
            <kbd className="px-1.5 py-0.5 bg-slate-800 rounded text-slate-200 font-mono">
              {"Space"}
            </kbd>{" "}
            {L.field.zoom}
          </span>
        </div>
      </div>

      {/* Candidates Scroll List */}
      <div className="flex-1 overflow-y-auto p-2 space-y-2">
        {filteredCandidates.length === 0 ? (
          <div className="h-48 flex flex-col items-center justify-center text-slate-500 text-xs">
            <Filter className="w-6 h-6 mb-2 stroke-[1.5] text-slate-600" />
            {L.help.noMitoses}
          </div>
        ) : (
          filteredCandidates.map((cand) => {
            const isSelected = cand.id === selectedCandidateId;
            const effectiveVerdict = cand.review_label ?? cand.final_decision;
            const isMitosis = effectiveVerdict === "mitosis";
            const isEquivocal = effectiveVerdict === "equivocal";
            const isRejected = effectiveVerdict === "not_mitosis";

            const imgSrc = viewMode === "context" ? cand.context_url : cand.crop_url;

            return (
              <div
                key={cand.id}
                ref={(el) => { cardRefs.current[cand.id] = el; }}
                onClick={() => onSelectCandidate(cand)}
                className={`relative rounded-lg p-2.5 transition-all cursor-pointer border ${
                  isSelected
                    ? "bg-slate-800 border-sky-500 shadow-md ring-1 ring-sky-500/50"
                    : isEquivocal
                    ? "bg-amber-950/20 border-amber-700/40 hover:bg-amber-950/30"
                    : isMitosis
                    ? "bg-emerald-950/20 border-emerald-800/40 hover:bg-emerald-950/30"
                    : "bg-slate-900/50 border-slate-800/50 opacity-70 hover:opacity-100"
                }`}
              >
                <div className="flex items-center gap-3">
                  {/* Crop / Context Thumbnail */}
                  <div className="relative w-16 h-16 rounded overflow-hidden bg-black shrink-0 border border-slate-700/80">
                    <img
                      src={imgSrc}
                      alt={cand.id}
                      className="w-full h-full object-cover"
                      loading="lazy"
                    />
                  </div>

                  {/* Candidate Details */}
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-mono text-xs font-semibold text-slate-200">
                        {cand.id}
                      </span>
                      <div className="flex items-center space-x-1">
                        {cand.counted && (
                          <span className="text-[9px] px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-300 border border-emerald-700/50 font-bold uppercase font-mono">
                            {L.field.counted}
                          </span>
                        )}
                        {cand.review_label ? (
                          <span
                            className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                              cand.review_label === "mitosis"
                                ? "bg-emerald-900/80 text-emerald-200 border border-emerald-700"
                                : "bg-rose-900/80 text-rose-200 border border-rose-700"
                            }`}
                          >
                            {cand.review_label === "mitosis"
                              ? L.action.markMitosis
                              : L.action.markNotMitosis}
                          </span>
                        ) : isEquivocal ? (
                          <span className="flex items-center gap-1 text-[10px] text-amber-300 bg-amber-950/80 px-1.5 py-0.5 rounded border border-amber-700/50 font-medium">
                            <HelpCircle className="w-3 h-3 text-amber-400" />
                            {L.field.equivocal}
                          </span>
                        ) : isMitosis ? (
                          <span className="flex items-center gap-1 text-[10px] text-emerald-300 bg-emerald-950/80 px-1.5 py-0.5 rounded border border-emerald-700/40 font-medium">
                            <CheckCircle2 className="w-3 h-3 text-emerald-400" />
                            {L.action.markMitosis}
                          </span>
                        ) : (
                          <span className="flex items-center gap-1 text-[10px] text-slate-400 bg-slate-800 px-1.5 py-0.5 rounded border border-slate-700/40">
                            <XCircle className="w-3 h-3 text-slate-500" />
                            {L.action.markNotMitosis}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="flex items-center gap-2 text-[10px] text-slate-400 font-mono">
                      <span>
                        {L.field.detectorProb}:{" "}
                        <strong className="text-slate-300">
                          {cand.p_a != null ? cand.p_a.toFixed(2) : "—"}
                        </strong>
                      </span>
                      <span>
                        {L.field.classifierProb}:{" "}
                        <strong className="text-slate-300">
                          {cand.p_b != null ? cand.p_b.toFixed(2) : "—"}
                        </strong>
                      </span>
                      <span className="text-slate-500">[{cand.decision_path}]</span>
                    </div>

                    {/* Quick review action buttons */}
                    <div className="mt-2 flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onToggleCandidate(cand.id, "mitosis");
                        }}
                        className={`px-2 py-0.5 rounded text-[10px] font-semibold flex items-center gap-1 transition ${
                          cand.review_label === "mitosis"
                            ? "bg-emerald-600 text-white"
                            : "bg-slate-800 text-slate-300 hover:bg-emerald-900 hover:text-emerald-200"
                        }`}
                      >
                        <Check className="w-3 h-3" />
                        <span>{L.action.markMitosis}</span>
                      </button>

                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onToggleCandidate(cand.id, "not_mitosis");
                        }}
                        className={`px-2 py-0.5 rounded text-[10px] font-semibold flex items-center gap-1 transition ${
                          cand.review_label === "not_mitosis"
                            ? "bg-rose-600 text-white"
                            : "bg-slate-800 text-slate-300 hover:bg-rose-900 hover:text-rose-200"
                        }`}
                      >
                        <X className="w-3 h-3" />
                        <span>{L.action.markNotMitosis}</span>
                      </button>

                      {cand.review_label !== null && (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            onToggleCandidate(cand.id, null);
                          }}
                          className="px-1.5 py-0.5 rounded text-[10px] bg-slate-800 text-slate-400 hover:text-slate-200 hover:bg-slate-700 transition"
                          title={L.action.resetView}
                        >
                          <RotateCcw className="w-3 h-3" />
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
