"use client";

import React from "react";
import { X, Cpu, Zap, ShieldCheck, Layers } from "lucide-react";
import { CostSummaryResponse, CachedReportMeta } from "@/types/finn";

interface CostDrawerProps {
  open: boolean;
  onClose: () => void;
  costData: CostSummaryResponse | null;
  cachedReports: CachedReportMeta[];
}

const WORKFLOW_LABELS: Record<string, { label: string; tier: string; badgeCls: string }> = {
  push_batch_triage: {
    label: "Push: Bulk Filing Triage (17 Categories)",
    tier: "Tier 1 • Flash-Lite",
    badgeCls: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  },
  push_enrich_results_pdf: {
    label: "Push: Quarterly Results PDF Table Extraction",
    tier: "Tier 2 • Flash",
    badgeCls: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  },
  push_enrich_results_fallback: {
    label: "Push: Results Primary Table Extraction",
    tier: "Tier 2 • Flash",
    badgeCls: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  },
  push_mgmt_extract: {
    label: "Push: Executive Change PDF Extraction",
    tier: "Tier 2 • Flash",
    badgeCls: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  },
  push_mgmt_bio: {
    label: "Push: Executive Grounded Bio Synthesis",
    tier: "Tier 2 • Flash",
    badgeCls: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  },
  pull_concall_map: {
    label: "Pull [Map]: Concall & AR Guidance Extraction",
    tier: "Tier 2 • Flash",
    badgeCls: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  },
  pull_deep_synthesis: {
    label: "Pull [Reduce]: 8-Module & 4Q Narrative Audit",
    tier: "Tier 3 • Pro",
    badgeCls: "bg-purple-500/15 text-purple-300 border-purple-500/30",
  },
  pull_copilot_qa: {
    label: "Pull: Primary-Source Copilot Q&A",
    tier: "Tier 2 • Flash",
    badgeCls: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30",
  },
};

