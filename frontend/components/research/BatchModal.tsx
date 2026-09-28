"use client";

import React, { useState } from "react";
import { L } from "@/lib/labels";
import { createBatch, cancelBatch, retryBatch } from "@/lib/api/research";

interface BatchModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

export function BatchModal({ isOpen, onClose, onSuccess }: BatchModalProps) {
  const [name, setName] = useState<string>("TCGA Evaluation Batch");
  const [manifestUri, setManifestUri] = useState<string>("gs://oncogemma-eval/manifests/val_v2.json");
  const [stages, setStages] = useState<string[]>(["triage", "mitosis", "grading"]);
  const [mode, setMode] = useState<string>("clinical");
  const [concurrency, setConcurrency] = useState<number>(4);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const [batchProgress, setBatchProgress] = useState<{
    succeeded: number;
    failed: number;
    running: number;
    total: number;
  } | null>(null);

  if (!isOpen) return null;

  const handleStageToggle = (stage: string) => {
    if (stages.includes(stage)) {
      setStages(stages.filter((s) => s !== stage));
    } else {
      setStages([...stages, stage]);
    }
  };

  const handleStartBatch = async () => {
    if (!name.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await createBatch({
        name,
        source: { manifest_uri: manifestUri },
        stages,
        mode,
        concurrency,
      });
      setActiveBatchId(res.batch_id);
      setBatchProgress({ succeeded: 0, failed: 0, running: 10, total: 10 });

      // Simulated SSE progress in mock mode
      let succ = 0;
      let fail = 0;
      const interval = setInterval(() => {
        succ += 2;
        if (succ + fail >= 10) {
          clearInterval(interval);
          setBatchProgress({ succeeded: 9, failed: 1, running: 0, total: 10 });
        } else {
          setBatchProgress({
            succeeded: succ,
            failed: fail,
            running: 10 - succ - fail,
            total: 10,
          });
        }
      }, 1000);

      onSuccess?.();
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = async () => {
    if (!activeBatchId) return;
    await cancelBatch(activeBatchId);
    setActiveBatchId(null);
    setBatchProgress(null);
  };

  const handleRetryFailed = async () => {
    if (!activeBatchId) return;
    await retryBatch(activeBatchId, { statuses: ["failed"] });
    setBatchProgress((prev) => (prev ? { ...prev, failed: 0, running: 1 } : null));
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4">
      <div className="flex w-full max-w-lg flex-col rounded-2xl bg-white p-6 shadow-xl border border-slate-200 animate-in fade-in zoom-in-95">
        <div className="flex items-center justify-between pb-3 border-b border-slate-100">
          <h3 className="text-sm font-bold text-slate-900">{L.heading.newBatch}</h3>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-600 font-bold text-sm"
          >
            {"✕"}
          </button>
        </div>

        {error && (
          <div className="mt-3 rounded-lg bg-rose-50 p-3 text-xs text-rose-700 font-medium">
            {error}
          </div>
        )}

        {activeBatchId ? (
          <div className="flex flex-col gap-4 py-4 text-xs">
            <div className="flex justify-between items-center">
              <span className="font-semibold text-slate-700">{L.heading.batchProgress}</span>
              <span className="font-mono text-slate-500">{activeBatchId}</span>
            </div>

            {batchProgress && (
              <div className="flex flex-col gap-2">
                <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full bg-emerald-500 transition-all duration-300"
                    style={{
                      width: `${(batchProgress.succeeded / batchProgress.total) * 100}%`,
                    }}
                  />
                </div>
                <div className="flex justify-between text-slate-600">
                  <span>
                    {L.status.done}{": "}
                    <strong>{batchProgress.succeeded}</strong>
                  </span>
                  <span>
                    {L.status.running}{": "}
                    <strong>{batchProgress.running}</strong>
                  </span>
                  <span>
                    {L.status.failed}{": "}
                    <strong className="text-rose-600">{batchProgress.failed}</strong>
                  </span>
                </div>
              </div>
            )}

            <div className="flex items-center justify-end gap-2 pt-3 border-t border-slate-100">
              {batchProgress?.failed && batchProgress.failed > 0 ? (
                <button
                  onClick={handleRetryFailed}
                  className="rounded-lg bg-amber-600 px-3 py-1.5 font-semibold text-white hover:bg-amber-700 transition"
                >
                  {L.action.retryFailed}
                </button>
              ) : null}
              <button
                onClick={handleCancel}
                className="rounded-lg border border-slate-300 px-3 py-1.5 font-semibold text-slate-700 hover:bg-slate-50 transition"
              >
                {L.action.cancelBatch}
              </button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-4 py-4 text-xs">
            <div className="flex flex-col gap-1">
              <label className="font-semibold text-slate-700">{L.field.runName}</label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="rounded-lg border border-slate-300 p-2 text-slate-800"
              />
            </div>

            <div className="flex flex-col gap-1">
              <label className="font-semibold text-slate-700">{"Manifest URI"}</label>
              <input
                type="text"
                value={manifestUri}
                onChange={(e) => setManifestUri(e.target.value)}
                className="rounded-lg border border-slate-300 p-2 text-slate-800 font-mono text-[11px]"
              />
            </div>

            <div className="flex flex-col gap-1">
              <label className="font-semibold text-slate-700">{L.heading.stages}</label>
              <div className="flex gap-2">
                {["triage", "mitosis", "grading"].map((st) => (
                  <button
                    key={st}
                    type="button"
                    onClick={() => handleStageToggle(st)}
                    className={`rounded px-3 py-1.5 text-xs font-semibold capitalize transition ${
                      stages.includes(st)
                        ? "bg-indigo-600 text-white"
                        : "border border-slate-300 bg-white text-slate-700"
                    }`}
                  >
                    {st}
                  </button>
                ))}
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{L.field.runMode}</label>
                <select
                  value={mode}
                  onChange={(e) => setMode(e.target.value)}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800"
                >
                  <option value="clinical">{"clinical"}</option>
                  <option value="eval">{"eval"}</option>
                  <option value="shadow">{"shadow"}</option>
                </select>
              </div>

              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{"Concurrency"}</label>
                <input
                  type="number"
                  min={1}
                  max={16}
                  value={concurrency}
                  onChange={(e) => setConcurrency(Number(e.target.value))}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800"
                />
              </div>
            </div>

            {/* Dry-run estimate display */}
            <div className="rounded-lg bg-slate-50 p-3 border border-slate-100 flex flex-col gap-1 text-[11px] text-slate-600">
              <span className="font-semibold text-slate-700">{"Dry-run estimate"}</span>
              <span>{"Estimated calls: ~850 model invocations"}</span>
              <span>{"Estimated cost: $1.20 / slide (~$24.00 total)"}</span>
            </div>

            <div className="flex items-center justify-end gap-2 pt-2 border-t border-slate-100">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg px-3 py-1.5 font-semibold text-slate-600 hover:bg-slate-100"
              >
                {L.action.cancel}
              </button>
              <button
                type="button"
                onClick={handleStartBatch}
                disabled={loading || stages.length === 0}
                className="rounded-lg bg-indigo-600 px-4 py-1.5 font-semibold text-white hover:bg-indigo-700"
              >
                {L.action.newBatch}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
