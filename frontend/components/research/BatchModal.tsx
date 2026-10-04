"use client";

import React, { useEffect, useRef, useState } from "react";
import { L } from "@/lib/labels";
import { useAuth } from "@/lib/auth/AuthProvider";
import {
  BatchEvent,
  BatchStatus,
  CreateBatchPayload,
  PIPELINE_STAGES,
  RUN_END_STAGES,
  RunEndStage,
  createBatch,
  cancelBatch,
  retryBatch,
  stagesThrough,
  subscribeBatchEvents,
} from "@/lib/api/research";

interface BatchModalProps {
  isOpen: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

type Split = "train" | "val" | "test";

const BATCH_STATUS_LABEL: Record<BatchStatus, string> = {
  created: L.status.pending,
  running: L.status.running,
  completed: L.status.done,
  cancelled: L.status.cancelled,
  failed: L.status.failed,
};

export function BatchModal({ isOpen, onClose, onSuccess }: BatchModalProps) {
  const { can } = useAuth();
  const [name, setName] = useState<string>("");
  const [manifestUri, setManifestUri] = useState<string>("");
  const [split, setSplit] = useState<Split>("val");
  const [testAccessReason, setTestAccessReason] = useState<string>("");
  const [endStage, setEndStage] = useState<RunEndStage>("grading");
  const [mode, setMode] = useState<"auto" | "manual">("auto");
  const [concurrency, setConcurrency] = useState<number>(1);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const [activeBatchId, setActiveBatchId] = useState<string | null>(null);
  const [batchEvent, setBatchEvent] = useState<BatchEvent | null>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);

  const stopProgress = () => {
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
  };

  const watchProgress = (batchId: string) => {
    stopProgress();
    unsubscribeRef.current = subscribeBatchEvents(batchId, setBatchEvent, setError);
  };

  useEffect(() => stopProgress, []);

  if (!isOpen) return null;

  const stages = stagesThrough(endStage);
  const manifestValid = manifestUri.trim().startsWith("gs://");
  const testReasonValid = split !== "test" || testAccessReason.trim().length > 0;
  const canSubmit = !loading && name.trim().length > 0 && manifestValid && testReasonValid;

