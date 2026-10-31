"use client";

import React, { useState, useEffect } from "react";
import {
  Sliders,
  AlertTriangle,
  Activity,
  TrendingUp,
  TrendingDown,
  ArrowUpRight,
  Check,
  Save,
} from "lucide-react";
import { AlertsResponse, TimeWindow } from "@/types/finn";
import { FilingCard } from "@/components/FilingCard";

interface AlertsTabProps {
  alertsData: AlertsResponse | null;
  loading: boolean;
  savingRules: boolean;
  onUpdateRules: (rules: {
    price_move_pct: number;
    volume_multiplier: number;
    watched_categories: string[];
  }) => void;
  onOpenPull: (ticker: string) => void;
  window: TimeWindow;
  onChangeWindow: (w: TimeWindow) => void;
}

const ALERT_WINDOWS: TimeWindow[] = ["24h", "3d", "7d", "14d", "30d"];

const WATCHABLE_CATEGORIES = [
  { id: "results", label: "Results" },
  { id: "M&A", label: "M&A" },
  { id: "management_change", label: "Mgmt Change" },
  { id: "fund_raise", label: "Fund Raise" },
  { id: "litigation", label: "Litigation" },
  { id: "capex", label: "Capex" },
  { id: "credit_rating", label: "Credit Rating" },
  { id: "dividend", label: "Dividend" },
  { id: "insider_trading", label: "Insider Trade" },
  { id: "pledging", label: "Pledging" },
];

