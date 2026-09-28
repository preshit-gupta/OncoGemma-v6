"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import { AnnotationTask, getAnnotationTasks, submitAnnotation } from "@/lib/api/research";
import { L } from "@/lib/labels";
import { useAuth } from "@/lib/auth/AuthProvider";
import { MITOTIC_FIGURE_DEFINITION } from "@/lib/definitions/mitotic_figure";

interface PointMark {
  id: string;
  x_um: number;
  y_um: number;
  kind: "MF" | "imposter";
}

export default function AnnotationPage() {
  const { can } = useAuth();
  const [tasks, setTasks] = useState<AnnotationTask[]>([]);
  const [activeTask, setActiveTask] = useState<AnnotationTask | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Annotator state
  const [points, setPoints] = useState<PointMark[]>([]);
  const [selectedPointId, setSelectedPointId] = useState<string | null>(null);
  const [currentTool, setCurrentTool] = useState<"MF" | "imposter">("MF");
  const [zoomLevel, setZoomLevel] = useState<"10x" | "40x">("40x");
  const [submitted, setSubmitted] = useState<boolean>(false);

  useEffect(() => {
    getAnnotationTasks()
      .then((res) => {
        setTasks(res.items);
        if (res.items.length > 0) {
          setActiveTask(res.items[0]);
        }
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message || String(err));
        setLoading(false);
      });
  }, []);

  // Keyboard shortcut listener
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Don't intercept when typing in input/textarea
      if (
        e.target instanceof HTMLInputElement ||
        e.target instanceof HTMLTextAreaElement
      ) {
        return;
      }

      if (e.key === "m" || e.key === "M") {
        setCurrentTool("MF");
      } else if (e.key === "x" || e.key === "X") {
        setCurrentTool("imposter");
      } else if (e.key === "Delete" || e.key === "Backspace") {
        if (selectedPointId) {
          setPoints((prev) => prev.filter((p) => p.id !== selectedPointId));
          setSelectedPointId(null);
        }
      } else if (e.key === " ") {
        e.preventDefault();
        setZoomLevel((prev) => (prev === "40x" ? "10x" : "40x"));
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [selectedPointId]);

  if (!can("research:annotate")) {
    return (
      <div className="flex h-96 items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-rose-200 bg-rose-50 p-6 text-center text-xs text-rose-800">
          <h2 className="text-sm font-bold mb-2">{L.error.forbidden}</h2>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex h-96 items-center justify-center">
        <div className="text-xs text-slate-500 font-medium animate-pulse">{L.status.processing}</div>
      </div>
    );
  }

  const handleCanvasClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const clickY = e.clientY - rect.top;

    // Convert click position to um relative coordinates
    const scale = zoomLevel === "40x" ? 0.25 : 1.0;
    const newPt: PointMark = {
      id: `pt_${Date.now()}`,
      x_um: Number((clickX * scale).toFixed(1)),
      y_um: Number((clickY * scale).toFixed(1)),
      kind: currentTool,
    };

    setPoints((prev) => [...prev, newPt]);
    setSelectedPointId(newPt.id);
  };

  const handleSubmit = async () => {
    if (!activeTask) return;
    const payload = {
      points: points.map((p) => ({
        x_um: p.x_um,
        y_um: p.y_um,
        class: p.kind,
      })),
    };

    await submitAnnotation({
      task_id: activeTask.id,
      payload,
      status: "submitted",
    });
    setSubmitted(true);
  };

  return (
    <div className="flex flex-col flex-1 overflow-y-auto bg-slate-50 p-6 gap-6">
      {/* Header bar */}
      <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
        <div>
          <div className="flex items-center gap-2 mb-1">
            <Link href="/research" className="text-xs text-indigo-600 hover:underline">
              {"← Back to Research"}
            </Link>
          </div>
          <h1 className="text-xl font-bold tracking-tight text-slate-900">
            {L.heading.annotationTasks}
          </h1>
          <p className="text-xs text-slate-500 mt-1">
            {"Standardised ground-truth annotation for model benchmark validation"}
          </p>
        </div>

        {activeTask?.blind && (
          <div className="flex items-center gap-2 rounded-lg bg-amber-100 border border-amber-300 px-3 py-1.5 text-xs font-bold text-amber-900">
            <span>{"BLINDED MODE"}</span>
            <span className="text-[11px] font-normal text-amber-800">
              {"· "}{L.help.blindAnnotationNotice}
            </span>
          </div>
        )}
      </div>

      {error ? (
        <div className="p-4 rounded-xl bg-rose-50 text-xs text-rose-700 font-medium">
          {error}
        </div>
      ) : !activeTask ? (
        <div className="p-8 text-center text-xs text-slate-400 italic">
          {"No annotation tasks available"}
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Main Annotation Workspace */}
          <div className="lg:col-span-2 flex flex-col gap-4">
            {/* Task Controls Bar */}
            <div className="flex items-center justify-between bg-white p-3 rounded-xl border border-slate-200 text-xs">
              <div className="flex items-center gap-2">
                <span className="font-bold text-slate-800 font-mono">{activeTask.slide_id}</span>
                <span className="rounded bg-slate-100 px-2 py-0.5 text-[10px] text-slate-600 font-mono">
                  {activeTask.kind}
                </span>
                <span className="rounded bg-indigo-50 text-indigo-700 px-2 py-0.5 text-[10px] font-mono">
                  {activeTask.protocol_version.slice(0, 16)}
                </span>
              </div>

              <div className="flex items-center gap-2">
                {/* Tool toggle */}
                <button
                  onClick={() => setCurrentTool("MF")}
                  className={`rounded-lg px-2.5 py-1 font-semibold transition ${
                    currentTool === "MF"
                      ? "bg-emerald-600 text-white"
                      : "bg-slate-100 text-slate-700 hover:bg-slate-200"
                  }`}
                >
                  {"Mitosis [M]"}
                </button>
                <button
                  onClick={() => setCurrentTool("imposter")}
                  className={`rounded-lg px-2.5 py-1 font-semibold transition ${
                    currentTool === "imposter"
                      ? "bg-rose-600 text-white"
                      : "bg-slate-100 text-slate-700 hover:bg-slate-200"
                  }`}
                >
                  {"Imposter [X]"}
                </button>

                {/* Zoom toggle */}
                <button
                  onClick={() => setZoomLevel((prev) => (prev === "40x" ? "10x" : "40x"))}
                  className="rounded-lg border border-slate-300 bg-white px-2.5 py-1 font-semibold text-slate-700 hover:bg-slate-50 transition"
                >
                  {zoomLevel === "40x" ? L.unit.mag40x : L.unit.mag10x}{" [Space]"}
                </button>

                <button
                  onClick={handleSubmit}
                  disabled={submitted}
                  className={`rounded-lg px-3.5 py-1 font-semibold text-white shadow-xs transition ${
                    submitted
                      ? "bg-slate-300 text-slate-500 cursor-not-allowed"
                      : "bg-indigo-600 hover:bg-indigo-700"
                  }`}
                >
                  {submitted ? L.status.confirmed : L.action.submitAnnotation}
                </button>
              </div>
            </div>

            {/* Simulated 40x / 10x WSI Canvas */}
            <div
              onClick={handleCanvasClick}
              className="relative aspect-video w-full overflow-hidden rounded-xl border border-slate-300 bg-slate-900 shadow-inner cursor-crosshair select-none"
            >
              {/* Background H&E slide tile mock image */}
              <img
                src="/mock/ctx_m1.png"
                alt=""
                className={`h-full w-full object-cover transition-transform duration-200 ${
                  zoomLevel === "40x" ? "scale-150" : "scale-100"
                }`}
              />

              {/* Bounded Task Region Polygon */}
              <div
                className="absolute inset-8 border-2 border-dashed border-sky-400 pointer-events-none rounded"
              >
                <span className="absolute top-1 left-1 bg-sky-900/80 text-sky-200 text-[10px] px-1.5 py-0.5 rounded font-mono">
                  {"HPF Region (512 × 512 µm)"}
                </span>
              </div>

              {/* Point Markers */}
              {points.map((pt) => {
                const scale = zoomLevel === "40x" ? 0.25 : 1.0;
                const posX = pt.x_um / scale;
                const posY = pt.y_um / scale;

                return (
                  <div
                    key={pt.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelectedPointId(pt.id);
                    }}
                    style={{ left: `${posX}px`, top: `${posY}px` }}
                    className={`absolute h-6 w-6 -translate-x-1/2 -translate-y-1/2 rounded-full flex items-center justify-center font-bold text-xs cursor-pointer shadow-lg transition ${
                      pt.kind === "MF"
                        ? "bg-emerald-500 text-white ring-2 ring-emerald-200"
                        : "bg-rose-500 text-white ring-2 ring-rose-200"
                    } ${selectedPointId === pt.id ? "ring-4 ring-yellow-400 scale-125" : ""}`}
                  >
                    {pt.kind === "MF" ? "M" : "X"}
                  </div>
                );
              })}

              <div className="absolute bottom-3 left-3 bg-black/70 backdrop-blur-xs text-white text-[11px] px-2.5 py-1 rounded font-mono">
                {points.length}{" points annotated · "}{L.field.zoom}{": "}{zoomLevel}
              </div>
            </div>

            {/* Instruction Tip Bar */}
            <div className="rounded-xl border border-slate-200 bg-white p-3 text-xs text-slate-500 flex justify-between items-center">
              <span>
                {"Hotkeys: "}<kbd className="px-1.5 py-0.5 bg-slate-100 rounded border font-mono">{"M"}</kbd>{" Mitosis, "}
                <kbd className="px-1.5 py-0.5 bg-slate-100 rounded border font-mono">{"X"}</kbd>{" Imposter, "}
                <kbd className="px-1.5 py-0.5 bg-slate-100 rounded border font-mono">{"Del"}</kbd>{" Delete selected, "}
                <kbd className="px-1.5 py-0.5 bg-slate-100 rounded border font-mono">{"Space"}</kbd>{" Zoom 10×/40×"}
              </span>
              {selectedPointId && (
                <button
                  onClick={() => {
                    setPoints((prev) => prev.filter((p) => p.id !== selectedPointId));
                    setSelectedPointId(null);
                  }}
                  className="text-rose-600 font-semibold hover:underline"
                >
                  {L.action.delete}
                </button>
              )}
            </div>
          </div>

          {/* Definition Side Panel */}
          <div className="flex flex-col rounded-xl border border-slate-200 bg-white p-5 shadow-sm text-xs gap-4 overflow-y-auto max-h-[680px]">
            <h3 className="font-bold text-slate-900 border-b border-slate-100 pb-2">
              {L.heading.mitosisDefinition}
            </h3>

            <pre className="whitespace-pre-wrap font-sans text-slate-700 text-[11px] leading-relaxed bg-slate-50 p-3 rounded-lg border border-slate-100">
              {MITOTIC_FIGURE_DEFINITION}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