export function CostDrawer({
  open,
  onClose,
  costData,
  cachedReports,
}: CostDrawerProps) {
  if (!open) return null;

  const totals = costData?.totals || {
    total_calls: 0,
    tokens_in: 0,
    tokens_out: 0,
    cost_usd: 0,
  };
  const rows = costData?.by_workflow || [];

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/75 backdrop-blur-sm p-0 sm:p-4">
      <div className="w-full max-w-[430px] max-h-[88vh] overflow-y-auto rounded-t-2xl sm:rounded-2xl border border-zinc-800 bg-zinc-950 p-4 text-zinc-100 shadow-2xl">
        <div className="flex items-center justify-between border-b border-zinc-800 pb-3">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded-lg bg-emerald-500/15 text-emerald-400">
              <Cpu className="w-4 h-4" />
            </div>
            <div>
              <h2 className="text-sm font-bold text-white">
                AI Cost & 3-Tier Model Routing
              </h2>
              <p className="text-[11px] text-zinc-400">
                Live Vertex AI / Gemini token ledger & SQLite WAL cache telemetry
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-white"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Summary KPI Strip */}
        <div className="mt-3 grid grid-cols-3 gap-2">
          <div className="rounded-xl border border-zinc-800/90 bg-zinc-900/70 p-2.5">
            <div className="text-[10px] font-mono uppercase text-zinc-400">
              Total Spend
            </div>
            <div className="mt-1 text-base font-mono font-bold text-emerald-400">
              ${(totals.cost_usd || 0).toFixed(4)}
            </div>
            <div className="text-[10px] text-zinc-500">
              {totals.total_calls} LLM calls
            </div>
          </div>

          <div className="rounded-xl border border-zinc-800/90 bg-zinc-900/70 p-2.5">
            <div className="text-[10px] font-mono uppercase text-zinc-400">
              Tokens Processed
            </div>
            <div className="mt-1 text-base font-mono font-bold text-zinc-100">
              {((totals.tokens_in + totals.tokens_out) / 1000).toFixed(1)}k
            </div>
            <div className="text-[10px] text-zinc-500">
              {(totals.tokens_in / 1000).toFixed(1)}k in ·{" "}
              {(totals.tokens_out / 1000).toFixed(1)}k out
            </div>
          </div>

          <div className="rounded-xl border border-zinc-800/90 bg-zinc-900/70 p-2.5">
            <div className="text-[10px] font-mono uppercase text-zinc-400">
              Cached Pulls
            </div>
            <div className="mt-1 text-base font-mono font-bold text-cyan-400">
              {cachedReports.length} Stocks
            </div>
            <div className="text-[10px] text-zinc-500">&lt;20ms • $0.00/hit</div>
          </div>
        </div>

        {/* Routing Architecture Explanation */}
        <div className="mt-3 rounded-xl border border-zinc-800/80 bg-zinc-900/40 p-3 space-y-2 text-xs">
          <div className="flex items-center gap-1.5 font-semibold text-zinc-200">
            <Layers className="w-3.5 h-3.5 text-emerald-400" />
            <span>Disciplined Model Routing Strategy</span>
          </div>
          <div className="grid grid-cols-1 gap-1.5 text-[11px] text-zinc-300">
            <div className="flex items-start justify-between gap-2 rounded-lg bg-zinc-900/90 p-2 border border-zinc-800/70">
              <div>
                <span className="font-mono font-semibold text-emerald-400">
                  Tier 1: gemini-2.5-flash-lite
                </span>
                <p className="text-zinc-400 mt-0.5">
                  Batch-triages 15 NSE/BSE filings per call into 17 categories + 1-line digest.
                </p>
              </div>
              <span className="font-mono text-[10px] text-zinc-400 shrink-0">
                $0.10 / 1M
              </span>
            </div>

            <div className="flex items-start justify-between gap-2 rounded-lg bg-zinc-900/90 p-2 border border-zinc-800/70">
              <div>
                <span className="font-mono font-semibold text-cyan-400">
                  Tier 2: gemini-2.5-flash
                </span>
                <p className="text-zinc-400 mt-0.5">
                  Extracts QoQ/YoY P&amp;L tables, exec bios, Concall Map notes &amp; Copilot Q&amp;A.
                </p>
              </div>
              <span className="font-mono text-[10px] text-zinc-400 shrink-0">
                $0.30 / 1M
              </span>
            </div>

            <div className="flex items-start justify-between gap-2 rounded-lg bg-zinc-900/90 p-2 border border-zinc-800/70">
              <div>
                <span className="font-mono font-semibold text-purple-400">
                  Tier 3: gemini-2.5-pro
                </span>
                <p className="text-zinc-400 mt-0.5">
                  Reserved strictly for 8-module Pull synthesis &amp; 4-Quarter Narrative vs Numbers audit.
                </p>
              </div>
              <span className="font-mono text-[10px] text-zinc-400 shrink-0">
                $1.25 / 1M
              </span>
            </div>
          </div>
        </div>

        {/* Ledger Breakdown by Workflow */}
        <div className="mt-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-zinc-200">
              Live SQLite Cost Ledger Breakdown
            </span>
            <span className="text-[10px] font-mono text-zinc-500">
              llm_cost_ledger
            </span>
          </div>

          <div className="space-y-1.5">
            {rows.map((r, i) => {
              const meta = WORKFLOW_LABELS[r.workflow] || {
                label: r.workflow,
                tier: r.model,
                badgeCls: "bg-zinc-800 text-zinc-300 border-zinc-700",
              };
              return (
                <div
                  key={`${r.workflow}-${r.model}-${i}`}
                  className="rounded-lg border border-zinc-800/80 bg-zinc-900/60 p-2.5 flex items-center justify-between gap-2"
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <span
                        className={`px-1.5 py-0.5 rounded border text-[9px] font-mono font-semibold ${meta.badgeCls}`}
                      >
                        {meta.tier}
                      </span>
                      <span className="text-[10px] font-mono text-zinc-500">
                        {r.model}
                      </span>
                    </div>
                    <div className="text-xs font-medium text-zinc-200 mt-1 truncate">
                      {meta.label}
                    </div>
                    <div className="text-[10px] font-mono text-zinc-500 mt-0.5">
                      {r.calls} calls • {(r.total_tokens_in / 1000).toFixed(1)}k in /{" "}
                      {(r.total_tokens_out / 1000).toFixed(1)}k out
                    </div>
                  </div>
                  <div className="text-right font-mono text-xs font-bold text-emerald-400 shrink-0">
                    ${Number(r.total_cost_usd || 0).toFixed(4)}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* Cache Guardrail Note */}
        <div className="mt-3 rounded-lg bg-emerald-500/10 border border-emerald-500/25 p-2.5 flex items-start gap-2 text-[11px] text-emerald-200">
          <ShieldCheck className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
          <div>
            <span className="font-semibold">Content-Addressed PDF &amp; Quarter Cache:</span>{" "}
            Extracted Concall/AR PDF text is SHA256-cached on disk (`cache_pdfs/`) and synthesized Pull reports are cached per quarter in SQLite WAL (`pull_reports_cache`), serving repeat institutional lookups in &lt;20ms at $0.00 marginal cost.
          </div>
        </div>
      </div>
    </div>
  );
}

