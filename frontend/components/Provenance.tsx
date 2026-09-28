"use client";

import React, { useState, useRef, useEffect } from "react";
import { Info } from "lucide-react";
import { L } from "@/lib/labels";

export interface ProvenanceProps {
  provenance?: {
    model_versions?: Record<string, string>;
    config_hash?: string;
    run_mode?: string;
  };
  model_versions?: Record<string, string>;
  config_hash?: string;
  run_mode?: string;
}

export const Provenance: React.FC<ProvenanceProps> = ({
  provenance,
  model_versions: directVersions,
  config_hash: directConfigHash,
  run_mode: directRunMode,
}) => {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const versions = provenance?.model_versions || directVersions || {};
  const configHash = provenance?.config_hash || directConfigHash;
  const runMode = provenance?.run_mode || directRunMode;

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    if (isOpen) {
      document.addEventListener("mousedown", handleClickOutside);
    }
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [isOpen]);

  const shortConfigHash = configHash ? configHash.slice(0, 8) : null;
  const versionEntries = Object.entries(versions);

  return (
    <div className="relative inline-block text-left" ref={containerRef}>
      <button
        type="button"
        onClick={() => setIsOpen((prev) => !prev)}
        className="p-1 rounded-full text-slate-400 hover:text-slate-200 hover:bg-slate-700/50 transition-colors focus:outline-none"
        title={L.heading.provenance}
        aria-label={L.heading.provenance}
      >
        <Info className="w-4 h-4" />
      </button>

      {isOpen && (
        <div className="absolute right-0 mt-2 w-72 rounded-lg bg-slate-900 border border-slate-700 p-3 shadow-xl z-50 text-xs">
          <div className="font-semibold text-slate-200 border-b border-slate-800 pb-1 mb-2">
            {L.heading.provenance}
          </div>

          {versionEntries.length > 0 && (
            <div className="mb-2">
              <span className="text-slate-400 font-medium">{L.field.modelVersions}:</span>
              <ul className="mt-1 space-y-1">
                {versionEntries.map(([model, ver]) => (
                  <li key={model} className="flex justify-between text-slate-300 font-mono">
                    <span className="text-slate-400">{model}:</span>
                    <span>{ver}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {shortConfigHash && (
            <div className="flex justify-between text-slate-300 mb-1">
              <span className="text-slate-400 font-medium">{L.field.configHash}:</span>
              <span className="font-mono text-cyan-400">{shortConfigHash}</span>
            </div>
          )}

          {runMode && (
            <div className="flex justify-between text-slate-300">
              <span className="text-slate-400 font-medium">{L.field.runMode}:</span>
              <span className="capitalize text-slate-200">{runMode}</span>
            </div>
          )}

          {versionEntries.length === 0 && !shortConfigHash && !runMode && (
            <div className="text-slate-500 italic">{L.status.pending}</div>
          )}
        </div>
      )}
    </div>
  );
};