export function AlertsTab({
  alertsData,
  loading,
  savingRules,
  onUpdateRules,
  onOpenPull,
  window,
  onChangeWindow,
}: AlertsTabProps) {
  const [pricePct, setPricePct] = useState<number>(3.0);
  const [volMult, setVolMult] = useState<number>(2.0);
  const [watched, setWatched] = useState<string[]>([
    "results",
    "M&A",
    "management_change",
    "fund_raise",
    "litigation",
    "capex",
  ]);

  useEffect(() => {
    if (alertsData?.rules) {
      setPricePct(Number(alertsData.rules.price_move_pct ?? 3.0));
      setVolMult(Number(alertsData.rules.volume_multiplier ?? 2.0));
      if (Array.isArray(alertsData.rules.watched_categories)) {
        setWatched(alertsData.rules.watched_categories);
      }
    }
  }, [alertsData?.rules]);

  function toggleCategory(catId: string) {
    const next = watched.includes(catId)
      ? watched.filter((c) => c !== catId)
      : [...watched, catId];
    setWatched(next);
    onUpdateRules({
      price_move_pct: pricePct,
      volume_multiplier: volMult,
      watched_categories: next,
    });
  }

  function handleSliderCommit(nextPrice: number, nextVol: number) {
    onUpdateRules({
      price_move_pct: nextPrice,
      volume_multiplier: nextVol,
      watched_categories: watched,
    });
  }

  const marketAlerts = alertsData?.market_alerts || [];
  const filingAlerts = alertsData?.filing_alerts || [];

  return (
    <div className="space-y-4 pb-6">
      {/* Filing alert window (price/volume alerts always use the latest EOD session) */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-zinc-400">Filing alert window</span>
        <div
          role="group"
          aria-label="Filing alert window"
          className="inline-flex items-center rounded-lg border border-zinc-800 bg-zinc-900/90 p-0.5"
        >
          {ALERT_WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              aria-pressed={window === w}
              onClick={() => onChangeWindow(w)}
              className={`px-2.5 py-1 rounded-md text-xs font-mono font-semibold transition ${
                window === w
                  ? "bg-amber-400 text-zinc-950 shadow-sm"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              {w}
            </button>
          ))}
        </div>
      </div>

      {/* Configurable Rules Card */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-3.5">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-amber-500/15 text-amber-400">
              <Sliders className="w-3.5 h-3.5" />
            </div>
            <div>
              <h2 className="text-xs font-bold text-white uppercase tracking-wide">
                Configurable Alert Thresholds
              </h2>
              <p className="text-[11px] text-zinc-400">
                Adjust sliders &amp; category triggers — re-evaluates portfolio live
              </p>
            </div>
          </div>
          {savingRules && (
            <span className="inline-flex items-center gap-1 text-[10px] font-mono text-emerald-400">
              <Save className="w-3 h-3 animate-pulse" />
              Saving...
            </span>
          )}
        </div>

        {/* Sliders for Price Move & Volume Spike */}
        <div className="grid grid-cols-2 gap-3 pt-1">
          <div className="rounded-lg border border-zinc-800/90 bg-zinc-950/70 p-2.5">
            <div className="flex items-center justify-between text-xs">
              <span className="text-zinc-400 font-medium">EOD Price Move</span>
              <span className="font-mono font-bold text-amber-400">
                ±{pricePct.toFixed(1)}%
              </span>
            </div>
            <input
              type="range"
              min={0.5}
              max={8.0}
              step={0.5}
              value={pricePct}
              onChange={(e) => setPricePct(parseFloat(e.target.value))}
              onMouseUp={() => handleSliderCommit(pricePct, volMult)}
              onTouchEnd={() => handleSliderCommit(pricePct, volMult)}
              className="mt-2 w-full accent-amber-400 cursor-pointer h-1.5 bg-zinc-800 rounded-lg"
            />
            <div className="flex justify-between text-[9px] font-mono text-zinc-500 mt-1">
              <span>±0.5% (Tight)</span>
              <span>±8.0%</span>
            </div>
          </div>

          <div className="rounded-lg border border-zinc-800/90 bg-zinc-950/70 p-2.5">
            <div className="flex items-center justify-between text-xs">
              <span className="text-zinc-400 font-medium">Volume Spike</span>
              <span className="font-mono font-bold text-cyan-400">
                {volMult.toFixed(1)}x 20d
              </span>
            </div>
            <input
              type="range"
              min={1.0}
              max={4.0}
              step={0.2}
              value={volMult}
              onChange={(e) => setVolMult(parseFloat(e.target.value))}
              onMouseUp={() => handleSliderCommit(pricePct, volMult)}
              onTouchEnd={() => handleSliderCommit(pricePct, volMult)}
              className="mt-2 w-full accent-cyan-400 cursor-pointer h-1.5 bg-zinc-800 rounded-lg"
            />
            <div className="flex justify-between text-[9px] font-mono text-zinc-500 mt-1">
              <span>1.0x avg</span>
              <span>4.0x spike</span>
            </div>
          </div>
        </div>

        {/* Watched Filing Category Chips */}
        <div className="mt-3 pt-2.5 border-t border-zinc-800/80">
          <div className="text-[11px] font-medium text-zinc-400 mb-2">
            Watched Material Filing Categories:
          </div>
          <div className="flex flex-wrap gap-1.5">
            {WATCHABLE_CATEGORIES.map((cat) => {
              const active = watched.includes(cat.id);
              return (
                <button
                  key={cat.id}
                  type="button"
                  onClick={() => toggleCategory(cat.id)}
                  className={`inline-flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-medium border transition ${
                    active
                      ? "bg-emerald-500/15 text-emerald-300 border-emerald-500/35"
                      : "bg-zinc-950 text-zinc-500 border-zinc-800 hover:text-zinc-300"
                  }`}
                >
                  {active && <Check className="w-2.5 h-2.5 text-emerald-400" />}
                  <span>{cat.label}</span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Section 1: Triggered Market EOD Price / Volume Anomalies */}
      <div className="space-y-2">
        <div className="flex items-center justify-between px-0.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400">
            <Activity className="w-3.5 h-3.5 text-amber-400" />
            <span>EOD Price &amp; Volume Anomalies ({marketAlerts.length})</span>
          </div>
          <span className="text-[10px] font-mono text-zinc-500">
            yfinance NSE EOD
          </span>
        </div>

        {loading ? (
          <div className="h-20 rounded-xl border border-zinc-800 bg-zinc-900/50 animate-pulse" />
        ) : marketAlerts.length === 0 ? (
          <div className="rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4 text-center text-xs text-zinc-400">
            No portfolio stocks breached ±{pricePct.toFixed(1)}% price move or{" "}
            {volMult.toFixed(1)}x 20d volume on the latest session. Try lowering the
            sliders above to inspect sensitivity.
          </div>
        ) : (
          <div className="space-y-2">
            {marketAlerts.map((item) => {
              const pct = Number(item.pct_change || 0);
              const isPos = pct >= 0;
              return (
                <div
                  key={item.ticker}
                  className="rounded-xl border border-amber-500/25 bg-zinc-900/80 p-3 flex items-center justify-between gap-3"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => onOpenPull(item.ticker)}
                        className="inline-flex items-center gap-1 rounded bg-zinc-800 hover:bg-zinc-700 px-2 py-0.5 text-xs font-mono font-bold text-white"
                      >
                        {item.ticker}
                        <ArrowUpRight className="w-3 h-3 text-zinc-400" />
                      </button>
                      <span className="text-xs text-zinc-300 truncate">
                        {item.company_name}
                      </span>
                    </div>

                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                      {(item.trigger_reasons || []).map((reason, idx) => (
                        <span
                          key={idx}
                          className="inline-flex items-center gap-1 rounded bg-amber-500/15 border border-amber-500/30 px-1.5 py-0.5 text-[10px] font-mono text-amber-300"
                        >
                          <AlertTriangle className="w-2.5 h-2.5" />
                          {reason}
                        </span>
                      ))}
                    </div>
                  </div>

                  <div className="text-right shrink-0">
                    <div className="text-xs font-mono font-bold text-white">
                      ₹{Number(item.close_price || 0).toLocaleString("en-IN")}
                    </div>
                    <div
                      className={`inline-flex items-center gap-0.5 text-[11px] font-mono font-semibold ${
                        isPos ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      {isPos ? (
                        <TrendingUp className="w-3 h-3" />
                      ) : (
                        <TrendingDown className="w-3 h-3" />
                      )}
                      {pct >= 0 ? `+${pct.toFixed(2)}%` : `${pct.toFixed(2)}%`}
                    </div>
                    <div className="text-[10px] font-mono text-zinc-400">
                      Vol: {Number(item.volume_ratio || 1).toFixed(2)}x 20d
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Section 2: Triggered Material Filing Alerts */}
      <div className="space-y-2.5 pt-1">
        <div className="flex items-center justify-between px-0.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400">
            <AlertTriangle className="w-3.5 h-3.5 text-emerald-400" />
            <span>Triggered Filing Alerts ({filingAlerts.length})</span>
          </div>
          <span className="text-[10px] font-mono text-zinc-500">
            Matching {watched.length} watched categories
          </span>
        </div>

        {filingAlerts.length === 0 ? (
          <div className="rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-4 text-center text-xs text-zinc-400">
            No material filings matched the selected categories in the last {window}.
          </div>
        ) : (
          filingAlerts.map((f) => (
            <FilingCard
              key={f.filing_id}
              filing={f}
              onSelectTicker={onOpenPull}
            />
          ))
        )}
      </div>
    </div>
  );
}