  const handleStartBatch = async () => {
    if (!canSubmit) return;
    setLoading(true);
    setError(null);
    try {
      const payload: CreateBatchPayload = {
        name: name.trim(),
        source:
          split === "test"
            ? { manifest_uri: manifestUri.trim(), split, confirm_test_access: testAccessReason.trim() }
            : { manifest_uri: manifestUri.trim(), split },
        stages,
        mode,
        concurrency,
      };
      const res = await createBatch(payload);
      setActiveBatchId(res.batch_id);
      setBatchEvent(null);
      watchProgress(res.batch_id);
      onSuccess?.();
    } catch (err: any) {
      setError(err.message || String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = async () => {
    if (!activeBatchId) return;
    setError(null);
    try {
      await cancelBatch(activeBatchId);
    } catch (err: any) {
      setError(err.message || String(err));
      return;
    }
    stopProgress();
    setActiveBatchId(null);
    setBatchEvent(null);
  };

  const handleRetryFailed = async () => {
    if (!activeBatchId) return;
    setError(null);
    try {
      await retryBatch(activeBatchId, { statuses: ["failed"] });
    } catch (err: any) {
      setError(err.message || String(err));
      return;
    }
    watchProgress(activeBatchId);
  };

  const counts = batchEvent?.counts ?? {};
  const total = Object.values(counts).reduce((sum, n) => sum + (n ?? 0), 0);
  const succeeded = counts.succeeded ?? 0;
  const failed = counts.failed ?? 0;
  const failureClasses = Object.entries(batchEvent?.failures_by_error_class ?? {});

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

            {batchEvent && (
              <div className="flex flex-col gap-2">
                <div className="flex justify-between text-slate-600">
                  <span>
                    {L.field.status}{": "}
                    <strong>{BATCH_STATUS_LABEL[batchEvent.status] ?? batchEvent.status}</strong>
                  </span>
                  <span className="font-mono">
                    {succeeded + failed}{" / "}{total}
                  </span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
                  <div
                    className="h-full bg-emerald-500 transition-all duration-300"
                    style={{
                      width: `${total > 0 ? (succeeded / total) * 100 : 0}%`,
                    }}
                  />
                </div>
                <div className="flex justify-between text-slate-600">
                  <span>
                    {L.status.pending}{": "}
                    <strong>{counts.pending ?? 0}</strong>
                  </span>
                  <span>
                    {L.status.done}{": "}
                    <strong>{succeeded}</strong>
                  </span>
                  <span>
                    {L.status.running}{": "}
                    <strong>{counts.running ?? 0}</strong>
                  </span>
                  <span>
                    {L.status.failed}{": "}
                    <strong className="text-rose-600">{failed}</strong>
                  </span>
                </div>
                {failureClasses.length > 0 && (
                  <div className="flex flex-col gap-1 rounded-lg bg-slate-50 p-3 border border-slate-100 text-[11px] text-slate-600">
                    <span className="font-semibold text-slate-700">{L.heading.failuresByClass}</span>
                    {failureClasses.map(([cls, n]) => (
                      <span key={cls} className="flex justify-between font-mono">
                        <span>{cls}</span>
                        <strong>{n}</strong>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div className="flex items-center justify-end gap-2 pt-3 border-t border-slate-100">
              {failed > 0 ? (
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
              <label className="font-semibold text-slate-700">{L.field.manifestUri}</label>
              <input
                type="text"
                value={manifestUri}
                placeholder={L.field.manifestUriPlaceholder}
                onChange={(e) => setManifestUri(e.target.value)}
                className="rounded-lg border border-slate-300 p-2 text-slate-800 font-mono text-[11px]"
              />
              <span className="text-[11px] text-slate-500">{L.help.manifestUriHelp}</span>
              {manifestUri.trim() && !manifestValid && (
                <span className="text-[11px] text-rose-600">{L.error.manifestUriInvalid}</span>
              )}
            </div>

            <div className="flex flex-col gap-1">
              <label className="font-semibold text-slate-700">{L.field.split}</label>
              <select
                value={split}
                onChange={(e) => setSplit(e.target.value as Split)}
                className="rounded-lg border border-slate-300 p-2 text-slate-800"
              >
                <option value="train">{L.field.splitTrain}</option>
                <option value="val">{L.field.splitVal}</option>
                {can("eval:test_split") && <option value="test">{L.field.splitTest}</option>}
              </select>
            </div>

            {split === "test" && (
              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{L.field.testAccessReason}</label>
                <input
                  type="text"
                  value={testAccessReason}
                  onChange={(e) => setTestAccessReason(e.target.value)}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800"
                />
                <span className="text-[11px] text-slate-500">{L.help.testAccessHelp}</span>
                {!testReasonValid && (
                  <span className="text-[11px] text-rose-600">{L.error.testAccessReasonRequired}</span>
                )}
              </div>
            )}

            <div className="flex flex-col gap-1">
              <label className="font-semibold text-slate-700">{L.heading.stages}</label>
              <div className="flex flex-wrap gap-2">
                {PIPELINE_STAGES.map((st) => {
                  const included = (stages as string[]).includes(st);
                  const isEnd = (RUN_END_STAGES as readonly string[]).includes(st);
                  return (
                    <button
                      key={st}
                      type="button"
                      disabled={!isEnd}
                      onClick={() => isEnd && setEndStage(st as RunEndStage)}
                      className={`rounded px-3 py-1.5 text-xs font-semibold transition ${
                        included
                          ? "bg-indigo-600 text-white"
                          : "border border-slate-300 bg-white text-slate-700"
                      } ${isEnd ? "" : "cursor-default opacity-80"}`}
                    >
                      {L.stage[st]}
                    </button>
                  );
                })}
              </div>
              <span className="text-[11px] text-slate-500">{L.help.runThroughHelp}</span>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{L.field.runMode}</label>
                <select
                  value={mode}
                  onChange={(e) => setMode(e.target.value as "auto" | "manual")}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800"
                >
                  <option value="auto">{L.field.modeAuto}</option>
                  <option value="manual">{L.field.modeManual}</option>
                </select>
              </div>

              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{L.field.concurrency}</label>
                <input
                  type="number"
                  min={1}
                  max={16}
                  value={concurrency}
                  onChange={(e) => setConcurrency(Math.max(1, Number(e.target.value)))}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800"
                />
              </div>
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
                disabled={!canSubmit}
                className="rounded-lg bg-indigo-600 px-4 py-1.5 font-semibold text-white hover:bg-indigo-700 disabled:opacity-50"
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
