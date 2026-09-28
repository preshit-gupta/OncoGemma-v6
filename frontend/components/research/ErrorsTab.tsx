"use client";

import React, { useState } from "react";
import { FixedSizeList as List } from "react-window";
import { MitosisErrorCard } from "@/lib/api/research";
import { L } from "@/lib/labels";

interface ErrorsTabProps {
  runId: string;
  errors: MitosisErrorCard[];
  onLogIssue?: (card: MitosisErrorCard) => void;
}

export function ErrorsTab({ runId, errors, onLogIssue }: ErrorsTabProps) {
  const [filterKind, setFilterKind] = useState<"all" | "fp" | "fn">("all");

  const filtered = errors.filter((e) => {
    if (filterKind === "all") return true;
    return e.kind === filterKind;
  });

  // Render 2 cards per row for virtualization
  const itemsPerRow = 2;
  const rowCount = Math.ceil(filtered.length / itemsPerRow);

  const Row = ({ index, style }: { index: number; style: React.CSSProperties }) => {
    const startIndex = index * itemsPerRow;
    const rowCards = filtered.slice(startIndex, startIndex + itemsPerRow);

    return (
      <div style={style} className="flex gap-4 px-1 pb-4">
        {rowCards.map((card) => {
          const isFp = card.kind === "fp";

          return (
            <div
              key={`${card.slide_id}_${card.centroid_um[0]}_${card.centroid_um[1]}`}
              className="flex-1 flex flex-col rounded-xl border border-slate-200 bg-white p-4 shadow-sm text-xs"
            >
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <span
                    className={`rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${
                      isFp ? "bg-rose-100 text-rose-800" : "bg-blue-100 text-blue-800"
                    }`}
                  >
                    {card.kind.toUpperCase()}
                  </span>
                  <span className="font-mono font-bold text-slate-800">
                    {card.slide_id}
                  </span>
                </div>
                <button
                  onClick={() => onLogIssue?.(card)}
                  className="rounded-lg bg-indigo-50 border border-indigo-200 px-2.5 py-1 text-[11px] font-semibold text-indigo-700 hover:bg-indigo-100 transition"
                >
                  {L.action.logIssue}
                </button>
              </div>

              {/* Crop with Ground Truth ○ and Prediction × markers */}
              <div className="relative aspect-square w-full overflow-hidden rounded-lg border border-slate-200 bg-slate-50 mb-3">
                <img
                  src={card.crop_url}
                  alt={card.slide_id}
                  className="h-full w-full object-cover"
                />

                {/* Ground Truth marker ○ (green circle) */}
                {card.gt_points_um.map((pt, i) => (
                  <div
                    key={`gt_${i}`}
                    className="absolute h-5 w-5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-emerald-400 bg-emerald-400/20 shadow"
                    style={{ left: "50%", top: "50%" }}
                  />
                ))}

                {/* Prediction marker × (red cross) */}
                {card.pred_points_um.map((pt, i) => (
                  <div
                    key={`pred_${i}`}
                    className="absolute -translate-x-1/2 -translate-y-1/2 font-bold text-base text-rose-500 drop-shadow"
                    style={{ left: "50%", top: "50%" }}
                  >
                    {"✕"}
                  </div>
                ))}

                <div className="absolute bottom-2 left-2 rounded bg-black/60 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                  {"64 "}{L.unit.um}
                </div>
              </div>

              {/* Metadata chips */}
              <div className="grid grid-cols-2 gap-2 text-[11px] text-slate-600 mb-2">
                <div className="flex justify-between">
                  <span>{L.field.detectorProb}</span>
                  <strong className="text-slate-800 font-mono">
                    {card.p_a !== null ? card.p_a.toFixed(2) : "—"}
                  </strong>
                </div>
                <div className="flex justify-between">
                  <span>{L.field.classifierProb}</span>
                  <strong className="text-slate-800 font-mono">
                    {card.p_b !== null ? card.p_b.toFixed(2) : "—"}
                  </strong>
                </div>
              </div>

              <div className="flex items-center justify-between pt-2 border-t border-slate-100 text-[10px]">
                <span className="font-semibold text-slate-700">
                  {card.vlm_verdict || "NO_VLM"}
                </span>
                {card.rule_override && (
                  <span className="rounded bg-purple-100 px-1.5 py-0.2 text-[9px] font-bold text-purple-700">
                    {L.field.ruleOverride}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  return (
    <div className="flex flex-col gap-4 text-xs">
      {/* Filter Kind bar */}
      <div className="flex items-center gap-3 bg-white p-3 rounded-xl border border-slate-200">
        <span className="font-semibold text-slate-600">{L.field.category}{":"}</span>
        <div className="flex gap-2">
          {(["all", "fp", "fn"] as const).map((k) => (
            <button
              key={k}
              onClick={() => setFilterKind(k)}
              className={`rounded-lg px-3 py-1 font-semibold uppercase transition ${
                filterKind === k
                  ? "bg-indigo-600 text-white"
                  : "bg-slate-100 text-slate-600 hover:bg-slate-200"
              }`}
            >
              {k}
            </button>
          ))}
        </div>
        <span className="text-slate-400 ml-auto">
          {"Showing "}{filtered.length}{" errors (100 per page)"}
        </span>
      </div>

      {/* Virtualised list of cards */}
      <div className="h-[620px] w-full">
        {filtered.length === 0 ? (
          <div className="p-8 text-center text-slate-400 italic">{"No errors found"}</div>
        ) : (
          <List
            height={620}
            itemCount={rowCount}
            itemSize={340}
            width="100%"
          >
            {Row}
          </List>
        )}
      </div>
    </div>
  );
}
