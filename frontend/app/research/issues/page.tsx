"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { Issue, getIssues, createIssue, updateIssue } from "@/lib/api/research";
import { L } from "@/lib/labels";
import { useAuth } from "@/lib/auth/AuthProvider";

export default function IssuesRegisterPage() {
  const { can } = useAuth();
  const [issues, setIssues] = useState<Issue[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [filterCategory, setFilterCategory] = useState<string>("");
  const [filterSeverity, setFilterSeverity] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");

  // Edit/Create Modal state
  const [modalOpen, setModalOpen] = useState<boolean>(false);
  const [editingIssue, setEditingIssue] = useState<Issue | null>(null);
  const [title, setTitle] = useState<string>("");
  const [category, setCategory] = useState<"biological" | "model" | "staging" | "technical">("model");
  const [severity, setSeverity] = useState<"critical" | "high" | "medium" | "low">("medium");
  const [statusVal, setStatusVal] = useState<"open" | "triaged" | "in_progress" | "resolved" | "wont_fix">("open");
  const [resolvedIn, setResolvedIn] = useState<string>("");
  const [resolutionNote, setResolutionNote] = useState<string>("");
  const [formError, setFormError] = useState<string | null>(null);

  const loadData = () => {
    setLoading(true);
    getIssues({
      category: filterCategory || undefined,
      severity: filterSeverity || undefined,
      status: filterStatus || undefined,
    })
      .then((res) => {
        setIssues(res);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || String(err));
        setLoading(false);
      });
  };

  useEffect(() => {
    loadData();
  }, [filterCategory, filterSeverity, filterStatus]);

  if (!can("research:read")) {
    return (
      <div className="flex h-96 items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-rose-200 bg-rose-50 p-6 text-center text-xs text-rose-800">
          <h2 className="text-sm font-bold mb-2">{L.error.forbidden}</h2>
        </div>
      </div>
    );
  }

  // Category counts
  const biologicalCount = issues.filter((i) => i.category === "biological").length;
  const modelCount = issues.filter((i) => i.category === "model").length;
  const stagingCount = issues.filter((i) => i.category === "staging").length;
  const technicalCount = issues.filter((i) => i.category === "technical").length;

  const handleOpenCreate = () => {
    setEditingIssue(null);
    setTitle("");
    setCategory("model");
    setSeverity("medium");
    setStatusVal("open");
    setResolvedIn("");
    setResolutionNote("");
    setFormError(null);
    setModalOpen(true);
  };

  const handleOpenEdit = (issue: Issue) => {
    setEditingIssue(issue);
    setTitle(issue.title);
    setCategory(issue.category);
    setSeverity(issue.severity);
    setStatusVal(issue.status);
    setResolvedIn(issue.resolved_in || "");
    setResolutionNote(issue.resolution_note || "");
    setFormError(null);
    setModalOpen(true);
  };

  const handleSaveIssue = async () => {
    if (!title.trim()) return;
    if (statusVal === "resolved" && !resolvedIn.trim()) {
      setFormError(L.error.resolutionRequiresRun);
      return;
    }

    try {
      if (editingIssue) {
        await updateIssue(editingIssue.id, {
          status: statusVal,
          resolved_in: resolvedIn.trim() || null,
          resolution_note: resolutionNote.trim() || null,
        });
      } else {
        await createIssue({
          title,
          category,
          severity,
          evidence: [],
        });
      }
      setModalOpen(false);
      loadData();
    } catch (err: any) {
      if (err.data?.error === "resolution_requires_run" || err.message === "resolution_requires_run") {
        setFormError(L.error.resolutionRequiresRun);
      } else {
        setFormError(err.message || String(err));
      }
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
            {L.heading.issueRegister}
          </h1>
          <p className="text-xs text-slate-500 mt-1">
            {"Evidence-backed issue tracker with validation run resolution verification"}
          </p>
        </div>

        {can("issue:write") && (
          <button
            onClick={handleOpenCreate}
            className="rounded-lg bg-indigo-600 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-indigo-700 shadow-sm transition"
          >
            {L.action.createIssue}
          </button>
        )}
      </div>

      {/* Category Counts Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-xs">
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <span className="text-slate-400 font-medium block mb-1">{"Biological (Remedy: Feature/Data)"}</span>
          <span className="text-2xl font-bold text-slate-900">{biologicalCount}</span>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <span className="text-slate-400 font-medium block mb-1">{"Model (Remedy: Weights/Prompt)"}</span>
          <span className="text-2xl font-bold text-slate-900">{modelCount}</span>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <span className="text-slate-400 font-medium block mb-1">{"Staging (Remedy: Pipeline logic)"}</span>
          <span className="text-2xl font-bold text-slate-900">{stagingCount}</span>
        </div>
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <span className="text-slate-400 font-medium block mb-1">{"Technical (Remedy: Infrastructure)"}</span>
          <span className="text-2xl font-bold text-slate-900">{technicalCount}</span>
        </div>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap items-center gap-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm text-xs">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-slate-500">{L.field.category}{":"}</span>
          <select
            value={filterCategory}
            onChange={(e) => setFilterCategory(e.target.value)}
            className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800"
          >
            <option value="">{L.action.filterAll}</option>
            <option value="biological">{"biological"}</option>
            <option value="model">{"model"}</option>
            <option value="staging">{"staging"}</option>
            <option value="technical">{"technical"}</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <span className="font-semibold text-slate-500">{L.field.severity}{":"}</span>
          <select
            value={filterSeverity}
            onChange={(e) => setFilterSeverity(e.target.value)}
            className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800"
          >
            <option value="">{L.action.filterAll}</option>
            <option value="critical">{"critical"}</option>
            <option value="high">{"high"}</option>
            <option value="medium">{"medium"}</option>
            <option value="low">{"low"}</option>
          </select>
        </div>

        <div className="flex items-center gap-2">
          <span className="font-semibold text-slate-500">{L.field.status}{":"}</span>
          <select
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value)}
            className="rounded-lg border border-slate-300 p-1.5 text-xs text-slate-800"
          >
            <option value="">{L.action.filterAll}</option>
            <option value="open">{"open"}</option>
            <option value="triaged">{"triaged"}</option>
            <option value="in_progress">{"in_progress"}</option>
            <option value="resolved">{"resolved"}</option>
            <option value="wont_fix">{"wont_fix"}</option>
          </select>
        </div>
      </div>

      {/* Issues Table */}
      <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden text-xs">
        {loading ? (
          <div className="p-8 text-center text-slate-500 animate-pulse">{L.status.processing}</div>
        ) : error ? (
          <div className="p-6 text-rose-700 bg-rose-50">{error}</div>
        ) : issues.length === 0 ? (
          <div className="p-8 text-center text-slate-400 italic">{"No issues found"}</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs border-collapse">
              <thead>
                <tr className="border-b border-slate-200 bg-slate-50 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
                  <th className="py-3 px-4">{"ID"}</th>
                  <th className="py-3 px-4">{"Title"}</th>
                  <th className="py-3 px-4">{L.field.category}</th>
                  <th className="py-3 px-4">{L.field.severity}</th>
                  <th className="py-3 px-4">{L.field.status}</th>
                  <th className="py-3 px-4">{L.field.resolvedIn}</th>
                  <th className="py-3 px-4">{L.field.action}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100">
                {issues.map((iss) => (
                  <tr key={iss.id} className="hover:bg-slate-50 transition">
                    <td className="py-3.5 px-4 font-mono font-bold text-slate-900">
                      {iss.id}
                    </td>
                    <td className="py-3.5 px-4 font-semibold text-slate-900">
                      <div>{iss.title}</div>
                      {iss.metric_impact && (
                        <div className="text-[10px] text-slate-500 font-mono mt-0.5">
                          {iss.metric_impact.metric}
                          {iss.metric_impact.est_delta ? ` (est Δ: ${iss.metric_impact.est_delta})` : ""}
                        </div>
                      )}
                    </td>
                    <td className="py-3.5 px-4 capitalize font-medium text-slate-700">
                      {iss.category}
                    </td>
                    <td className="py-3.5 px-4">
                      <span
                        className={`rounded px-1.5 py-0.5 text-[10px] font-bold uppercase ${
                          iss.severity === "critical"
                            ? "bg-rose-100 text-rose-800"
                            : iss.severity === "high"
                            ? "bg-amber-100 text-amber-800"
                            : "bg-slate-100 text-slate-700"
                        }`}
                      >
                        {iss.severity}
                      </span>
                    </td>
                    <td className="py-3.5 px-4">
                      <span
                        className={`rounded px-2 py-0.5 text-[10px] font-semibold capitalize ${
                          iss.status === "resolved"
                            ? "bg-emerald-100 text-emerald-800"
                            : "bg-slate-100 text-slate-700"
                        }`}
                      >
                        {iss.status}
                      </span>
                    </td>
                    <td className="py-3.5 px-4 font-mono text-[11px] text-slate-600">
                      {iss.resolved_in || "—"}
                    </td>
                    <td className="py-3.5 px-4">
                      {can("issue:write") && (
                        <button
                          onClick={() => handleOpenEdit(iss)}
                          className="text-indigo-600 hover:underline font-semibold"
                        >
                          {L.action.edit}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Create / Edit Issue Modal */}
      {modalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 backdrop-blur-xs p-4">
          <div className="flex w-full max-w-md flex-col rounded-2xl bg-white p-6 shadow-xl border border-slate-200">
            <h3 className="text-sm font-bold text-slate-900 mb-4 pb-2 border-b border-slate-100">
              {editingIssue ? L.action.updateIssue : L.action.createIssue}
            </h3>

            {formError && (
              <div className="mb-4 rounded-lg bg-rose-50 p-3 text-xs text-rose-700 font-medium">
                {formError}
              </div>
            )}

            <div className="flex flex-col gap-3 text-xs mb-4">
              <div className="flex flex-col gap-1">
                <label className="font-semibold text-slate-700">{"Title"}</label>
                <input
                  type="text"
                  value={title}
                  disabled={!!editingIssue}
                  onChange={(e) => setTitle(e.target.value)}
                  className="rounded-lg border border-slate-300 p-2 text-slate-800 disabled:bg-slate-100"
                />
              </div>

              {!editingIssue && (
                <>
                  <div className="flex flex-col gap-1">
                    <label className="font-semibold text-slate-700">{L.field.category}</label>
                    <select
                      value={category}
                      onChange={(e) => setCategory(e.target.value as any)}
                      className="rounded-lg border border-slate-300 p-2 text-slate-800"
                    >
                      <option value="biological">{"biological"}</option>
                      <option value="model">{"model"}</option>
                      <option value="staging">{"staging"}</option>
                      <option value="technical">{"technical"}</option>
                    </select>
                    <span className="text-[10px] text-slate-400">
                      {L.help.categoryRemedyHelp}
                    </span>
                  </div>

                  <div className="flex flex-col gap-1">
                    <label className="font-semibold text-slate-700">{L.field.severity}</label>
                    <select
                      value={severity}
                      onChange={(e) => setSeverity(e.target.value as any)}
                      className="rounded-lg border border-slate-300 p-2 text-slate-800"
                    >
                      <option value="critical">{"critical"}</option>
                      <option value="high">{"high"}</option>
                      <option value="medium">{"medium"}</option>
                      <option value="low">{"low"}</option>
                    </select>
                  </div>
                </>
              )}

              {editingIssue && (
                <>
                  <div className="flex flex-col gap-1">
                    <label className="font-semibold text-slate-700">{L.field.status}</label>
                    <select
                      value={statusVal}
                      onChange={(e) => setStatusVal(e.target.value as any)}
                      className="rounded-lg border border-slate-300 p-2 text-slate-800"
                    >
                      <option value="open">{"open"}</option>
                      <option value="triaged">{"triaged"}</option>
                      <option value="in_progress">{"in_progress"}</option>
                      <option value="resolved">{"resolved"}</option>
                      <option value="wont_fix">{"wont_fix"}</option>
                    </select>
                  </div>

                  <div className="flex flex-col gap-1">
                    <label className="font-semibold text-slate-700">{L.field.resolvedIn}</label>
                    <input
                      type="text"
                      value={resolvedIn}
                      onChange={(e) => setResolvedIn(e.target.value)}
                      className="rounded-lg border border-slate-300 p-2 text-slate-800 font-mono text-[11px]"
                    />
                    <span className="text-[10px] text-slate-400">
                      {L.help.resolutionRequiresRunHelp}
                    </span>
                  </div>

                  <div className="flex flex-col gap-1">
                    <label className="font-semibold text-slate-700">{L.field.resolutionNote}</label>
                    <textarea
                      rows={2}
                      value={resolutionNote}
                      onChange={(e) => setResolutionNote(e.target.value)}
                      className="rounded-lg border border-slate-300 p-2 text-slate-800"
                    />
                  </div>
                </>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 pt-3 border-t border-slate-100 text-xs">
              <button
                onClick={() => setModalOpen(false)}
                className="rounded-lg px-3 py-1.5 font-semibold text-slate-600 hover:bg-slate-100"
              >
                {L.action.cancel}
              </button>
              <button
                onClick={handleSaveIssue}
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
