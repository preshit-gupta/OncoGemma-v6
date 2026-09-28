"use client";

import React from "react";
import {
  ScatterChart,
  Scatter,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Line,
  ComposedChart,
} from "recharts";
import { Reliability, MetricsV1 } from "@/lib/api/research";
import { PALETTE } from "@/components/research/palette";
import { L } from "@/lib/labels";

interface CalibrationTabProps {
  metrics: MetricsV1;
}

function ReliabilityChart({
  chartTitle,
  reliability,
}: {
  chartTitle: string;
  reliability?: Reliability;
}) {
  if (!reliability) {
    return (
      <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs">
        <h4 className="font-bold text-slate-800 mb-2">{chartTitle}</h4>
        <p className="text-slate-400 italic">{"No data"}</p>
      </div>
    );
  }

  const { bins, ece } = reliability;
  const totalN = bins.reduce((sum, b) => sum + b.n, 0);

  // Diagonal reference points
  const chartData = bins.map((b) => ({
    x: Number(b.p_mean.toFixed(2)),
    y: Number(b.frac_pos.toFixed(2)),
    n: b.n,
    diagonal: Number(b.p_mean.toFixed(2)),
  }));

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs flex flex-col gap-3">
      <div className="flex items-center justify-between border-b border-slate-100 pb-2">
        <h4 className="font-bold text-slate-900">{chartTitle}</h4>
        <div className="flex items-center gap-3">
          <span className="font-mono text-[11px] bg-slate-100 px-2 py-0.5 rounded text-slate-700">
            {L.field.ece}{": "}<strong>{ece.toFixed(3)}</strong>
          </span>
          <span className="text-[11px] text-slate-400 font-mono">
            {"(n="}{totalN}{")"}
          </span>
        </div>
      </div>

      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={chartData} margin={{ top: 10, right: 30, left: 0, bottom: 20 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={PALETTE.grid} />
            <XAxis
              dataKey="x"
              domain={[0, 1]}
              type="number"
              label={{ value: "Predicted Probability", position: "insideBottom", offset: -10 }}
              tick={{ fontSize: 11 }}
            />
            <YAxis
              domain={[0, 1]}
              label={{ value: "Observed Fraction Positive", angle: -90, position: "insideLeft" }}
              tick={{ fontSize: 11 }}
            />
            <Tooltip
              formatter={(value: any, name: string) => [value, name === "y" ? "Observed" : "Diagonal"]}
            />
            <Line
              type="monotone"
              dataKey="diagonal"
              name="Ideal (Diagonal)"
              stroke={PALETTE.neutral}
              strokeDasharray="4 4"
              dot={false}
              isAnimationActive={false}
            />
            <Scatter
              dataKey="y"
              name="Observed"
              fill={PALETTE.primary}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function CalibrationTab({ metrics }: CalibrationTabProps) {
  const { calibration } = metrics;

  return (
    <div className="flex flex-col gap-6 text-xs">
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        <ReliabilityChart
          chartTitle="Detector Calibration (p_a)"
          reliability={calibration.p_a}
        />
        <ReliabilityChart
          chartTitle="Classifier Calibration (p_b)"
          reliability={calibration.p_b}
        />
        <ReliabilityChart
          chartTitle="Tumor Tile Mask Calibration (p_tumor)"
          reliability={calibration.p_tumor}
        />
      </div>
    </div>
  );
}
