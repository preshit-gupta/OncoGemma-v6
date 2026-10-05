"use client";

import React, { useEffect, useState, useCallback, useRef } from "react";
import { 
  Flame, 
  CheckCircle2, 
  Plus, 
  Sliders, 
  ShieldAlert, 
  ArrowRight, 
  Info,
  Loader2,
  Trash2,
  Crosshair,
  ZoomIn,
  X,
  RotateCcw,
  MapPin,
  Move
} from "lucide-react";
import { retryStage } from "@/lib/api";
import { 
  getTriage, 
  postTriageEdits, 
  confirmTriage, 
  TriageStageV6, 
  Hotspot, 
  EditOp 
} from "@/lib/api/triage";
import { L } from "@/lib/labels";
import { Provenance } from "../Provenance";
import { OpenSeadragonViewer } from "./OpenSeadragonViewer";

interface TriageViewerProps {
  caseId: string;
  mppX?: number;
  mppY?: number;
  imageWidthPx?: number;
  imageHeightPx?: number;
  onRefreshCase?: () => void;
  tileUrlTemplate?: string | null;
  onAdvanceToMitosis?: () => void;
}

export function TriageViewer({
  caseId,
  mppX = 0.25,
  mppY = 0.25,
  imageWidthPx = 2048,
  imageHeightPx = 2048,
  onRefreshCase,
  tileUrlTemplate = null,
  onAdvanceToMitosis
}: TriageViewerProps) {
  const [data, setData] = useState<TriageStageV6 | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [reprocessing, setReprocessing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [heatmapOpacity, setHeatmapOpacity] = useState<number>(0.6);
  const [showHeatmap, setShowHeatmap] = useState<boolean>(true);
  const [showHotspotMask, setShowHotspotMask] = useState<boolean>(true);
  const [hotspotsList, setHotspotsList] = useState<Hotspot[]>([]);
  const [conflictingHotspotIds, setConflictingHotspotIds] = useState<string[]>([]);
  const [selectedHotspotId, setSelectedHotspotId] = useState<string | null>(null);
  const [previewHotspot, setPreviewHotspot] = useState<Hotspot | null>(null);
  const [isAddingRoiMode, setIsAddingRoiMode] = useState<boolean>(false);
  const [noInvasiveTumor, setNoInvasiveTumor] = useState<boolean>(false);
  const [excludeReasonInput, setExcludeReasonInput] = useState<{ [id: string]: string }>({});
  const [movingHotspotId, setMovingHotspotId] = useState<string | null>(null);
  const [acceptFewerHpfs, setAcceptFewerHpfs] = useState<boolean>(false);
  const [tooFewSitesPrompt, setTooFewSitesPrompt] = useState<boolean>(false);

  // The loader must not depend on `data`: each fetch would re-create it and the effect below
  // would fetch again, forever. Whether data is already shown is read from a ref instead.
  const hasDataRef = useRef(false);
  const fetchTriageData = useCallback(async (silent: boolean = false) => {
    try {
      if (!silent && !hasDataRef.current) setLoading(true);
      setError(null);
      const stageData = await getTriage(caseId);
      hasDataRef.current = true;
      setData(stageData);
      setHotspotsList(stageData.hotspots || []);
      setConflictingHotspotIds([]);
    } catch (err: any) {
      if (!silent) setError(err.message || L.error.genericError);
    } finally {
      if (!silent) setLoading(false);
    }
  }, [caseId]);

  useEffect(() => {
    hasDataRef.current = false;
    fetchTriageData();
  }, [fetchTriageData]);

  // Auto-poll while triage is running or queued
  useEffect(() => {
    if (!data || data.status === "running" || data.status === "queued") {
      const timer = setInterval(() => {
        fetchTriageData(true);
      }, 3000);
      return () => clearInterval(timer);
    }
  }, [data, fetchTriageData]);

  const circleAreaMm2 = (hs: Hotspot): number => {
    const r = hs.hpf_diameter_um / 2;
    return (Math.PI * r * r) / 1e6;
  };

  const executeEdits = async (edits: EditOp[]) => {
    try {
      setSubmitting(true);
      setError(null);
      const updated = await postTriageEdits(caseId, edits);
      setData(updated);
      setHotspotsList(updated.hotspots);
      setConflictingHotspotIds([]);
    } catch (err: any) {
      if (err.data?.error === "hotspot_overlap") {
        const ids: string[] = Array.from(new Set<string>((err.data.ids || []).flat()));
        setConflictingHotspotIds(ids);
        setError(L.fmt.hotspotOverlap(ids));
      } else if (err.data?.error === "invalid_site") {
        setError(L.error.invalidSite);
      } else if (err.data?.error === "too_many_sites") {
        setError(L.fmt.tooManySites(err.data.hpf_target));
      } else if (err.data?.error === "triage_rerun_required") {
        setError(L.error.triageRerunRequired);
      } else {
        setError(err.message || L.error.genericError);
      }
      // Revert local hotspots list
      if (data?.hotspots) {
        setHotspotsList(data.hotspots);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleExcludeHotspot = (id: string) => {
    const reason = excludeReasonInput[id] || "Pathologist excluded";
    executeEdits([{ op: "exclude", id, reason }]);
  };

  const handleRestoreHotspot = (id: string) => {
    executeEdits([{ op: "restore", id }]);
  };

  const handleDeleteHotspot = (id: string) => {
    executeEdits([{ op: "delete", id }]);
    if (selectedHotspotId === id) setSelectedHotspotId(null);
    if (previewHotspot?.id === id) setPreviewHotspot(null);
    if (movingHotspotId === id) setMovingHotspotId(null);
  };

  // A click on the slide pins a new HPF, or sets the new centre of the site being moved.
  const handleSlideClick = (x_um: number, y_um: number) => {
    const center: [number, number] = [Number(x_um.toFixed(1)), Number(y_um.toFixed(1))];
    if (movingHotspotId) {
      const id = movingHotspotId;
      setMovingHotspotId(null);
      executeEdits([{ op: "move", id, center_um: center }]);
      return;
    }
    setIsAddingRoiMode(false);
    executeEdits([{ op: "add", center_um: center }]);
  };

  const handleConfirmStage = async (zeroTumor: boolean = false) => {
    try {
      setSubmitting(true);
      setError(null);
      await confirmTriage(caseId, zeroTumor || noInvasiveTumor, acceptFewerHpfs);
      if (data) {
        setData({ ...data, status: "confirmed" });
      }
      if (onAdvanceToMitosis && !zeroTumor && !noInvasiveTumor) {
        onAdvanceToMitosis();
      } else if (onRefreshCase) {
        onRefreshCase();
      }
    } catch (err: any) {
      if (err.data?.error === "hpf_sites_lt_10") {
        // Fewer active sites than the target: ask for the acknowledgement instead of failing.
        setTooFewSitesPrompt(true);
        setError(L.fmt.tissueInadequateAck(err.data.hpf_target));
      } else if (err.data?.error === "hotspot_overlap") {
        const ids: string[] = Array.from(new Set<string>((err.data.ids || []).flat()));
        setConflictingHotspotIds(ids);
        setError(L.fmt.hotspotOverlap(ids));
      } else if (err.data?.error === "triage_rerun_required") {
        setError(L.error.triageRerunRequired);
      } else {
        setError(err.message || L.error.stageExecutionFailed);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleReprocessTriage = async () => {
    try {
      setReprocessing(true);
      await retryStage(caseId, "triage");
      if (onRefreshCase) onRefreshCase();
      await fetchTriageData(true);
    } catch (err: any) {
      setError(err.message || L.error.stageExecutionFailed);
    } finally {
      setReprocessing(false);
    }
  };

  const activeHotspotsCount = hotspotsList.filter((h) => !h.excluded).length;
  const hpfTarget = data?.hpf_target ?? 0;
  const tooFewSites = Boolean(data) && activeHotspotsCount < hpfTarget;
  const totalAreaMm2 = hotspotsList
    .filter((h) => !h.excluded)
    .reduce((sum, h) => sum + circleAreaMm2(h), 0);

  const siteLabel = (hs: Hotspot) =>
    hs.rank !== null ? `#${hs.rank}` : `U${hs.id.split("_").pop()}`;

  const viewerHotspots = hotspotsList.map((hs) => ({
    id: hs.id,
    polygon_um: hs.polygon_um,
    center_um: hs.center_um,
    radius_um: hs.hpf_diameter_um / 2,
    label: siteLabel(hs),
    at_periphery: hs.at_periphery,
    area_mm2: circleAreaMm2(hs),
    source: hs.source,
    excluded: hs.excluded,
    conflicting: conflictingHotspotIds.includes(hs.id),
  }));

  const overlayGrid = data?.heatmap
    ? {
        origin_um: data.heatmap.origin_um,
        stride_um: data.heatmap.tile_um,
        nx: data.heatmap.nx,
        ny: data.heatmap.ny,
      }
    : null;

  if (loading) {
    return (
      <div className="w-full h-full bg-slate-950 flex flex-col items-center justify-center text-slate-400">
        <Loader2 className="w-8 h-8 animate-spin text-sky-500 mb-2" />
        <p className="text-sm font-medium">{L.status.extractingHotspots}</p>
      </div>
    );
  }

  return (
    <div className="w-full h-full flex bg-slate-950 overflow-hidden">
      {/* Left Main Viewport */}
      <div className="flex-1 relative">
        <OpenSeadragonViewer
          caseId={caseId}
          mppX={mppX}
          mppY={mppY || mppX}
          imageWidthPx={imageWidthPx}
          imageHeightPx={imageHeightPx}
          overlayImageUri={data?.heatmap?.png_url || null}
          overlayOpacity={heatmapOpacity}
          showOverlay={showHeatmap && Boolean(data?.heatmap)}
          showHotspotMask={showHotspotMask}
          hotspots={viewerHotspots}
          selectedHotspotId={selectedHotspotId}
          onSelectHotspot={setSelectedHotspotId}
          isAddingRoiMode={isAddingRoiMode || movingHotspotId !== null}
          onAddRoiClick={handleSlideClick}
          tileUrlTemplate={tileUrlTemplate}
          grid={overlayGrid}
        />

        {/* Flag banners */}
        <div className="absolute top-4 left-4 right-4 z-20 flex flex-col space-y-2 pointer-events-none">
          {data?.flags?.includes("hotspots_limited_by_tissue") && (
            <div className="pointer-events-auto bg-sky-950/90 border border-sky-800 text-sky-200 text-xs px-4 py-2.5 rounded-lg shadow-lg flex items-center space-x-2 backdrop-blur">
              <Info className="w-4 h-4 text-sky-400 shrink-0" />
              <span>{L.fmt.tissueInadequateBanner(data.n_sites_available, data.hpf_target)}</span>
            </div>
          )}

          {data?.flags?.includes("no_invasive_tumor_detected") && (
            <div className="pointer-events-auto bg-amber-950/90 border border-amber-800 text-amber-200 text-xs px-4 py-2.5 rounded-lg shadow-lg flex items-center justify-between backdrop-blur">
              <div className="flex items-center space-x-2">
                <Info className="w-4 h-4 text-amber-400 shrink-0" />
                <span>{L.help.noInvasiveDetected}</span>
              </div>
              <button
                type="button"
                onClick={() => handleConfirmStage(true)}
                className="px-3 py-1 bg-amber-600 hover:bg-amber-500 text-white rounded font-medium shadow-sm transition"
              >
                {L.action.confirmZeroTumor}
              </button>
            </div>
          )}

          {error && (
            <div className="pointer-events-auto bg-rose-950/90 border border-rose-800 text-rose-200 text-xs px-4 py-2.5 rounded-lg shadow-lg flex items-center justify-between backdrop-blur">
              <div className="flex items-center space-x-2">
                <ShieldAlert className="w-4 h-4 text-rose-400 shrink-0" />
                <span>{error}</span>
              </div>
              <button
                type="button"
                onClick={() => setError(null)}
                className="text-rose-400 hover:text-rose-200"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>

        {/* Pin / move HPF floating banner */}
        {(isAddingRoiMode || movingHotspotId !== null) && (
          <div className="absolute top-16 left-1/2 -translate-x-1/2 z-30 bg-sky-950/95 border-2 border-sky-400 rounded-2xl px-5 py-2.5 shadow-2xl flex items-center space-x-3 backdrop-blur">
            <Crosshair className="w-4 h-4 text-sky-400 animate-spin" />
            <span className="text-xs font-medium text-sky-100">
              {movingHotspotId !== null ? L.help.moveHpfHint : L.help.pinHpfHint}
            </span>
            <button
              type="button"
              onClick={() => {
                setIsAddingRoiMode(false);
                setMovingHotspotId(null);
              }}
              className="px-2.5 py-1 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg text-xs font-bold transition border border-slate-700"
            >
              {L.action.cancel}
            </button>
          </div>
        )}

        {/* Heatmap & Hotspot Locations Mask Floating Toolbar */}
        <div className="absolute bottom-6 left-4 z-20 bg-slate-900/95 backdrop-blur border border-slate-800 rounded-lg p-3 shadow-xl flex flex-col space-y-2.5">
          {/* Row 1: Tumor Heatmap Toggle */}
          <div className="flex items-center space-x-3 justify-between">
            <div className="flex items-center space-x-2 text-xs font-semibold text-slate-200">
              <Flame className="w-4 h-4 text-amber-400" />
              <span>{L.action.toggleHeatmap}</span>
            </div>

            <div className="flex items-center space-x-2">
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={showHeatmap}
                  onChange={(e) => setShowHeatmap(e.target.checked)}
                  className="sr-only peer"
                />
                <div className="w-8 h-4 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-3 after:w-3 after:transition-all peer-checked:bg-sky-600"></div>
              </label>

              {showHeatmap && (
                <div className="flex items-center space-x-1.5 border-l border-slate-800 pl-2">
                  <Sliders className="w-3.5 h-3.5 text-slate-400" />
                  <input
                    type="range"
                    min="0.1"
                    max="1.0"
                    step="0.05"
                    value={heatmapOpacity}
                    onChange={(e) => setHeatmapOpacity(parseFloat(e.target.value))}
                    className="w-14 accent-sky-500 cursor-pointer"
                  />
                  <span className="text-[10px] font-mono text-slate-400">
                    {Math.round(heatmapOpacity * 100)}%
                  </span>
                </div>
              )}
            </div>
          </div>

          {/* Row 2: Hotspot Locations Mask Toggle */}
          <div className="flex items-center space-x-3 justify-between pt-2 border-t border-slate-800/80">
            <div className="flex items-center space-x-2 text-xs font-semibold text-slate-200">
              <MapPin className="w-4 h-4 text-sky-400" />
              <span>{L.action.toggleMask}</span>
            </div>

            <label className="relative inline-flex items-center cursor-pointer">
              <input
                type="checkbox"
                checked={showHotspotMask}
                onChange={(e) => setShowHotspotMask(e.target.checked)}
                className="sr-only peer"
              />
              <div className="w-8 h-4 bg-slate-700 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:border-slate-300 after:border after:rounded-full after:h-3 after:w-3 after:transition-all peer-checked:bg-sky-600"></div>
            </label>
          </div>
        </div>
      </div>

      {/* Right Pathologist Review Sidebar Rail */}
      <div className="w-96 border-l border-slate-800 bg-slate-900 flex flex-col h-full shadow-2xl z-20">
        {/* Header */}
        <div className="p-4 border-b border-slate-800 flex items-center justify-between bg-slate-900/50">
          <div>
            <h2 className="text-sm font-bold text-slate-100 flex items-center space-x-2">
              <Flame className="w-4 h-4 text-amber-500" />
              <span>{L.heading.hotspotTriage}</span>
            </h2>
          </div>
          <div className="flex items-center space-x-2">
            {data?.provenance && <Provenance provenance={data.provenance} />}
            <button
              type="button"
              onClick={() => fetchTriageData()}
              className="p-1.5 bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 rounded-lg text-xs font-semibold flex items-center space-x-1 transition shadow-sm"
              title={L.action.refresh}
            >
              <RotateCcw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} />
              <span className="hidden sm:inline">{L.action.refresh}</span>
            </button>
            <button
              type="button"
              onClick={handleReprocessTriage}
              disabled={reprocessing}
              className="p-1.5 bg-amber-600/20 hover:bg-amber-600/40 text-amber-400 border border-amber-500/30 rounded-lg text-xs font-semibold flex items-center space-x-1 transition shadow-sm"
              title={L.action.rerunHotspots}
            >
              <RotateCcw className={`w-3.5 h-3.5 ${reprocessing ? "animate-spin" : ""}`} />
              <span className="hidden sm:inline">{L.action.rerunHotspots}</span>
            </button>
            <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase ${
              data?.status === "confirmed" ? "bg-emerald-950 text-emerald-400 border border-emerald-800" : "bg-amber-950 text-amber-400 border border-amber-800"
            }`}>
              {data?.status}
            </span>
          </div>
        </div>

        {/* Stats Summary Panel */}
        <div className="p-4 bg-slate-950/60 border-b border-slate-800 grid grid-cols-2 gap-3">
          <div className="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
            <div className="text-[10px] font-semibold uppercase text-slate-400">{L.fmt.hpfSitesActive(activeHotspotsCount, hpfTarget)}</div>
            <div className={`text-lg font-bold font-mono ${tooFewSites ? "text-amber-400" : "text-sky-400"}`}>{activeHotspotsCount} / {hpfTarget}</div>
          </div>
          <div className="bg-slate-900 p-2.5 rounded-lg border border-slate-800">
            <div className="text-[10px] font-semibold uppercase text-slate-400">{L.field.tumorArea}</div>
            <div className="text-lg font-bold font-mono text-amber-400">{L.fmt.areaMm2(totalAreaMm2)}</div>
          </div>
        </div>

        {/* Hotspots List */}
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-xs font-bold text-slate-300 uppercase tracking-wider">{L.heading.proposedRois}</span>
            <button
              type="button"
              onClick={() => {
                setMovingHotspotId(null);
                setIsAddingRoiMode(!isAddingRoiMode);
              }}
              className={`px-2.5 py-1 rounded text-xs font-semibold flex items-center space-x-1.5 transition ${
                isAddingRoiMode
                  ? "bg-sky-600 text-white ring-2 ring-sky-400 shadow-md shadow-sky-600/30"
                  : "bg-sky-600/20 hover:bg-sky-600/40 text-sky-400 border border-sky-600/40"
              }`}
              title={L.help.pinHpfHint}
            >
              <Plus className="w-3.5 h-3.5" />
              <span>{isAddingRoiMode ? L.action.cancel : L.action.pinHpf}</span>
            </button>
          </div>

          {hotspotsList.length === 0 ? (
            <div className="p-4 border border-dashed border-slate-800 rounded-lg text-center text-xs text-slate-500">
              {L.help.noHotspots}
            </div>
          ) : (
            hotspotsList.map((hs) => {
              const isSelected = selectedHotspotId === hs.id;
              const isConflicting = conflictingHotspotIds.includes(hs.id);
              const scoreKindLabel =
                hs.score_kind === "prescan_then_tumor"
                  ? L.field.prescanThenTumor
                  : hs.score_kind === "prescan_expected_count"
                  ? L.field.prescanExpectedCount
                  : hs.score_kind === "mean_p_tumor"
                  ? L.field.meanTumorProb
                  : null;

              return (
                <div
                  key={hs.id}
                  className={`p-3 rounded-lg border transition ${
                    isConflicting
                      ? "bg-rose-950/40 border-rose-500 ring-2 ring-rose-500/50 shadow-lg text-rose-200"
                      : isSelected
                      ? "bg-slate-900 border-sky-500 ring-1 ring-sky-500/50 shadow-lg"
                      : hs.excluded
                      ? "bg-slate-950/40 border-slate-800/60 opacity-60"
                      : "bg-slate-900/90 border-slate-800 hover:border-slate-700"
                  }`}
                >
                  <div className="flex items-center justify-between mb-2">
                    <div className="flex items-center space-x-2">
                      <span className="font-mono text-xs font-bold text-sky-400">
                        {siteLabel(hs)}
                      </span>
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">
                        {hs.source}
                      </span>
                    </div>

                    <div className="flex items-center space-x-1.5">
                      {!hs.excluded && (
                        <>
                          <button
                            type="button"
                            onClick={() => {
                              setSelectedHotspotId(null);
                              setTimeout(() => setSelectedHotspotId(hs.id), 50);
                            }}
                            className={`px-2 py-0.5 rounded text-[11px] font-semibold flex items-center space-x-1 transition ${
                              isSelected
                                ? "bg-sky-600 text-white shadow-md shadow-sky-600/30"
                                : "bg-slate-800 hover:bg-sky-600/30 text-sky-400 border border-slate-700"
                            }`}
                            title={L.action.locate}
                          >
                            <Crosshair className="w-3 h-3" />
                            <span>{L.action.locate}</span>
                          </button>

                          <button
                            type="button"
                            onClick={() => {
                              setIsAddingRoiMode(false);
                              setMovingHotspotId(movingHotspotId === hs.id ? null : hs.id);
                            }}
                            className={`px-2 py-0.5 rounded text-[11px] font-semibold flex items-center space-x-1 transition ${
                              movingHotspotId === hs.id
                                ? "bg-amber-600 text-white shadow-md shadow-amber-600/30"
                                : "bg-slate-800 hover:bg-amber-600/30 text-amber-400 border border-slate-700"
                            }`}
                            title={L.help.moveHpfHint}
                          >
                            <Move className="w-3 h-3" />
                            <span>{L.action.moveHpf}</span>
                          </button>
                        </>
                      )}

                      {hs.excluded && (
                        <button
                          type="button"
                          onClick={() => handleRestoreHotspot(hs.id)}
                          className="text-xs text-emerald-400 hover:underline font-semibold"
                        >
                          {L.action.includeHotspot}
                        </button>
                      )}
                      <button
                        type="button"
                        onClick={() => handleDeleteHotspot(hs.id)}
                        className="p-1 hover:bg-slate-800 text-slate-500 hover:text-rose-400 rounded"
                        title={L.help.deleteHotspot}
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>

                  {/* Hotspot metadata */}
                  <div className="grid grid-cols-2 gap-1.5 text-[11px] font-mono text-slate-400 mb-2">
                    {scoreKindLabel && (
                      <div className="col-span-2">
                        {L.field.scoreKind}: <span className="text-sky-300">{scoreKindLabel}</span>
                      </div>
                    )}
                    {hs.tumor_fraction !== null && (
                      <div>
                        {L.field.tumorFraction}: <span className="text-slate-200">{Math.round(hs.tumor_fraction * 100)}%</span>
                      </div>
                    )}
                    {hs.prescan_expected !== null && (
                      <div>
                        {L.field.expectedMitoses}: <span className="text-slate-200">{hs.prescan_expected.toFixed(1)}</span>
                      </div>
                    )}
                    <div>
                      {L.field.tumorArea}: <span className="text-slate-200">{L.fmt.areaMm2(circleAreaMm2(hs))}</span>
                    </div>
                  </div>

                  {!hs.excluded && (
                    <div className="flex items-center space-x-2 pt-2 border-t border-slate-800/80">
                      <input
                        type="text"
                        placeholder={L.field.exclusionReason}
                        value={excludeReasonInput[hs.id] || ""}
                        onChange={(e) => setExcludeReasonInput({ ...excludeReasonInput, [hs.id]: e.target.value })}
                        className="flex-1 bg-slate-950 border border-slate-800 text-xs px-2 py-1 rounded text-slate-300 placeholder-slate-600 focus:outline-none focus:border-slate-700"
                      />
                      <button
                        type="button"
                        onClick={() => handleExcludeHotspot(hs.id)}
                        className="px-2 py-1 bg-rose-950/60 hover:bg-rose-900 text-rose-300 border border-rose-800/60 rounded text-xs font-semibold transition"
                      >
                        {L.action.excludeHotspot}
                      </button>
                    </div>
                  )}

                  {hs.excluded && hs.exclude_reason && (
                    <div className="text-[11px] text-amber-400 italic mt-1">
                      {L.status.excluded}: {hs.exclude_reason}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>

        {/* Footer Confirmation Gate */}
        <div className="p-4 border-t border-slate-800 bg-slate-950/90 space-y-3">
          <label className="flex items-start space-x-2 cursor-pointer bg-slate-900/60 p-2 rounded-lg border border-slate-800">
            <input
              type="checkbox"
              checked={noInvasiveTumor}
              onChange={(e) => setNoInvasiveTumor(e.target.checked)}
              className="mt-0.5 accent-rose-500 rounded cursor-pointer"
            />
            <span className="text-xs text-slate-300">
              {L.help.noInvasiveTumor}
            </span>
          </label>

          {(tooFewSites || tooFewSitesPrompt) && !noInvasiveTumor && data?.status !== "confirmed" && (
            <label className="flex items-start space-x-2 cursor-pointer bg-amber-950/40 p-2 rounded-lg border border-amber-800/70">
              <input
                type="checkbox"
                checked={acceptFewerHpfs}
                onChange={(e) => setAcceptFewerHpfs(e.target.checked)}
                className="mt-0.5 accent-amber-500 rounded cursor-pointer"
              />
              <span className="text-xs text-amber-200">
                {L.fmt.tissueInadequateAck(hpfTarget)}
              </span>
            </label>
          )}

          <div className="flex items-center space-x-2">
            <button
              type="button"
              onClick={handleReprocessTriage}
              disabled={reprocessing}
              className="px-3 py-2.5 bg-amber-600/20 hover:bg-amber-600/30 text-amber-300 border border-amber-500/40 rounded-lg text-xs font-semibold flex items-center justify-center space-x-1.5 transition shadow-sm"
              title={L.action.rerunHotspots}
            >
              <RotateCcw className={`w-3.5 h-3.5 ${reprocessing ? "animate-spin" : ""}`} />
              <span>{L.action.rerunHotspots}</span>
            </button>

            <button
              type="button"
              onClick={() => handleConfirmStage(false)}
              disabled={submitting || data?.status === "confirmed" || (activeHotspotsCount === 0 && !noInvasiveTumor) || (tooFewSites && !acceptFewerHpfs && !noInvasiveTumor)}
              className={`flex-1 py-2.5 rounded-lg text-xs font-bold flex items-center justify-center space-x-2 shadow-lg transition ${
                data?.status === "confirmed"
                  ? "bg-emerald-950/60 border border-emerald-800/80 text-emerald-300 cursor-not-allowed"
                  : (activeHotspotsCount > 0 && (!tooFewSites || acceptFewerHpfs)) || noInvasiveTumor
                  ? "bg-sky-600 hover:bg-sky-500 text-white shadow-sky-600/20"
                  : "bg-slate-800 text-slate-500 cursor-not-allowed"
              }`}
              title={data?.status === "confirmed" ? L.status.confirmed : L.action.confirmHotspots}
            >
              {submitting ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : data?.status === "confirmed" ? (
                <>
                  <CheckCircle2 className="w-4 h-4 text-emerald-400" />
                  <span>{L.status.confirmed}</span>
                </>
              ) : (
                <>
                  <span>{L.action.confirmHotspots}</span>
                  <ArrowRight className="w-4 h-4" />
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
