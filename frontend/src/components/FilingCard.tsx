"use client";

import React, { useState } from "react";
import {
  FileText,
  ExternalLink,
  Sparkles,
  TrendingUp,
  TrendingDown,
  UserCheck,
  UserMinus,
  Building2,
  ArrowUpRight,
  Loader2,
} from "lucide-react";
import { FilingItem, MetricDelta } from "@/types/finn";
import { finnApi } from "@/lib/api";

interface FilingCardProps {
  filing: FilingItem;
  onSelectTicker?: (ticker: string) => void;
  onEnriched?: (filingId: string, enriched: Record<string, unknown>) => void;
  compact?: boolean;
}

const CATEGORY_STYLES: Record<string, { label: string; cls: string }> = {
  results: {
    label: "Quarterly Results",
    cls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  },
  "M&A": {
    label: "M&A / Corporate Action",
    cls: "bg-purple-500/15 text-purple-300 border-purple-500/30",
  },
  management_change: {
    label: "Key Management",
    cls: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  },
  fund_raise: {
    label: "Fund Raise",
    cls: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  },
  litigation: {
    label: "Litigation / Order",
    cls: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  },
  capex: {
    label: "Capex / Capacity",
    cls: "bg-blue-500/15 text-blue-300 border-blue-500/30",
  },
  credit_rating: {
    label: "Credit Rating",
    cls: "bg-indigo-500/15 text-indigo-300 border-indigo-500/30",
  },
  dividend: {
    label: "Dividend",
    cls: "bg-teal-500/15 text-teal-300 border-teal-500/30",
  },
  earnings_call: {
    label: "Concall / Recording",
    cls: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  },
  press_release: {
    label: "Press Release",
    cls: "bg-slate-500/20 text-slate-300 border-slate-500/30",
  },
};

