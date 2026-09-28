"use client";

import React, { useState } from "react";
import { Confusion, MetricsV1 } from "@/lib/api/research";
import { L } from "@/lib/labels";

interface ConfusionTabProps {
  metrics: MetricsV1;
  onSelectCell?: (component: "grade" | "tubule" | "pleo" | "mitotic", trueLabel: any, predLabel: any) => void;
}

function MatrixTable({
  matrixTitle,
  confusion,
  onSelectCell,
  component,
}: {
  matrixTitle: string;
  confusion?: Confusion;
  onSelectCell?: (component: "grade" | "tubule" | "pleo" | "mitotic", trueLabel: any, predLabel: any) => void;
  component: "grade" | "tubule" | "pleo" | "mitotic";
}) {
  if (!confusion || !confusion.matrix || confusion.matrix.length === 0) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs">
        <h4 className="font-bold text-slate-800 mb-2">{matrixTitle}</h4>
        <p className="text-slate-400 italic">{"No data"}</p>
      </div>
    );
  }

  const { labels, matrix } = confusion;

  // Find max value in matrix for shading scale
  let maxVal = 1;
  for (const row of matrix) {
    for (const val of row) {
      if (val > maxVal) maxVal = val;
    }
  }

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs">
      <div className="flex items-center justify-between mb-4">
        <h4 className="font-bold text-slate-900">{matrixTitle}</h4>
        <span className="text-[10px] text-slate-400">
          {"Rows = True, Cols = Pred"}
        </span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-center border-collapse text-xs">
          <thead>
            <tr>
              <th className="p-2 text-slate-400 text-left">{"True \\ Pred"}</th>
              {labels.map((l) => (
                <th key={String(l)} className="p-2 font-bold text-slate-700">
                  {l === "none" ? "None" : `Class ${l}`}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {matrix.map((row, rIdx) => {
              const trueLabel = labels[rIdx];
              return (
                <tr key={String(trueLabel)}>
                  <td className="p-2 font-bold text-slate-700 text-left">
                    {trueLabel === "none" ? "None" : `Class ${trueLabel}`}
                  </td>
                  {row.map((val, cIdx) => {
                    const predLabel = labels[cIdx];
                    const isDiagonal = rIdx === cIdx;
                    const intensity = Math.min(1, val / maxVal);
                    const bgColor =
                      val === 0
                        ? "bg-slate-50"
                        : isDiagonal
                        ? `rgba(16, 185, 129, ${0.15 + intensity * 0.7})`
                        : `rgba(239, 68, 68, ${0.15 + intensity * 0.7})`;

                    return (
                      <td
                        key={String(predLabel)}
                        onClick={() => onSelectCell?.(component, trueLabel, predLabel)}
                        className={`p-3 border border-slate-100 font-semibold cursor-pointer hover:ring-2 hover:ring-indigo-400 transition ${
                          val > 0 && isDiagonal
                            ? "text-emerald-950 font-bold"
                            : val > 0
                            ? "text-rose-950 font-bold"
                            : "text-slate-400"
                        }`}
                        style={{ backgroundColor: bgColor }}
                      >
                        {val}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ConfusionTab({ metrics, onSelectCell }: ConfusionTabProps) {
  const [selectedComp, setSelectedComp] = useState<"grade" | "tubule" | "pleo" | "mitotic">("grade");
  const { confusion } = metrics;

  return (
    <div className="flex flex-col gap-6 text-xs">
      {/* Component selector */}
      <div className="flex gap-2 border-b border-slate-200 pb-2">
        {(["grade", "tubule", "pleo", "mitotic"] as const).map((c) => (
          <button
            key={c}
            onClick={() => setSelectedComp(c)}
            className={`px-3 py-1.5 rounded-lg font-semibold capitalize transition ${
              selectedComp === c
                ? "bg-indigo-600 text-white"
                : "bg-white text-slate-600 border border-slate-200 hover:bg-slate-50"
            }`}
          >
            {c === "grade" ? L.field.grade : c === "tubule" ? L.field.tubuleScore : c === "pleo" ? L.field.pleoScore : L.field.mitosisScore}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {selectedComp === "grade" && (
          <MatrixTable
            matrixTitle="Nottingham Grade (G1 / G2 / G3 / None)"
            confusion={confusion.grade}
            component="grade"
            onSelectCell={onSelectCell}
          />
        )}
        {selectedComp === "tubule" && (
          <MatrixTable
            matrixTitle="Tubule Formation Score (1 / 2 / 3 / None)"
            confusion={confusion.tubule}
            component="tubule"
            onSelectCell={onSelectCell}
          />
        )}
        {selectedComp === "pleo" && (
          <MatrixTable
            matrixTitle="Nuclear Pleomorphism Score (1 / 2 / 3 / None)"
            confusion={confusion.pleo}
            component="pleo"
            onSelectCell={onSelectCell}
          />
        )}
        {selectedComp === "mitotic" && (
          <MatrixTable
            matrixTitle="Mitotic Score (1 / 2 / 3 / None)"
            confusion={confusion.mitotic}
            component="mitotic"
            onSelectCell={onSelectCell}
          />
        )}
      </div>
    </div>
  );
}
