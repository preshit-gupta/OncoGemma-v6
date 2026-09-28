"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { QAItem, getQAItems, reviewQAItem } from "@/lib/api/research";
import { L } from "@/lib/labels";
import { useAuth } from "@/lib/auth/AuthProvider";

export default function LabelsQAPage() {
  const { can } = useAuth();
  const [items, setItems] = useState<QAItem[]>([]);
  const [selectedPatientId, setSelectedPatientId] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Edit action modal / reason input
  const [actionType, setActionType] = useState<"edit" | "exclude" | null>(null);
  const [actionReason, setActionReason] = useState<string>("");
  const [editValues, setEditValues] = useState<{
    tubule?: number;
    pleo?: number;
    mitoses?: number;
    grade?: number;
  }>({});
  const [formError, setFormError] = useState<string | null>(null);

  const loadData = () => {
    setLoading(true);
    getQAItems()
      .then((res) => {
        setItems(res);
        if (res.length > 0 && !selectedPatientId) {
          setSelectedPatientId(res[0].patient_id);
          setEditValues(res[0].llm.values as any);
        }
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || String(err));
        setLoading(false);
      });
  };

  useEffect(() => {
    loadData();
  }, []);

  if (!can("labels:qa")) {
    return (
      <div className="flex h-96 items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-rose-200 bg-rose-50 p-6 text-center text-xs text-rose-800">
          <h2 className="text-sm font-bold mb-2">{L.error.forbidden}</h2>
        </div>
      </div>
    );
  }

  const currentItem = items.find((i) => i.patient_id === selectedPatientId) || items[0];

  const handleAccept = async () => {
    if (!currentItem) return;
    try {
      await reviewQAItem(currentItem.patient_id, { action: "accept" });
      loadData();
    } catch (err: any) {
      setError(err.message || String(err));
    }
  };

  const handleOpenActionModal = (type: "edit" | "exclude") => {
    setActionType(type);
    setActionReason("");
    setFormError(null);
    if (currentItem) {
      setEditValues(currentItem.llm.values as any);
    }
  };

  const handleConfirmAction = async () => {
    if (!currentItem || !actionType) return;
    if (!actionReason.trim()) {
      setFormError(L.error.reasonRequired);
      return;
    }

    try {
      await reviewQAItem(currentItem.patient_id, {
        action: actionType,
        values: actionType === "edit" ? editValues : undefined,
        reason: actionReason.trim(),
      });
      setActionType(null);
      loadData();
    } catch (err: any) {
      setFormError(err.message || String(err));
    }
  };

  return (
    <div className="flex flex-col flex-1 overflow-y-auto bg-slate-50 p-6 gap-6">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Link href="/research" className="text-xs text-indigo-600 hover:underline">
              {"← Back to Research"}
            </Link>
          </div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900">
            {L.heading.labelQaQueue}
          </h1>
          <p className="text-xs text-slate-500 mt-1">
            {"TCGA synoptic pathology report label QA and extraction adjudication"}
          </p>
        </div>
      </div>

      {loading ? (
        <div className="p-8 text-center text-slate-500 text-xs animate-pulse">
          {L.status.processing}
        </div>
      ) : error ? (
        <div className="p-4 rounded-xl bg-rose-50 text-xs text-rose-700 font-medium">
          {error}
        </div>
      ) : !currentItem ? (
        <div className="p-8 text-center text-slate-400 italic text-xs">
          {"No items in QA queue"}
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 text-xs">
          {/* Left Column: Queue List */}
          <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm flex flex-col gap-2">
            <span className="font-bold text-slate-800 mb-2">{"Queue Items"}</span>
            {items.map((item) => (
              <div
                key={item.patient_id}
                onClick={() => {
                  setSelectedPatientId(item.patient_id);
                  setEditValues(item.llm.values as any);
                }}
                className={`p-3 rounded-lg border cursor-pointer transition ${
                  item.patient_id === currentItem.patient_id
                    ? "border-indigo-500 bg-indigo-50/50"
                    : "border-slate-200 hover:bg-slate-50"
                }`}
              >
                <div className="flex justify-between items-center mb-1">
                  <span className="font-mono font-bold text-slate-900">{item.patient_id}</span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-semibold capitalize ${
                      item.status === "accepted"
                        ? "bg-emerald-100 text-emerald-800"
                        : item.status === "edited"
                        ? "bg-indigo-100 text-indigo-800"
                        : item.status === "excluded"
                        ? "bg-rose-100 text-rose-800"
                        : "bg-amber-100 text-amber-800"
                    }`}
                  >
                    {item.status}
                  </span>
                </div>
                <div className="text-[11px] text-slate-500">
                  {"Grade "}{item.llm.values.grade ?? "—"}{" (score "}{item.llm.values.total ?? "—"}{")"}
                </div>
              </div>
            ))}
          </div>

          {/* Right Column: Comparison & Action Workspace */}
          <div className="lg:col-span-2 flex flex-col gap-4">
            {/* Action Bar */}
            <div className="flex items-center justify-between bg-white p-4 rounded-xl border border-slate-200">
              <div className="flex items-center gap-2">
                <span className="font-mono font-bold text-slate-900 text-sm">
                  {currentItem.patient_id}
                </span>
                <span
                  className={`rounded px-2 py-0.5 text-xs font-semibold capitalize ${
                    currentItem.status === "accepted"
                      ? "bg-emerald-100 text-emerald-800"
                      : "bg-slate-100 text-slate-700"
                  }`}
                >
                  {currentItem.status}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <button
                  onClick={handleAccept}
                  className="rounded-lg bg-emerald-600 px-3.5 py-1.5 font-semibold text-white hover:bg-emerald-700 transition shadow-xs"
                >
                  {L.action.accept}
                </button>
                <button
                  onClick={() => handleOpenActionModal("edit")}
                  className="rounded-lg border border-slate-300 px-3 py-1.5 font-semibold text-slate-700 hover:bg-slate-50 transition"
                >
                  {L.action.edit}
                </button>
                <button
                  onClick={() => handleOpenActionModal("exclude")}
                  className="rounded-lg border border-rose-300 px-3 py-1.5 font-semibold text-rose-700 hover:bg-rose-50 transition"
                >
                  {L.action.exclude}
                </button>
              </div>
            </div>

            {/* Side-by-side values (Regex vs LLM Extracted) */}
            <div className="grid grid-cols-2 gap-4">
              {/* Regex Values */}
              <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                <span className="font-bold text-slate-800 block mb-3 border-b border-slate-100 pb-1">
                  {"Regex Extracted"}
                </span>
                <div className="space-y-2 text-xs">
                  <div className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-500">{L.field.grade}</span>
                    <strong className="text-slate-900 font-mono">
                      {currentItem.regex.grade ?? "—"}
                    </strong>
                  </div>
                  <div className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-500">{L.field.tubuleScore}</span>
                    <strong className="text-slate-900 font-mono">
                      {currentItem.regex.tubule ?? "—"}
                    </strong>
                  </div>
                  <div className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-500">{L.field.pleoScore}</span>
                    <strong className="text-slate-900 font-mono">
                      {currentItem.regex.pleo ?? "—"}
                    </strong>
                  </div>
                  <div className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-500">{L.field.mitosisScore}</span>
                    <strong className="text-slate-900 font-mono">
                      {currentItem.regex.mitoses ?? "—"}
                    </strong>
                  </div>
                </div>
              </div>

              {/* LLM Values */}
              <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
                <span className="font-bold text-slate-800 block mb-3 border-b border-slate-100 pb-1">
                  {"LLM Extracted (Structured)"}
                </span>
                <div className="space-y-2 text-xs">
                  <div className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-500">{L.field.grade}</span>
                    <strong className="text-slate-900 font-mono">
                      {currentItem.llm.values.grade ?? "—"}
                    </strong>
                  </div>
                  <div className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-500">{L.field.tubuleScore}</span>
                    <strong className="text-slate-900 font-mono">
                      {currentItem.llm.values.tubule ?? "—"}
                    </strong>
                  </div>
                  <div className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-500">{L.field.pleoScore}</span>
                    <strong className="text-slate-900 font-mono">
                      {currentItem.llm.values.pleo ?? "—"}
                    </strong>
                  </div>
                  <div className="flex justify-between py-1 border-b border-slate-50">
                    <span className="text-slate-500">{L.field.mitosisScore}</span>
                    <strong className="text-slate-900 font-mono">
                      {currentItem.llm.values.mitoses ?? "—"}
                    </strong>
                  </div>
                </div>
              </div>
            </div>

            {/* Evidence Quotes */}
            <div className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
              <span className="font-bold text-slate-900 block mb-3 border-b border-slate-100 pb-2">
                {"Evidence Quotes from Pathology Report"}
              </span>
              <div className="space-y-2 text-xs">
                {currentItem.llm.evidence.map((ev, idx) => (
                  <div key={idx} className="rounded-lg bg-amber-50 p-2.5 border border-amber-200 flex gap-2">
                    <span className="font-bold text-amber-900 uppercase font-mono text-[10px] shrink-0">
                      {ev.field}{":"}
                    </span>
                    <span className="text-slate-800 italic">
                      {`"${ev.quote}"`}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Edit / Exclude Action Modal */}
      {actionType && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4">
          <div className="flex w-full max-w-md flex-col rounded-2xl bg-white p-6 shadow-xl border border-slate-200 text-xs">
            <h3 className="text-sm font-bold text-slate-900 mb-4 pb-2 border-b border-slate-100 capitalize">
              {actionType === "edit" ? L.action.edit : L.action.exclude}
            </h3>

            {formError && (
              <div className="mb-4 rounded-lg bg-rose-50 p-3 text-rose-700 font-medium">
                {formError}
              </div>
            )}

            {actionType === "edit" && (
              <div className="grid grid-cols-2 gap-3 mb-4">
                <div className="flex flex-col gap-1">
                  <label className="font-semibold text-slate-700">{L.field.grade}</label>
                  <input
                    type="number"
                    min={1}
                    max={3}
                    value={editValues.grade ?? 2}
                    onChange={(e) => setEditValues({ ...editValues, grade: Number(e.target.value) })}
                    className="rounded-lg border border-slate-300 p-2 text-slate-800"
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="font-semibold text-slate-700">{L.field.tubuleScore}</label>
                  <input
                    type="number"
                    min={1}
                    max={3}
                    value={editValues.tubule ?? 2}
                    onChange={(e) => setEditValues({ ...editValues, tubule: Number(e.target.value) })}
                    className="rounded-lg border border-slate-300 p-2 text-slate-800"
                  />
                </div>
              </div>
            )}

            <div className="flex flex-col gap-1 mb-4">
              <label className="font-semibold text-slate-700">{L.field.reason}{" (Required)"}</label>
              <textarea
                rows={3}
                value={actionReason}
                onChange={(e) => setActionReason(e.target.value)}
                className="rounded-lg border border-slate-300 p-2.5 text-slate-800"
              />
            </div>

            <div className="flex items-center justify-end gap-2 pt-3 border-t border-slate-100">
              <button
                onClick={() => setActionType(null)}
                className="rounded-lg px-3 py-1.5 font-semibold text-slate-600 hover:bg-slate-100"
              >
                {L.action.cancel}
              </button>
              <button
                onClick={handleConfirmAction}
                className="rounded-lg bg-indigo-600 px-4 py-1.5 font-semibold text-white hover:bg-indigo-700"
              >
                {L.action.save}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