function DeltaPill({ val }: { val?: string }) {
  if (!val || val === "N/A" || val === "-") {
    return <span className="text-[11px] font-mono text-zinc-500">—</span>;
  }
  const isNeg = val.trim().startsWith("-");
  const isPos = val.trim().startsWith("+") || (!isNeg && parseFloat(val) > 0);
  return (
    <span
      className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[11px] font-mono font-medium ${
        isPos
          ? "bg-emerald-500/15 text-emerald-300 border border-emerald-500/25"
          : isNeg
          ? "bg-rose-500/15 text-rose-300 border border-rose-500/25"
          : "bg-zinc-800 text-zinc-300"
      }`}
    >
      {isPos ? <TrendingUp className="w-2.5 h-2.5" /> : isNeg ? <TrendingDown className="w-2.5 h-2.5" /> : null}
      {val}
    </span>
  );
}

export function FilingCard({
  filing,
  onSelectTicker,
  onEnriched,
  compact = false,
}: FilingCardProps) {
  const [enriching, setEnriching] = useState(false);
  const [localEnriched, setLocalEnriched] = useState<Record<string, unknown> | undefined>(
    filing.enriched
  );

  const enriched = (localEnriched || filing.enriched || {}) as FilingItem["enriched"];
  const catMeta = CATEGORY_STYLES[filing.category] || {
    label: filing.category.replace(/_/g, " "),
    cls: "bg-zinc-800 text-zinc-300 border-zinc-700",
  };

  const hasResultsTable =
    filing.category === "results" &&
    enriched?.metrics &&
    (enriched.metrics.revenue?.value || enriched.metrics.pat?.value);

  const hasMgmtBio =
    filing.category === "management_change" &&
    Boolean(enriched?.two_line_bio || enriched?.person_name);

  const canEnrich =
    (filing.category === "results" && !hasResultsTable) ||
    (filing.category === "management_change" && !hasMgmtBio);

  async function handleEnrich() {
    try {
      setEnriching(true);
      const res = await finnApi.enrichFiling(filing.filing_id);
      setLocalEnriched(res.enriched);
      onEnriched?.(filing.filing_id, res.enriched);
    } catch {
      // ignore error in UI
    } finally {
      setEnriching(false);
    }
  }

  const timeLabel = filing.published_at ? filing.published_at.slice(0, 16) : "";

  return (
    <div className="rounded-xl border border-zinc-800/90 bg-zinc-900/75 p-3.5 transition hover:border-zinc-700/90">
      {filing.trigger_reason && (
        <div className="mb-2 flex items-center gap-1.5 rounded-md bg-amber-500/10 border border-amber-500/25 px-2 py-1 text-[11px] font-medium text-amber-300">
          <Sparkles className="w-3 h-3 shrink-0" />
          <span>{filing.trigger_reason}</span>
        </div>
      )}

      {/* Top row: Ticker + Category badge + Timestamp */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <button
            type="button"
            onClick={() => onSelectTicker?.(filing.ticker)}
            className="inline-flex items-center gap-1 rounded-md bg-zinc-800 hover:bg-zinc-700 px-2 py-0.5 text-xs font-mono font-bold text-white tracking-tight transition"
            title={`Open ${filing.ticker} Deep Pull Research`}
          >
            {filing.ticker}
            <ArrowUpRight className="w-3 h-3 text-zinc-400" />
          </button>
          <span
            className={`inline-flex items-center rounded-md border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${catMeta.cls}`}
          >
            {catMeta.label}
          </span>
        </div>

        <div className="flex items-center gap-1.5 text-[11px] font-mono text-zinc-500 shrink-0">
          <span>{filing.exchange}</span>
          <span>•</span>
          <span>{timeLabel}</span>
        </div>
      </div>

      {/* 1-Line Investor Summary */}
      <p className="mt-2 text-[13px] font-medium leading-snug text-zinc-100">
        {filing.summary || filing.title}
      </p>

      {!compact && filing.summary && filing.title !== filing.summary && (
        <p className="mt-1 text-[11px] text-zinc-400 line-clamp-1">{filing.title}</p>
      )}

      {/* HERO CARD 1: Enriched Quarterly Results Table */}
      {hasResultsTable && enriched?.metrics && (
        <div className="mt-3 rounded-lg border border-emerald-500/25 bg-zinc-950/90 p-2.5">
          <div className="flex items-center justify-between border-b border-zinc-800/80 pb-1.5 mb-2">
            <div className="flex items-center gap-1.5">
              <span className="text-[11px] font-semibold text-emerald-400 uppercase tracking-wider">
                {enriched.period || "Latest Quarter"}
              </span>
              <span className="text-[10px] font-mono text-zinc-500">
                ({enriched.unit || "₹ Cr"})
              </span>
            </div>
            {enriched.source && (
              <span className="text-[10px] font-mono text-zinc-500 truncate max-w-[170px]">
                {enriched.source}
              </span>
            )}
          </div>

          <div className="grid grid-cols-4 gap-1 text-[10px] font-mono uppercase text-zinc-500 pb-1 border-b border-zinc-800/50">
            <div>Metric</div>
            <div className="text-right">Actual</div>
            <div className="text-right">QoQ %</div>
            <div className="text-right">YoY %</div>
          </div>

          <div className="divide-y divide-zinc-800/40 text-xs">
            {(
              [
                ["Revenue", enriched.metrics.revenue],
                ["EBITDA", enriched.metrics.ebitda],
                ["PBT", enriched.metrics.pbt],
                ["PAT", enriched.metrics.pat],
              ] as [string, MetricDelta | undefined][]
            ).map(([label, m]) => (
              <div key={label} className="grid grid-cols-4 items-center py-1.5 gap-1">
                <span className="font-medium text-zinc-300 text-[11px]">{label}</span>
                <span className="text-right font-mono font-semibold text-zinc-100 text-[11px]">
                  {m?.value || "—"}
                </span>
                <div className="flex justify-end">
                  <DeltaPill val={m?.qoq_pct} />
                </div>
                <div className="flex justify-end">
                  <DeltaPill val={m?.yoy_pct} />
                </div>
              </div>
            ))}
          </div>

          {enriched.takeaway && (
            <div className="mt-2 pt-2 border-t border-zinc-800/80 text-[11px] text-zinc-300 leading-relaxed">
              <span className="font-semibold text-emerald-400">Takeaway: </span>
              {enriched.takeaway}
            </div>
          )}
        </div>
      )}

      {/* HERO CARD 2: Enriched Management Change Bio */}
      {hasMgmtBio && enriched && (
        <div className="mt-3 rounded-lg border border-amber-500/25 bg-zinc-950/90 p-2.5">
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2">
              <div
                className={`p-1.5 rounded-md ${
                  (enriched.action || "").toLowerCase().includes("resign")
                    ? "bg-rose-500/15 text-rose-400"
                    : "bg-emerald-500/15 text-emerald-400"
                }`}
              >
                {(enriched.action || "").toLowerCase().includes("resign") ? (
                  <UserMinus className="w-3.5 h-3.5" />
                ) : (
                  <UserCheck className="w-3.5 h-3.5" />
                )}
              </div>
              <div>
                <div className="text-xs font-semibold text-zinc-100">
                  {enriched.person_name && enriched.person_name !== "Unknown"
                    ? enriched.person_name
                    : filing.company_name || filing.ticker}
                </div>
                <div className="text-[11px] text-zinc-400">
                  {enriched.role || "Key Managerial Personnel"}
                </div>
              </div>
            </div>

            {enriched.action && (
              <span
                className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide ${
                  enriched.action.toLowerCase().includes("resign")
                    ? "bg-rose-500/15 text-rose-300 border border-rose-500/30"
                    : "bg-emerald-500/15 text-emerald-300 border border-emerald-500/30"
                }`}
              >
                {enriched.action}
              </span>
            )}
          </div>

          {enriched.two_line_bio && (
            <p className="mt-2 text-[11.5px] leading-relaxed text-zinc-300 border-t border-zinc-800/80 pt-2">
              {enriched.two_line_bio}
            </p>
          )}

          {enriched.source_url && (
            <div className="mt-1.5 flex items-center justify-end">
              <a
                href={enriched.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-[10px] font-mono text-amber-400 hover:underline"
              >
                <span>Verified Profile / Web Source</span>
                <ExternalLink className="w-2.5 h-2.5" />
              </a>
            </div>
          )}
        </div>
      )}

      {/* Footer actions: PDF Link + On-Demand Enrich + Pull Jump */}
      <div className="mt-2.5 flex items-center justify-between gap-2 pt-2 border-t border-zinc-800/60">
        <div className="flex items-center gap-2">
          {filing.pdf_url ? (
            <a
              href={filing.pdf_url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 rounded-md bg-zinc-800/90 hover:bg-zinc-700 px-2 py-1 text-[11px] font-medium text-zinc-200 transition"
            >
              <FileText className="w-3 h-3 text-zinc-400" />
              <span>Exchange PDF</span>
              <ExternalLink className="w-2.5 h-2.5 text-zinc-500" />
            </a>
          ) : (
            <span className="text-[11px] text-zinc-500">No PDF attached</span>
          )}

          {canEnrich && (
            <button
              type="button"
              onClick={handleEnrich}
              disabled={enriching}
              className="inline-flex items-center gap-1 rounded-md bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/30 px-2 py-1 text-[11px] font-medium text-emerald-300 transition disabled:opacity-50"
            >
              {enriching ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <Sparkles className="w-3 h-3" />
              )}
              <span>{enriching ? "Extracting..." : "Deep Enrich"}</span>
            </button>
          )}
        </div>

        <button
          type="button"
          onClick={() => onSelectTicker?.(filing.ticker)}
          className="inline-flex items-center gap-1 text-[11px] font-medium text-zinc-400 hover:text-emerald-400 transition"
        >
          <Building2 className="w-3 h-3" />
          <span>Pull Research</span>
        </button>
      </div>
    </div>
  );
}

