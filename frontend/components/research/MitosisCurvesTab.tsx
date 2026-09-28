"use client";

import React, { useState, useEffect } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceDot,
  Legend,
} from "recharts";
import { MetricsV1, getRunMitosisCurves } from "@/lib/api/research";
import { PALETTE } from "@/components/research/palette";
import { L } from "@/lib/labels";

interface MitosisCurvesTabProps {
  runId: string;
  metrics: MetricsV1;
}

export function MitosisCurvesTab({ runId, metrics }: MitosisCurvesTabProps) {
  const [tauA, setTauA] = useState<number>(0.5);
  const [tauB, setTauB] = useState<number>(0.5);
  const [whatIf, setWhatIf] = useState<{
    f1: number;
    precision: number;
    recall: number;
  } | null>(null);

  const curves = metrics.curves.mitosis_pr;

  useEffect(() => {
    getRunMitosisCurves(runId, { tau_a: tauA, tau_b: tauB }).then((res) => {
      if (res.what_if) {
        setWhatIf(res.what_if);
      }
    });
  }, [runId, tauA, tauB]);

  // Format PR curve data for recharts
  const chartData = curves
    ? curves.recall.map((r, i) => ({
        recall: Number(r.toFixed(2)),
        precision: Number(curves.precision[i].toFixed(2)),
        f1: Number(curves.f1[i].toFixed(2)),
        threshold: curves.thresholds[i],
      }))
    : [];

  return (
    <div className="flex flex-col gap-6 text-xs">
      {/* What-if interactive panel */}
      <div className="rounded-xl border border-indigo-200 bg-indigo-50/50 p-5 shadow-sm">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <span className="font-bold text-slate-900">{"Threshold Sweep (Val What-If)"}</span>
            <span className="rounded bg-indigo-100 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
              {"val what-if"}
            </span>
          </div>
          <span className="text-[11px] text-slate-500">
            {L.help.valWhatIfHelp}
          </span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-4">
          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between font-medium text-slate-700">
              <span>{"Detector Threshold τ_a"}</span>
              <span className="font-bold text-indigo-600">{tauA.toFixed(2)}</span>
            </div>
            <input
              type="range"
              min={0.1}
              max={0.9}
              step={0.05}
              value={tauA}
              onChange={(e) => setTauA(Number(e.target.value))}
              className="accent-indigo-600 w-full"
            />
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex justify-between font-medium text-slate-700">
              <span>{"Classifier Threshold τ_b"}</span>
              <span className="font-bold text-indigo-600">{tauB.toFixed(2)}</span>
            </div>
            <input
              type="range"
              min={0.1}
              max={0.9}
              step={0.05}
              value={tauB}
              onChange={(e) => setTauB(Number(e.target.value))}
              className="accent-indigo-600 w-full"
            />
          </div>
        </div>

        {whatIf && (
          <div className="flex items-center gap-6 rounded-lg bg-white p-3 border border-indigo-100 text-xs">
            <div>
              <span className="text-slate-400 block text-[10px]">{"Simulated F1"}</span>
              <span className="text-base font-bold text-slate-900">{whatIf.f1.toFixed(2)}</span>
            </div>
            <div>
              <span className="text-slate-400 block text-[10px]">{"Simulated Precision"}</span>
              <span className="text-base font-bold text-slate-900">{whatIf.precision.toFixed(2)}</span>
            </div>
            <div>
              <span className="text-slate-400 block text-[10px]">{"Simulated Recall"}</span>
              <span className="text-base font-bold text-slate-900">{whatIf.recall.toFixed(2)}</span>
            </div>
            <span className="text-slate-400 text-[10px] ml-auto">
              {"(n=30 cases)"}
            </span>
          </div>
        )}
      </div>

      {/* PR Curve Chart */}
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="flex items-center justify-between mb-4 border-b border-slate-100 pb-2">
          <h4 className="font-bold text-slate-900">{"Precision-Recall Curve (n=30)"}</h4>
          <span className="text-[11px] text-slate-500 font-mono">
            {"Operating Point: (R=0.68, P=0.74, F1=0.71)"}
          </span>
        </div>

        <div className="h-72 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData} margin={{ top: 10, right: 30, left: 0, bottom: 20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={PALETTE.grid} />
              <XAxis
                dataKey="recall"
                label={{ value: "Recall", position: "insideBottom", offset: -10 }}
                type="number"
                domain={[0, 1]}
                tick={{ fontSize: 11 }}
              />
              <YAxis
                label={{ value: "Precision", angle: -90, position: "insideLeft" }}
                domain={[0, 1]}
                tick={{ fontSize: 11 }}
              />
              <Tooltip />
              <Legend verticalAlign="top" height={36} />
              <Line
                type="monotone"
                dataKey="precision"
                name="PR Curve"
                stroke={PALETTE.primary}
                strokeWidth={2}
                dot={{ r: 3 }}
              />
              <Line
                type="monotone"
                dataKey="f1"
                name="F1"
                stroke={PALETTE.secondary}
                strokeWidth={2}
                strokeDasharray="4 4"
                dot={false}
              />
              <ReferenceDot
                x={0.68}
                y={0.74}
                r={6}
                fill={PALETTE.accent1}
                stroke="#fff"
                strokeWidth={2}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
