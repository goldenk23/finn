"use client";

import React, { useState } from "react";
import {
  Plus,
  Trash2,
  TrendingUp,
  TrendingDown,
  Zap,
  AlertTriangle,
  ArrowUpRight,
  Loader2,
  Briefcase,
} from "lucide-react";
import { PortfolioItem } from "@/types/finn";

interface PortfolioTabProps {
  portfolio: PortfolioItem[];
  loading: boolean;
  onAddStock: (ticker: string, sector: string) => Promise<void>;
  onRemoveStock: (ticker: string) => Promise<void>;
  onOpenPull: (ticker: string) => void;
  onFilterDigest: (ticker: string) => void;
}

export function PortfolioTab({
  portfolio,
  loading,
  onAddStock,
  onRemoveStock,
  onOpenPull,
  onFilterDigest,
}: PortfolioTabProps) {
  const [newTicker, setNewTicker] = useState("");
  const [newSector, setNewSector] = useState("Equities");
  const [adding, setAdding] = useState(false);
  const [removingTicker, setRemovingTicker] = useState<string | null>(null);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    const clean = newTicker.trim().toUpperCase();
    if (!clean) return;
    try {
      setAdding(true);
      await onAddStock(clean, newSector);
      setNewTicker("");
    } finally {
      setAdding(false);
    }
  }

  async function handleRemove(ticker: string) {
    try {
      setRemovingTicker(ticker);
      await onRemoveStock(ticker);
    } finally {
      setRemovingTicker(null);
    }
  }

  const cachedCount = portfolio.filter((p) => p.has_cached_pull).length;
  const anomalyCount = portfolio.filter(
    (p) => p.price_alert || p.volume_alert
  ).length;

  return (
    <div className="space-y-3.5 pb-6">
      {/* Portfolio Summary Strip */}
      <div className="grid grid-cols-3 gap-2">
        <div className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-2.5">
          <div className="text-[10px] font-mono uppercase text-zinc-400">
            Tracked Stocks
          </div>
          <div className="mt-0.5 text-base font-mono font-bold text-white">
            {portfolio.length}
          </div>
          <div className="text-[10px] text-zinc-500">NSE / BSE Watchlist</div>
        </div>

        <div className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-2.5">
          <div className="text-[10px] font-mono uppercase text-zinc-400">
            EOD Anomalies
          </div>
          <div className="mt-0.5 text-base font-mono font-bold text-amber-400">
            {anomalyCount}
          </div>
          <div className="text-[10px] text-zinc-500">Price / Vol Alerts</div>
        </div>

        <div className="rounded-xl border border-zinc-800 bg-zinc-900/80 p-2.5">
          <div className="text-[10px] font-mono uppercase text-zinc-400">
            Warm Pull Cache
          </div>
          <div className="mt-0.5 text-base font-mono font-bold text-emerald-400">
            {cachedCount}/{portfolio.length}
          </div>
          <div className="text-[10px] text-zinc-500">&lt;25ms Instant</div>
        </div>
      </div>

      {/* Add NSE Stock Form */}
      <form
        onSubmit={handleAdd}
        className="rounded-xl border border-zinc-800 bg-zinc-900/90 p-3 flex items-center gap-2"
      >
        <input
          type="text"
          value={newTicker}
          onChange={(e) => setNewTicker(e.target.value)}
          placeholder="Add NSE ticker (e.g. DIXON, BEL)..."
          className="flex-1 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-1.5 text-xs font-mono text-white placeholder-zinc-500 focus:border-emerald-500 focus:outline-none"
        />
        <select
          value={newSector}
          onChange={(e) => setNewSector(e.target.value)}
          className="rounded-lg border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-300 focus:outline-none"
        >
          <option value="Equities">Equities</option>
          <option value="IT Services">IT Services</option>
          <option value="Banking">Banking</option>
          <option value="Consumer">Consumer</option>
          <option value="Industrials">Industrials</option>
          <option value="Pharma">Pharma</option>
          <option value="Auto">Auto</option>
        </select>
        <button
          type="submit"
          disabled={adding || !newTicker.trim()}
          className="inline-flex items-center gap-1 rounded-lg bg-emerald-500 hover:bg-emerald-400 px-3 py-1.5 text-xs font-bold text-zinc-950 transition disabled:opacity-50"
        >
          {adding ? (
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
          ) : (
            <Plus className="w-3.5 h-3.5" />
          )}
          <span>Add</span>
        </button>
      </form>

      {/* Dense Institutional Watchlist */}
      <div className="space-y-2">
        <div className="flex items-center justify-between px-0.5">
          <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-zinc-400">
            <Briefcase className="w-3.5 h-3.5 text-emerald-400" />
            <span>Active Portfolio Watchlist</span>
          </div>
          <span className="text-[10px] font-mono text-zinc-500">
            Tap ticker for Deep Pull
          </span>
        </div>

        {loading ? (
          <div className="space-y-2">
            {[1, 2, 3, 4].map((i) => (
              <div
                key={i}
                className="h-16 rounded-xl border border-zinc-800 bg-zinc-900/50 animate-pulse"
              />
            ))}
          </div>
        ) : (
          portfolio.map((item) => {
            const pct = Number(item.pct_change || 0);
            const isPos = pct >= 0;
            const vRatio = Number(item.volume_ratio || 1);

            return (
              <div
                key={item.ticker}
                className="rounded-xl border border-zinc-800/90 bg-zinc-900/75 hover:border-zinc-700 p-3 transition"
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <button
                        type="button"
                        onClick={() => onOpenPull(item.ticker)}
                        className="inline-flex items-center gap-1 rounded bg-zinc-800 hover:bg-emerald-500 hover:text-zinc-950 px-2 py-0.5 text-xs font-mono font-bold text-white transition"
                      >
                        <span>{item.ticker}</span>
                        <ArrowUpRight className="w-3 h-3" />
                      </button>

                      <span className="text-[10px] font-mono text-zinc-400 bg-zinc-800/60 px-1.5 py-0.5 rounded">
                        {item.sector}
                      </span>

                      {item.has_cached_pull && (
                        <span
                          className="inline-flex items-center gap-0.5 rounded bg-emerald-500/15 border border-emerald-500/30 px-1.5 py-0.5 text-[9px] font-mono text-emerald-300"
                          title="Pull Research pre-warmed in SQLite cache"
                        >
                          <Zap className="w-2.5 h-2.5" />
                          Pull Warm
                        </span>
                      )}

                      {(item.price_alert || item.volume_alert) && (
                        <span className="inline-flex items-center gap-0.5 rounded bg-amber-500/15 border border-amber-500/30 px-1.5 py-0.5 text-[9px] font-mono text-amber-300">
                          <AlertTriangle className="w-2.5 h-2.5" />
                          Anomaly
                        </span>
                      )}
                    </div>

                    <div className="text-xs text-zinc-300 mt-1 truncate">
                      {item.company_name}
                    </div>
                  </div>

                  {/* Right Side Price + Delta + Volume */}
                  <div className="text-right shrink-0">
                    <div className="text-xs font-mono font-bold text-white">
                      {item.close_price
                        ? `₹${Number(item.close_price).toLocaleString("en-IN")}`
                        : "—"}
                    </div>
                    <div
                      className={`inline-flex items-center gap-0.5 text-[11px] font-mono font-semibold ${
                        isPos ? "text-emerald-400" : "text-rose-400"
                      }`}
                    >
                      {isPos ? (
                        <TrendingUp className="w-2.5 h-2.5" />
                      ) : (
                        <TrendingDown className="w-2.5 h-2.5" />
                      )}
                      {isPos ? `+${pct.toFixed(2)}%` : `${pct.toFixed(2)}%`}
                    </div>
                    <div className="text-[10px] font-mono text-zinc-500">
                      Vol {vRatio.toFixed(2)}x 20d
                    </div>
                  </div>
                </div>

                {/* Bottom Action Strip */}
                <div className="mt-2 pt-2 border-t border-zinc-800/60 flex items-center justify-between text-[11px]">
                  <div className="flex items-center gap-3">
                    <button
                      type="button"
                      onClick={() => onOpenPull(item.ticker)}
                      className="text-emerald-400 hover:underline font-medium"
                    >
                      8-Module Pull Report →
                    </button>
                    <button
                      type="button"
                      onClick={() => onFilterDigest(item.ticker)}
                      className="text-zinc-400 hover:text-zinc-200"
                    >
                      Material Filings ({item.material_filings_count || 0})
                    </button>
                  </div>

                  <button
                    type="button"
                    onClick={() => handleRemove(item.ticker)}
                    disabled={removingTicker === item.ticker}
                    className="text-zinc-500 hover:text-rose-400 p-1 rounded transition"
                    title={`Remove ${item.ticker} from portfolio`}
                  >
                    <Trash2 className="w-3.5 h-3.5" />
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

