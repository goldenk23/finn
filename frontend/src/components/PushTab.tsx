"use client";

import React, { useState } from "react";
import {
  Filter,
  RefreshCw,
  ChevronDown,
  ChevronUp,
  Sparkles,
  CheckCircle2,
  Layers,
} from "lucide-react";
import { DigestResponse, PortfolioItem, TimeWindow } from "@/types/finn";
import { FilingCard } from "@/components/FilingCard";

interface PushTabProps {
  digest: DigestResponse | null;
  loading: boolean;
  refreshing: boolean;
  window: TimeWindow;
  selectedTicker: string | null;
  portfolio: PortfolioItem[];
  onChangeWindow: (w: TimeWindow) => void;
  onChangeTicker: (t: string | null) => void;
  onRefreshLive: () => void;
  onOpenPull: (ticker: string) => void;
  onFilingEnriched: (filingId: string, enriched: Record<string, unknown>) => void;
}

const WINDOWS: TimeWindow[] = ["24h", "48h", "7d", "14d", "30d"];

export function PushTab({
  digest,
  loading,
  refreshing,
  window,
  selectedTicker,
  portfolio,
  onChangeWindow,
  onChangeTicker,
  onRefreshLive,
  onOpenPull,
  onFilingEnriched,
}: PushTabProps) {
  const [showRoutine, setShowRoutine] = useState(false);

  const stats = digest?.stats || {
    total_filings: 0,
    material_count: 0,
    routine_collapsed_count: 0,
    noise_filtered_pct: 0,
  };
  const material = digest?.material || [];
  const routine = digest?.routine || [];

  return (
    <div className="space-y-3.5 pb-6">
      {/* Top Control Bar: Window Switcher + Refresh Live */}
      <div className="flex items-center justify-between gap-2">
        <div className="inline-flex items-center rounded-lg border border-zinc-800 bg-zinc-900/90 p-0.5">
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => onChangeWindow(w)}
              className={`px-2.5 py-1 rounded-md text-xs font-mono font-semibold transition ${
                window === w
                  ? "bg-emerald-500 text-zinc-950 shadow-sm"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {w}
            </button>
          ))}
        </div>

        <button
          type="button"
          onClick={onRefreshLive}
          disabled={refreshing}
          className="inline-flex items-center gap-1.5 rounded-lg border border-emerald-500/30 bg-emerald-500/10 hover:bg-emerald-500/20 px-2.5 py-1.5 text-xs font-semibold text-emerald-300 transition disabled:opacity-50"
        >
          <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? "animate-spin" : ""}`} />
          <span>{refreshing ? "Scraping NSE/BSE..." : "Refresh Live"}</span>
        </button>
      </div>

      {/* Horizontal Portfolio Ticker Filter Chips */}
      <div className="flex items-center gap-1.5 overflow-x-auto no-scrollbar pb-1">
        <button
          type="button"
          onClick={() => onChangeTicker(null)}
          className={`shrink-0 px-2.5 py-1 rounded-full text-[11px] font-mono font-semibold border transition ${
            selectedTicker === null
              ? "bg-zinc-100 text-zinc-950 border-zinc-100"
              : "bg-zinc-900/80 text-zinc-400 border-zinc-800 hover:border-zinc-700"
          }`}
        >
          All Portfolio ({portfolio.length})
        </button>
        {portfolio.map((p) => (
          <button
            key={p.ticker}
            type="button"
            onClick={() =>
              onChangeTicker(selectedTicker === p.ticker ? null : p.ticker)
            }
            className={`shrink-0 px-2.5 py-1 rounded-full text-[11px] font-mono font-medium border transition ${
              selectedTicker === p.ticker
                ? "bg-emerald-500/20 text-emerald-300 border-emerald-500/40"
                : "bg-zinc-900/70 text-zinc-400 border-zinc-800/80 hover:text-zinc-200"
            }`}
          >
            {p.ticker}
          </button>
        ))}
      </div>

      {/* Noise Filter Signal Banner */}
      <div className="rounded-xl border border-emerald-500/25 bg-gradient-to-r from-emerald-950/40 via-zinc-900/90 to-zinc-900/90 p-3">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-emerald-500/15 text-emerald-400">
              <Filter className="w-3.5 h-3.5" />
            </div>
            <div>
              <div className="text-xs font-bold text-white flex items-center gap-1.5">
                <span>{stats.noise_filtered_pct}% Exchange Noise Buried</span>
                <span className="text-[10px] font-mono font-normal text-emerald-400 bg-emerald-500/10 px-1.5 py-0.5 rounded">
                  Flash-Lite Triage
                </span>
              </div>
              <p className="text-[11px] text-zinc-400 mt-0.5">
                <span className="text-emerald-300 font-semibold">
                  {stats.material_count} Material Signals
                </span>{" "}
                prioritized •{" "}
                <span className="text-zinc-300">
                  {stats.routine_collapsed_count} Routine Filings
                </span>{" "}
                collapsed out of {stats.total_filings}
              </p>
            </div>
          </div>
        </div>

        {stats.total_filings > 0 && (
          <div className="mt-2.5 h-1.5 w-full overflow-hidden rounded-full bg-zinc-800 flex">
            <div
              className="bg-emerald-400 h-full transition-all"
              style={{
                width: `${Math.max(
                  8,
                  Math.min(100, 100 - stats.noise_filtered_pct)
                )}%`,
              }}
              title="Material Signals"
            />
          </div>
        )}
      </div>

      {/* Material Filings Feed */}
      <div className="space-y-2.5">
        <div className="flex items-center justify-between px-0.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400">
            <Sparkles className="w-3.5 h-3.5 text-emerald-400" />
            <span>Material Corporate Signals ({material.length})</span>
          </div>
          <span className="text-[10px] font-mono text-zinc-500">
            Ranked by Materiality
          </span>
        </div>

        {loading ? (
          <div className="space-y-2.5">
            {[1, 2, 3].map((n) => (
              <div
                key={n}
                className="h-28 rounded-xl border border-zinc-800/80 bg-zinc-900/50 animate-pulse p-3.5"
              />
            ))}
          </div>
        ) : material.length === 0 ? (
          <div className="rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-5 text-center space-y-3">
            <CheckCircle2 className="w-6 h-6 text-emerald-400/80 mx-auto" />
            <div>
              <p className="text-xs font-semibold text-zinc-200">
                No material filings in the last {window}
                {selectedTicker ? ` for ${selectedTicker}` : ""}.
              </p>
              <p className="text-[11px] text-zinc-400 mt-1">
                {stats.routine_collapsed_count > 0
                  ? `${stats.routine_collapsed_count} routine exchange notices were automatically filtered out.`
                  : "Exchange feeds are quiet for this window."}
              </p>
            </div>

            {window !== "7d" && window !== "14d" ? (
              <button
                type="button"
                onClick={() => onChangeWindow("7d")}
                className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 px-3.5 py-2 text-xs font-bold text-zinc-950 shadow-sm transition"
              >
                <span>
                  Quiet last {window} across your portfolio — View 7d Material Signals →
                </span>
              </button>
            ) : (
              <button
                type="button"
                onClick={() => onChangeWindow("14d")}
                className="inline-flex items-center justify-center gap-1.5 rounded-lg border border-emerald-500/40 bg-emerald-500/15 hover:bg-emerald-500/25 px-3.5 py-1.5 text-xs font-semibold text-emerald-300 transition"
              >
                <span>Expand to 14d Material Signals →</span>
              </button>
            )}
          </div>
        ) : (
          material.map((f) => (
            <FilingCard
              key={f.filing_id}
              filing={f}
              onSelectTicker={onOpenPull}
              onEnriched={onFilingEnriched}
            />
          ))
        )}
      </div>

      {/* Collapsible Routine / Low-Relevance Filings Accordion */}
      {routine.length > 0 && (
        <div className="pt-2">
          <button
            type="button"
            onClick={() => setShowRoutine(!showRoutine)}
            className="w-full flex items-center justify-between rounded-xl border border-zinc-800/90 bg-zinc-900/60 hover:bg-zinc-900 px-3.5 py-2.5 text-left transition"
          >
            <div className="flex items-center gap-2">
              <Layers className="w-3.5 h-3.5 text-zinc-500" />
              <div>
                <span className="text-xs font-semibold text-zinc-300">
                  Routine / Low-Relevance Filings ({routine.length} collapsed)
                </span>
                <p className="text-[10px] text-zinc-500">
                  Newspaper ads, loss of share certificates, routine notices, unclassified
                </p>
              </div>
            </div>
            {showRoutine ? (
              <ChevronUp className="w-4 h-4 text-zinc-400 shrink-0" />
            ) : (
              <ChevronDown className="w-4 h-4 text-zinc-400 shrink-0" />
            )}
          </button>

          {showRoutine && (
            <div className="mt-2 space-y-2 pl-2 border-l-2 border-zinc-800">
              {routine.map((r) => (
                <FilingCard
                  key={r.filing_id}
                  filing={r}
                  onSelectTicker={onOpenPull}
                  compact
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

