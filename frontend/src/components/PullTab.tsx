"use client";

import React, { useState, useEffect, useRef } from "react";
import {
  Search,
  Sparkles,
  Zap,
  ExternalLink,
  FileText,
  Scale,
  TrendingUp,
  TrendingDown,
  Shield,
  Target,
  Send,
  Loader2,
  RefreshCw,
  CheckCircle2,
  AlertCircle,
  BookOpen,
  BarChart3,
  Coins,
} from "lucide-react";
import {
  CachedReportMeta,
  CashVsPatPoint,
  CopilotCitationLink,
  CopilotQAResponse,
  PortfolioItem,
  PullResponse,
  RoutingTier,
  TrajectoryPoint,
} from "@/types/finn";
import { finnApi } from "@/lib/api";

interface PullTabProps {
  pullData: PullResponse | null;
  loading: boolean;
  error: string | null;
  activeTicker: string;
  portfolio: PortfolioItem[];
  cachedReports: CachedReportMeta[];
  onSelectTicker: (ticker: string, force?: boolean) => void;
  onCostUpdated: () => void;
  /** Configured primary model per tier (from /api/cost), shown before a response reports the real one. */
  routing?: RoutingTier[];
}

const ADHOC_DEMO_TICKERS = ["DIXON", "POLYCAB", "INDHOTEL", "PERSISTENT"];

// ponytail: lightweight client-side NSE symbol + company name/alias directory resolves common investor searches (e.g. "Reliance", "Zomato" -> ETERNAL, "Taj" -> INDHOTEL) without an extra backend autocomplete roundtrip.
const NSE_DIRECTORY: { ticker: string; name: string; aliases?: string[] }[] = [
  { ticker: "RELIANCE", name: "Reliance Industries Ltd", aliases: ["ril", "jio"] },
  { ticker: "TCS", name: "Tata Consultancy Services Ltd", aliases: ["tata consultancy"] },
  { ticker: "HDFCBANK", name: "HDFC Bank Ltd", aliases: ["hdfc"] },
  { ticker: "INFY", name: "Infosys Ltd", aliases: ["infosys"] },
  { ticker: "TMPV", name: "Tata Motors Passenger Vehicles Ltd", aliases: ["tata motors", "tatamotors", "jlr"] },
  { ticker: "SUNPHARMA", name: "Sun Pharmaceutical Industries", aliases: ["sun pharma"] },
  { ticker: "TRENT", name: "Trent Ltd (Zudio / Westside)", aliases: ["zudio", "westside"] },
  { ticker: "LT", name: "Larsen & Toubro Ltd", aliases: ["l&t", "larsen"] },
  { ticker: "BAJFINANCE", name: "Bajaj Finance Ltd", aliases: ["bajaj", "bafin"] },
  { ticker: "MARUTI", name: "Maruti Suzuki India Ltd", aliases: ["suzuki"] },
  { ticker: "ETERNAL", name: "Eternal Ltd (Zomato / Blinkit)", aliases: ["zomato", "eternal", "blinkit"] },
  { ticker: "HAL", name: "Hindustan Aeronautics Ltd", aliases: ["hindustan aeronautics"] },
  { ticker: "ITC", name: "ITC Ltd", aliases: ["itc"] },
  { ticker: "TITAN", name: "Titan Company Ltd", aliases: ["tanishq"] },
  { ticker: "KPITTECH", name: "KPIT Technologies Ltd", aliases: ["kpit"] },
  { ticker: "CIPLA", name: "Cipla Ltd", aliases: ["cipla"] },
  { ticker: "DIXON", name: "Dixon Technologies (India) Ltd", aliases: ["dixon tech"] },
  { ticker: "POLYCAB", name: "Polycab India Ltd", aliases: ["polycab"] },
  { ticker: "INDHOTEL", name: "The Indian Hotels Company (Taj)", aliases: ["indian hotels", "taj", "ihcl"] },
  { ticker: "PERSISTENT", name: "Persistent Systems Ltd", aliases: ["persistent"] },
  { ticker: "ICICIBANK", name: "ICICI Bank Ltd", aliases: ["icici"] },
  { ticker: "SBIN", name: "State Bank of India", aliases: ["sbi"] },
  { ticker: "BHARTIARTL", name: "Bharti Airtel Ltd", aliases: ["airtel"] },
  { ticker: "BEL", name: "Bharat Electronics Ltd", aliases: ["bharat electronics"] },
  { ticker: "M&M", name: "Mahindra & Mahindra Ltd", aliases: ["mahindra"] },
  { ticker: "ULTRACEMCO", name: "UltraTech Cement Ltd", aliases: ["ultratech"] },
];

const QUICK_QUESTIONS = [
  "What drove operating margin & PAT performance over the last 4 quarters?",
  "How does CFO compare to PAT and what is driving working capital days?",
  "Summarize management's capex & growth guidance from recent concalls.",
];

function VerdictBadge({ verdict }: { verdict: string }) {
  const v = (verdict || "").toLowerCase();
  if (v.includes("beat")) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md bg-emerald-500/20 border border-emerald-500/40 px-2 py-0.5 text-[10px] font-mono font-bold uppercase text-emerald-300">
        <CheckCircle2 className="w-3 h-3" />
        {verdict}
      </span>
    );
  }
  if (v.includes("deliver") || v.includes("align")) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md bg-cyan-500/20 border border-cyan-500/40 px-2 py-0.5 text-[10px] font-mono font-bold uppercase text-cyan-300">
        <CheckCircle2 className="w-3 h-3" />
        {verdict}
      </span>
    );
  }
  if (v.includes("miss") || v.includes("diverg")) {
    return (
      <span className="inline-flex items-center gap-1 rounded-md bg-rose-500/20 border border-rose-500/40 px-2 py-0.5 text-[10px] font-mono font-bold uppercase text-rose-300">
        <AlertCircle className="w-3 h-3" />
        {verdict}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-md bg-amber-500/20 border border-amber-500/40 px-2 py-0.5 text-[10px] font-mono font-bold uppercase text-amber-300">
      {verdict || "Mixed"}
    </span>
  );
}

// ponytail: native SVG/Tailwind combo chart avoids pulling in recharts (~45 transitive packages & SSR ResponsiveContainer hydration warnings); ceiling: fixed 4-6 period bar+line view without pan/zoom; upgrade path: swap in Recharts ComposedChart if interactive brush/zoom is required.
function TrajectoryChart({ series }: { series: TrajectoryPoint[] }) {
  if (!series || series.length === 0) {
    return (
      <div className="text-xs text-zinc-500 py-4 text-center">
        No time-series data available.
      </div>
    );
  }
  const maxRev = Math.max(...series.map((d) => Math.abs(Number(d.revenue) || 0)), 1);

  return (
    <div className="mt-3 rounded-lg border border-zinc-800/80 bg-zinc-950/90 p-3">
      <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 mb-2">
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center gap-1">
            <span className="w-2 h-2 rounded-sm bg-emerald-500/80 inline-block" />
            Revenue (₹ Cr)
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="w-2 h-2 rounded-sm bg-cyan-400 inline-block" />
            PAT (₹ Cr)
          </span>
        </div>
        <span className="text-amber-400">OPM % Pill</span>
      </div>

      <div className="grid grid-cols-6 gap-1.5 items-end pt-2">
        {series.slice(-6).map((pt, i) => {
          const rev = Number(pt.revenue) || 0;
          const pat = Number(pt.pat) || 0;
          const opm = Number(pt.opm_pct) || 0;
          const revHeight = Math.max(12, Math.round((Math.abs(rev) / maxRev) * 88));
          const patHeight = Math.max(6, Math.round((Math.abs(pat) / maxRev) * 88));

          return (
            <div key={`${pt.period}-${i}`} className="flex flex-col items-center">
              <span className="mb-1 rounded bg-amber-500/15 border border-amber-500/30 px-1 py-0.5 text-[9px] font-mono font-semibold text-amber-300">
                {opm.toFixed(1)}%
              </span>

              <div className="h-24 w-full flex items-end justify-center gap-1 border-b border-zinc-800 pb-1">
                <div
                  className="w-3 rounded-t bg-emerald-500/80 transition-all"
                  style={{ height: `${revHeight}px` }}
                  title={`Revenue: ₹${rev.toLocaleString("en-IN")} Cr`}
                />
                <div
                  className={`w-3 rounded-t transition-all ${
                    pat >= 0 ? "bg-cyan-400" : "bg-rose-500"
                  }`}
                  style={{ height: `${patHeight}px` }}
                  title={`PAT: ₹${pat.toLocaleString("en-IN")} Cr`}
                />
              </div>

              <div className="mt-1 text-[9px] font-mono text-zinc-300 font-semibold truncate max-w-full">
                {pt.period.replace("20", "'")}
              </div>
              <div className="text-[9px] font-mono text-zinc-500">
                {rev >= 1000 ? `${(rev / 1000).toFixed(1)}k` : Math.round(rev)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function CashVsPatChart({ series }: { series: CashVsPatPoint[] }) {
  if (!series || series.length === 0) return null;
  const maxVal = Math.max(
    ...series.flatMap((d) => [Math.abs(Number(d.cfo) || 0), Math.abs(Number(d.pat) || 0)]),
    1
  );

  return (
    <div className="mt-3 rounded-lg border border-zinc-800/80 bg-zinc-950/90 p-3">
      <div className="flex items-center justify-between text-[10px] font-mono text-zinc-400 mb-2">
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center gap-1">
            <span className="w-2 h-2 rounded-sm bg-emerald-400 inline-block" />
            Operating Cash Flow (CFO)
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="w-2 h-2 rounded-sm bg-indigo-400 inline-block" />
            Net Profit (PAT)
          </span>
        </div>
        <span>₹ Cr</span>
      </div>

      <div className="grid grid-cols-4 gap-2 items-end pt-1">
        {series.slice(-4).map((pt, i) => {
          const cfo = Number(pt.cfo) || 0;
          const pat = Number(pt.pat) || 0;
          const cfoH = Math.max(10, Math.round((Math.abs(cfo) / maxVal) * 76));
          const patH = Math.max(10, Math.round((Math.abs(pat) / maxVal) * 76));

          return (
            <div key={`${pt.period}-${i}`} className="flex flex-col items-center">
              <div className="h-20 w-full flex items-end justify-center gap-1.5 border-b border-zinc-800 pb-1">
                <div
                  className={`w-4 rounded-t ${
                    cfo >= 0 ? "bg-emerald-400" : "bg-rose-500"
                  }`}
                  style={{ height: `${cfoH}px` }}
                  title={`CFO: ₹${cfo.toLocaleString("en-IN")} Cr`}
                />
                <div
                  className={`w-4 rounded-t ${
                    pat >= 0 ? "bg-indigo-400" : "bg-amber-500"
                  }`}
                  style={{ height: `${patH}px` }}
                  title={`PAT: ₹${pat.toLocaleString("en-IN")} Cr`}
                />
              </div>
              <div className="mt-1 text-[10px] font-mono font-semibold text-zinc-300">
                {pt.period}
              </div>
              <div className="text-[9px] font-mono text-zinc-500">
                {Math.round(cfo)} vs {Math.round(pat)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ponytail: zero-dependency React Markdown renderer converts **bold**, *italic*, `code`, [links](https://...), lists, and headings into safe React elements without dangerouslySetInnerHTML (XSS-proof); ceiling: no LaTeX or nested HTML blocks; upgrade path: react-markdown + remark-gfm if full CommonMark AST is needed.
function renderInlineMarkdown(text?: string | null): React.ReactNode {
  if (!text) return null;
  const tokenRegex =
    /(\[[^\]]+\]\(https?:\/\/[^\s)]+\)|`[^`]+`|\*\*\*[^*]+\*\*\*|\*\*[^*]+\*\*|__[^_]+__|(?<![a-zA-Z0-9*])\*[^*\n]+\*(?![*])|(?<![a-zA-Z0-9_])_[^_\n]+_(?![a-zA-Z0-9_]))/g;
  const parts = text.split(tokenRegex);
  return parts.map((part, idx) => {
    if (!part) return null;
    if (part.startsWith("[") && part.includes("](") && part.endsWith(")")) {
      const m = part.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/i);
      if (m) {
        return (
          <a
            key={idx}
            href={m[2]}
            target="_blank"
            rel="noopener noreferrer"
            className="text-cyan-400 underline hover:text-cyan-300"
          >
            {renderInlineMarkdown(m[1])}
          </a>
        );
      }
    }
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return (
        <code
          key={idx}
          className="rounded bg-zinc-800/90 px-1 py-0.5 font-mono text-[11px] text-cyan-300"
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    if (part.startsWith("***") && part.endsWith("***") && part.length > 6) {
      return (
        <strong key={idx} className="font-semibold italic text-white">
          {part.slice(3, -3)}
        </strong>
      );
    }
    if (
      ((part.startsWith("**") && part.endsWith("**")) ||
        (part.startsWith("__") && part.endsWith("__"))) &&
      part.length > 4
    ) {
      return (
        <strong key={idx} className="font-semibold text-white">
          {renderInlineMarkdown(part.slice(2, -2))}
        </strong>
      );
    }
    if (
      ((part.startsWith("*") && part.endsWith("*")) ||
        (part.startsWith("_") && part.endsWith("_"))) &&
      part.length > 2
    ) {
      return (
        <em key={idx} className="italic text-zinc-200">
          {renderInlineMarkdown(part.slice(1, -1))}
        </em>
      );
    }
    return <React.Fragment key={idx}>{part}</React.Fragment>;
  });
}

function FormattedMarkdown({ content }: { content: string }) {
  if (!content) return null;
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: React.ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const rawLine = lines[i];
    const trimmed = rawLine.trim();

    if (!trimmed) {
      i++;
      continue;
    }

    // Heading (#, ##, ###, ####)
    const headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (headingMatch) {
      blocks.push(
        <div
          key={`h-${i}`}
          className="font-bold text-white text-xs mt-1.5 mb-0.5"
        >
          {renderInlineMarkdown(headingMatch[2])}
        </div>
      );
      i++;
      continue;
    }

    // Unordered list (-, *, •)
    if (/^([-*•])\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^([-*•])\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^([-*•])\s+/, ""));
        i++;
      }
      blocks.push(
        <ul
          key={`ul-${i}`}
          className="space-y-1 pl-3.5 list-disc marker:text-cyan-400"
        >
          {items.map((it, idx) => (
            <li key={idx} className="leading-relaxed text-zinc-100">
              {renderInlineMarkdown(it)}
            </li>
          ))}
        </ul>
      );
      continue;
    }

    // Ordered list (1. or 1))
    if (/^\d+[.)]\s+/.test(trimmed)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+[.)]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^\d+[.)]\s+/, ""));
        i++;
      }
      blocks.push(
        <ol
          key={`ol-${i}`}
          className="space-y-1 pl-4 list-decimal marker:text-cyan-400"
        >
          {items.map((it, idx) => (
            <li key={idx} className="leading-relaxed text-zinc-100">
              {renderInlineMarkdown(it)}
            </li>
          ))}
        </ol>
      );
      continue;
    }

    // Paragraph (combine consecutive non-empty non-list lines)
    const paraLines: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^([-*•]|\d+[.)]|#{1,4})\s+/.test(lines[i].trim())
    ) {
      paraLines.push(lines[i].trim());
      i++;
    }
    blocks.push(
      <p key={`p-${i}`} className="leading-relaxed text-zinc-100">
        {renderInlineMarkdown(paraLines.join(" "))}
      </p>
    );
  }

  return <div className="space-y-1.5 text-xs">{blocks}</div>;
}

const MONTH_TOKENS = [
  "jan",
  "feb",
  "mar",
  "apr",
  "may",
  "jun",
  "jul",
  "aug",
  "sep",
  "oct",
  "nov",
  "dec",
];

function quarterToConcallMonths(low: string): { months: string[]; year: string | null } {
  const m = low.match(/\bq([1-4])\s*(?:fy)?\s*'?(\d{2,4})\b/);
  if (!m) return { months: [], year: null };
  const qNum = parseInt(m[1], 10);
  const rawYr = m[2];
  const fyYear = rawYr.length === 2 ? 2000 + parseInt(rawYr, 10) : parseInt(rawYr, 10);
  if (qNum === 1) return { months: ["jul", "aug", "jun"], year: String(fyYear - 1) };
  if (qNum === 2) return { months: ["oct", "nov", "sep"], year: String(fyYear - 1) };
  if (qNum === 3) return { months: ["jan", "feb", "dec"], year: String(fyYear) };
  return { months: ["apr", "may", "mar"], year: String(fyYear) };
}

function resolveCitationLink(
  rawCit: string | CopilotCitationLink,
  pullData: PullResponse | null
): CopilotCitationLink {
  if (typeof rawCit === "object" && rawCit !== null && rawCit.label) {
    if (
      (rawCit.kind === "webpage" || rawCit.kind === "document") &&
      rawCit.url &&
      /^https?:\/\//i.test(rawCit.url)
    ) {
      return rawCit;
    }
    if (rawCit.kind === "internal_report" && rawCit.section_id) {
      return rawCit;
    }
    if (!rawCit.available || rawCit.kind === "unavailable") {
      return { ...rawCit, available: false, kind: "unavailable" };
    }
  }

  const label = (typeof rawCit === "string" ? rawCit : rawCit?.label || "").trim();
  const low = label.toLowerCase();
  if (
    !label ||
    ["unavailable", "not available", "n/a", "none", "missing", "unspecified"].some((u) =>
      low.includes(u)
    )
  ) {
    return { label: label || "Source", kind: "unavailable", url: null, section_id: null, available: false };
  }

  const report = pullData?.report;
  const fundamentals = pullData?.fundamentals;
  const sourcesUsed = report?.sources_used;
  const rawSc = (sourcesUsed?.screener_url || fundamentals?.screener_url || "").trim();
  const baseSc = /^https?:\/\//i.test(rawSc) ? rawSc.split("#")[0] : "";

  const docsParsed = sourcesUsed?.documents_parsed || [];
  const concalls = fundamentals?.concalls || sourcesUsed?.concalls_available || [];
  const annualReports = fundamentals?.annual_reports || sourcesUsed?.annual_reports_available || [];
  const filings = sourcesUsed?.material_filings || [];

  // 1. Internal Synthesized Report sections
  if (
    [
      "synthesized report",
      "pull report",
      "research report",
      "narrative vs",
      "4q audit",
      "quarters audit",
      "bull vs",
      "bull/bear",
      "business snapshot",
      "financial snapshot",
      "cash quality",
      "forward guidance",
      "trajectory",
    ].some((k) => low.includes(k))
  ) {
    let secId = "pull-report-overview";
    if (["narrative", "4q", "audit", "promise"].some((k) => low.includes(k))) {
      secId = "pull-module-narrative";
    } else if (["trajectory", "growing", "compressing", "series"].some((k) => low.includes(k))) {
      secId = "pull-module-trajectory";
    } else if (["cash", "cfo", "balance sheet", "leverage", "borrowing"].some((k) => low.includes(k))) {
      secId = "pull-module-cash-bs";
    } else if (["guidance", "target", "outlook", "tone"].some((k) => low.includes(k))) {
      secId = "pull-module-guidance";
    } else if (["bull", "bear", "risk", "debate"].some((k) => low.includes(k))) {
      secId = "pull-module-bull-bear";
    } else if (["snapshot", "segment", "kpi", "overview", "business"].some((k) => low.includes(k))) {
      secId = "pull-module-snapshot";
    }
    return {
      label,
      kind: "internal_report",
      url: `#${secId}`,
      section_id: secId,
      available: true,
    };
  }

  // 2. Primary Documents: Annual Reports
  if (low.includes("annual report") || /\bar\b/.test(low)) {
    const arCandidates: { titleLow: string; url: string }[] = [];
    for (const ar of annualReports) {
      const u = (ar.url || "").trim();
      if (/^https?:\/\//i.test(u)) {
        arCandidates.push({ titleLow: (ar.title || "").toLowerCase(), url: u });
      }
    }
    for (const d of docsParsed) {
      if ((d.doc_type || "").toLowerCase().includes("annual")) {
        const u = (d.url || "").trim();
        if (/^https?:\/\//i.test(u)) {
          arCandidates.push({ titleLow: (d.period || "").toLowerCase(), url: u });
        }
      }
    }

    const yrMatch = low.match(/\b(20\d{2})\b/);
    const fyMatch = low.match(/\bfy\s*'?(\d{2})\b/);
    const targetYear = yrMatch ? yrMatch[1] : fyMatch ? `20${fyMatch[1]}` : null;

    let matchedUrl: string | null = null;
    if (targetYear) {
      const hit = arCandidates.find(
        (c) => c.titleLow.includes(targetYear) || c.titleLow.includes(`fy${targetYear.slice(-2)}`)
      );
      matchedUrl = hit ? hit.url : null;
    } else if (arCandidates.length > 0) {
      matchedUrl = arCandidates[0].url;
    }

    return {
      label,
      kind: matchedUrl ? "document" : "unavailable",
      url: matchedUrl,
      section_id: null,
      available: Boolean(matchedUrl),
    };
  }

  // 3. Primary Documents: Concalls / Transcripts / Investor Presentations / PPTs
  if (
    ["concall", "transcript", "earnings call", "conference call", "presentation", "ppt", "investor deck"].some(
      (k) => low.includes(k)
    )
  ) {
    const wantsPpt = ["ppt", "presentation", "deck"].some((k) => low.includes(k));
    const ccEntries: {
      period: string;
      transcript_url?: string;
      ppt_url?: string;
      url?: string;
      audio_url?: string;
    }[] = [...concalls];

    for (const d of docsParsed) {
      if (!(d.doc_type || "").toLowerCase().includes("annual")) {
        ccEntries.push({
          period: d.period || "",
          transcript_url: (d.doc_type || "").toLowerCase().includes("transcript") ? d.url : "",
          ppt_url: (d.doc_type || "").toLowerCase().includes("presentation") ? d.url : "",
          url: d.url || "",
        });
      }
    }

    const pickUrl = (entry: {
      transcript_url?: string;
      ppt_url?: string;
      url?: string;
      audio_url?: string;
    }): string | null => {
      const candidates = wantsPpt
        ? [entry.ppt_url, entry.url, entry.transcript_url, entry.audio_url]
        : [entry.transcript_url, entry.url, entry.ppt_url, entry.audio_url];
      for (const c of candidates) {
        const v = (c || "").trim();
        if (/^https?:\/\//i.test(v)) return v;
      }
      return null;
    };

    const explicitMonths = MONTH_TOKENS.filter((m) => new RegExp(`\\b${m}[a-z]*\\b`).test(low));
    const explicitYearMatch = low.match(/\b(20\d{2})\b/);
    const explicitYear = explicitYearMatch ? explicitYearMatch[1] : null;
    const { months: qMonths, year: qYear } = quarterToConcallMonths(low);
    const hasSpecificPeriod =
      explicitMonths.length > 0 || Boolean(explicitYear) || qMonths.length > 0;

    let matchedCcUrl: string | null = null;
    if (explicitMonths.length > 0 || explicitYear) {
      for (const entry of ccEntries) {
        const pLow = (entry.period || "").toLowerCase();
        const monthOk =
          explicitMonths.length > 0 ? explicitMonths.every((m) => pLow.includes(m)) : true;
        const yearOk = explicitYear ? pLow.includes(explicitYear) : true;
        if (monthOk && yearOk) {
          matchedCcUrl = pickUrl(entry);
          if (matchedCcUrl) break;
        }
      }
    }

    if (!matchedCcUrl && qMonths.length > 0) {
      for (const entry of ccEntries) {
        const pLow = (entry.period || "").toLowerCase();
        if (qMonths.some((m) => pLow.includes(m)) && (!qYear || pLow.includes(qYear))) {
          matchedCcUrl = pickUrl(entry);
          if (matchedCcUrl) break;
        }
      }
    }

    if (!hasSpecificPeriod && !matchedCcUrl) {
      for (const entry of ccEntries) {
        matchedCcUrl = pickUrl(entry);
        if (matchedCcUrl) break;
      }
    }

    return {
      label,
      kind: matchedCcUrl ? "document" : "unavailable",
      url: matchedCcUrl,
      section_id: null,
      available: Boolean(matchedCcUrl),
    };
  }

  // 4. Exchange Filings / Press Releases
  if (["filing", "announcement", "exchange", "press release", "bse", "nse"].some((k) => low.includes(k))) {
    const fHit = filings.find((f) => /^https?:\/\//i.test((f.pdf_url || "").trim()));
    return {
      label,
      kind: fHit ? "document" : "unavailable",
      url: fHit ? fHit.pdf_url : null,
      section_id: null,
      available: Boolean(fHit),
    };
  }

  // 5. Screener Financial Tables & Ratios
  if (
    baseSc &&
    [
      "screener",
      "table",
      "quarter",
      "q1",
      "q2",
      "q3",
      "q4",
      "balance sheet",
      "cash flow",
      "cfo",
      "profit & loss",
      "profit and loss",
      "p&l",
      "income statement",
      "ratio",
      "roce",
      "roe",
      "working capital",
      "10y",
      "financial",
      "fy2",
      "mar 20",
      "jun 20",
      "sep 20",
      "dec 20",
    ].some((k) => low.includes(k))
  ) {
    let scUrl = baseSc;
    if (low.includes("balance sheet")) {
      scUrl = `${baseSc}#balance-sheet`;
    } else if (low.includes("cash flow") || low.includes("cfo")) {
      scUrl = `${baseSc}#cash-flow`;
    } else if (
      ["profit & loss", "profit and loss", "p&l", "income statement", "10y", "annual p&l"].some((k) =>
        low.includes(k)
      )
    ) {
      scUrl = `${baseSc}#profit-loss`;
    } else if (low.includes("top ratio") || low.includes("market cap") || low.includes("valuation")) {
      scUrl = `${baseSc}#top`;
    } else if (
      ["ratio", "working capital", "roce", "roe", "cash conversion"].some((k) => low.includes(k))
    ) {
      scUrl = `${baseSc}#ratios`;
    } else if (
      ["quarter", "q1", "q2", "q3", "q4", "table", "jun ", "sep ", "dec ", "mar "].some((k) =>
        low.includes(k)
      )
    ) {
      scUrl = `${baseSc}#quarters`;
    }
    return {
      label,
      kind: "webpage",
      url: scUrl,
      section_id: null,
      available: true,
    };
  }

  return {
    label,
    kind: "unavailable",
    url: null,
    section_id: null,
    available: false,
  };
}

function SourceBadge({
  link,
  onJumpToSection,
}: {
  link: CopilotCitationLink;
  onJumpToSection: (sectionId: string, label: string) => void;
}) {
  if (!link.available || link.kind === "unavailable") {
    return (
      <span
        aria-disabled="true"
        title="Direct source link is unavailable in primary filings"
        className="inline-flex items-center gap-1 rounded bg-zinc-900/60 border border-zinc-700/70 px-2 py-0.5 text-[9.5px] font-mono text-zinc-500 cursor-not-allowed"
      >
        <AlertCircle className="w-2.5 h-2.5 shrink-0 text-amber-500/70" />
        <span>{link.label}</span>
        <span className="text-[8.5px] uppercase text-amber-400/80">(Unavailable)</span>
      </span>
    );
  }

  if (link.kind === "internal_report" && link.section_id) {
    return (
      <a
        href={`#${link.section_id}`}
        onClick={(e) => {
          e.preventDefault();
          onJumpToSection(link.section_id!, link.label);
        }}
        title={`Open section in synthesized report: ${link.label}`}
        className="inline-flex items-center gap-1 rounded bg-zinc-900 hover:bg-emerald-950/80 border border-emerald-500/35 hover:border-emerald-400 px-2 py-0.5 text-[9.5px] font-mono text-emerald-300 hover:text-emerald-200 transition cursor-pointer"
      >
        <BookOpen className="w-2.5 h-2.5 shrink-0" />
        <span>{link.label}</span>
        <span className="text-[8.5px] uppercase text-emerald-400/90">↘ Report</span>
      </a>
    );
  }

  return (
    <a
      href={link.url || "#"}
      target="_blank"
      rel="noopener noreferrer"
      title={`Open supporting ${link.kind === "document" ? "primary filing PDF" : "Screener financial table"}: ${link.label}`}
      className="inline-flex items-center gap-1 rounded bg-zinc-900 hover:bg-cyan-950/80 border border-cyan-500/35 hover:border-cyan-400 px-2 py-0.5 text-[9.5px] font-mono text-cyan-300 hover:text-cyan-200 transition cursor-pointer"
    >
      <FileText className="w-2.5 h-2.5 shrink-0" />
      <span>{link.label}</span>
      <ExternalLink className="w-2.5 h-2.5 shrink-0 opacity-85" />
    </a>
  );
}

export function PullTab({
  pullData,
  loading,
  error,
  activeTicker,
  portfolio,
  cachedReports,
  onSelectTicker,
  onCostUpdated,
  routing,
}: PullTabProps) {
  const tierModel = (tier: number) => routing?.find((t) => t.tier === tier)?.model;
  const [searchInput, setSearchInput] = useState("");
  const [trajMode, setTrajMode] = useState<"quarterly" | "annual">("quarterly");
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [qaHistory, setQaHistory] = useState<CopilotQAResponse[]>([]);
  const [qaError, setQaError] = useState<string | null>(null);
  const [highlightedSection, setHighlightedSection] = useState<{
    id: string;
    label: string;
  } | null>(null);
  const askReqIdRef = useRef<number>(0);
  const askAbortRef = useRef<AbortController | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [rerunError, setRerunError] = useState<string | null>(null);
  const activeTickerRef = useRef(activeTicker);
  activeTickerRef.current = activeTicker;

  // Re-run from the stale banner keeps the old report on screen until the new one is cached, then reloads it ($0).
  async function handleStaleRerun(ticker: string) {
    setRerunning(true);
    setRerunError(null);
    try {
      await finnApi.getPullReport(ticker, true);
      onCostUpdated();
      if (activeTickerRef.current === ticker) onSelectTicker(ticker, false);
    } catch (err) {
      if (activeTickerRef.current === ticker) {
        setRerunError(err instanceof Error ? err.message : "Re-run failed. Try again.");
      }
    } finally {
      if (activeTickerRef.current === ticker) setRerunning(false);
    }
  }

  useEffect(() => {
    setRerunning(false);
    setRerunError(null);
    askReqIdRef.current += 1;
    if (askAbortRef.current) {
      askAbortRef.current.abort();
      askAbortRef.current = null;
    }
    setAsking(false);
    setQaError(null);
    setQuestion("");
    setHighlightedSection(null);
  }, [activeTicker]);

  function handleJumpToSection(sectionId: string, label: string) {
    setHighlightedSection({ id: sectionId, label });
    if (typeof window !== "undefined") {
      window.history.replaceState(null, "", `#${sectionId}`);
      const el = document.getElementById(sectionId);
      if (el) {
        el.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
  }

  const cachedSet = new Set(cachedReports.map((c) => c.ticker));

  // Merge portfolio + NSE_DIRECTORY for instant client-side company name/alias autocomplete
  const directoryList = React.useMemo(() => {
    const map = new Map<string, { ticker: string; name: string; aliases: string[] }>();
    for (const item of NSE_DIRECTORY) {
      map.set(item.ticker, {
        ticker: item.ticker,
        name: item.name,
        aliases: item.aliases || [],
      });
    }
    for (const p of portfolio) {
      if (!map.has(p.ticker)) {
        map.set(p.ticker, {
          ticker: p.ticker,
          name: p.company_name || p.ticker,
          aliases: [],
        });
      }
    }
    return Array.from(map.values());
  }, [portfolio]);

  const suggestions = React.useMemo(() => {
    const q = searchInput.trim().toLowerCase().replace(".ns", "").replace(".bo", "");
    if (!q) return [];
    return directoryList
      .filter(
        (d) =>
          d.ticker.toLowerCase().includes(q) ||
          d.name.toLowerCase().includes(q) ||
          d.aliases.some((a) => a.toLowerCase().includes(q))
      )
      .slice(0, 5);
  }, [searchInput, directoryList]);

  function resolveTickerSymbol(raw: string): string {
    const cleaned = raw.trim().toUpperCase().replace(".NS", "").replace(".BO", "");
    if (!cleaned) return "";
    const exact = directoryList.find((d) => d.ticker === cleaned);
    if (exact) return exact.ticker;
    const q = cleaned.toLowerCase();
    const matched = directoryList.find(
      (d) =>
        d.name.toLowerCase().includes(q) ||
        d.aliases.some((a) => a.toLowerCase() === q || a.toLowerCase().includes(q))
    );
    return matched ? matched.ticker : cleaned;
  }

  function handleSearchSubmit(e: React.FormEvent) {
    e.preventDefault();
    const resolved = resolveTickerSymbol(searchInput);
    if (resolved) {
      onSelectTicker(resolved, false);
      setSearchInput("");
    }
  }

  async function handleAskQuestion(qText: string) {
    const cleanQ = qText.trim();
    const targetTicker = pullData?.ticker?.trim().toUpperCase();
    if (!cleanQ || !targetTicker || targetTicker !== activeTicker.toUpperCase()) return;

    const reqId = ++askReqIdRef.current;
    if (askAbortRef.current) {
      askAbortRef.current.abort();
    }
    const controller = new AbortController();
    askAbortRef.current = controller;

    try {
      setAsking(true);
      setQaError(null);
      // qaHistory is newest-first; send this ticker's last 2 turns oldest-first, trimmed to the backend's field limits.
      const history = qaHistory
        .filter((t) => t.ticker === targetTicker)
        .slice(0, 2)
        .reverse()
        .map((t) => ({ question: t.question.slice(0, 1000), answer: t.answer.slice(0, 4000) }));
      const res = await finnApi.askPullCopilot(targetTicker, cleanQ, history, controller.signal);
      if (reqId !== askReqIdRef.current) return;
      if ((res.ticker || "").toUpperCase() !== targetTicker) return;
      setQaHistory((prev) => [res, ...prev]);
      setQuestion("");
      onCostUpdated();
    } catch (err) {
      if (reqId !== askReqIdRef.current) return;
      if (err instanceof Error && err.name === "AbortError") return;
      const rawMsg = err instanceof Error ? err.message : "Unable to reach Copilot right now.";
      const friendly =
        rawMsg.includes("HTTPSConnectionPool") || rawMsg.includes("WinError 10013")
          ? "Cannot reach Google AI service (outbound HTTPS socket blocked). Restart the backend in a network-authorized terminal."
          : rawMsg.length > 220
          ? "Copilot could not complete this request right now. Please try again."
          : rawMsg;
      setQaError(friendly);
    } finally {
      if (reqId === askReqIdRef.current) {
        setAsking(false);
      }
    }
  }

  const isMatchingReport = Boolean(
    pullData &&
      pullData.ticker &&
      pullData.ticker.toUpperCase() === activeTicker.toUpperCase()
  );
  const report = isMatchingReport ? pullData?.report : undefined;
  const eod = isMatchingReport ? pullData?.eod : undefined;
  const companyName =
    pullData?.fundamentals?.company_name || eod?.company_name || pullData?.ticker;

  return (
    <div className="space-y-4 pb-8">
      {/* Search Any NSE/BSE Ticker Bar */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-900/90 p-3">
        <form onSubmit={handleSearchSubmit} className="relative">
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search className="w-3.5 h-3.5 text-zinc-400 absolute left-3 top-1/2 -translate-y-1/2" />
              <input
                type="text"
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                placeholder="Ticker or company"
                className="w-full rounded-lg border border-zinc-800 bg-zinc-950 pl-8 pr-3 min-h-11 text-base text-white placeholder-zinc-500 focus:border-emerald-500 focus:outline-none font-mono"
              />
            </div>
            <button
              type="submit"
              disabled={!searchInput.trim()}
              className="min-h-11 shrink-0 rounded-lg bg-emerald-500 hover:bg-emerald-400 px-3 text-xs font-bold text-zinc-950 transition disabled:opacity-50"
            >
              Analyze
            </button>
          </div>

          {/* Client-side Autocomplete Dropdown */}
          {suggestions.length > 0 && (
            <div className="mt-1.5 rounded-lg border border-zinc-700 bg-zinc-950 shadow-xl divide-y divide-zinc-800/80 overflow-hidden">
              {suggestions.map((item) => {
                const isCached = cachedSet.has(item.ticker);
                return (
                  <button
                    key={item.ticker}
                    type="button"
                    onClick={() => {
                      onSelectTicker(item.ticker, false);
                      setSearchInput("");
                    }}
                    className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-zinc-900 transition"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-mono text-xs font-bold text-emerald-400">
                        {item.ticker}
                      </span>
                      <span className="text-xs text-zinc-300 truncate">
                        {item.name}
                      </span>
                    </div>
                    {isCached ? (
                      <span className="inline-flex items-center gap-0.5 rounded bg-emerald-500/15 border border-emerald-500/30 px-1.5 py-0.5 text-[9px] font-mono text-emerald-300 shrink-0">
                        <Zap className="w-2.5 h-2.5" />
                        Instant Cache
                      </span>
                    ) : (
                      <span className="text-[10px] font-mono text-zinc-500 shrink-0">
                        NSE
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </form>

        {/* Quick Select Chips: Portfolio + Outside-Portfolio Ad-Hoc Tickers */}
        <div className="mt-2.5 space-y-1.5">
          <div className="flex items-center gap-1.5 overflow-x-auto no-scrollbar pb-0.5">
            <span className="text-[10px] font-mono uppercase text-zinc-500 shrink-0">
              Portfolio:
            </span>
            {portfolio.map((p) => {
              const isCached = cachedSet.has(p.ticker);
              const isSelected = activeTicker === p.ticker;
              return (
                <button
                  key={p.ticker}
                  type="button"
                  onClick={() => onSelectTicker(p.ticker, false)}
                  className={`shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-mono font-medium border transition ${
                    isSelected
                      ? "bg-emerald-500 text-zinc-950 border-emerald-400 font-bold"
                      : isCached
                      ? "bg-emerald-500/10 text-emerald-300 border-emerald-500/30 hover:bg-emerald-500/20"
                      : "bg-zinc-950 text-zinc-400 border-zinc-800 hover:text-zinc-200"
                  }`}
                  title={isCached ? "Cached (<25ms instant load)" : "On-demand synthesis"}
                >
                  {isCached && (
                    <Zap
                      className={`w-2.5 h-2.5 ${
                        isSelected ? "text-zinc-950" : "text-emerald-400"
                      }`}
                    />
                  )}
                  <span>{p.ticker}</span>
                </button>
              );
            })}
          </div>

          <div className="flex items-center gap-1.5 overflow-x-auto no-scrollbar">
            <span className="text-[10px] font-mono uppercase text-amber-400/90 shrink-0">
              Ad-Hoc (Non-Portfolio):
            </span>
            {ADHOC_DEMO_TICKERS.map((t) => {
              const isCached = cachedSet.has(t);
              const isSelected = activeTicker === t;
              return (
                <button
                  key={t}
                  type="button"
                  onClick={() => onSelectTicker(t, false)}
                  className={`shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-mono font-medium border transition ${
                    isSelected
                      ? "bg-amber-400 text-zinc-950 border-amber-300 font-bold"
                      : isCached
                      ? "bg-amber-500/15 text-amber-300 border-amber-500/35"
                      : "bg-zinc-950 text-zinc-300 border-zinc-800 hover:border-amber-500/40"
                  }`}
                >
                  {isCached && <Zap className="w-2.5 h-2.5 text-amber-400" />}
                  <span>{t}</span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Loading State for Cold Pull Synthesis */}
      {loading && (
        <div className="rounded-xl border border-emerald-500/30 bg-zinc-900/90 p-5 text-center space-y-3">
          <Loader2 className="w-7 h-7 text-emerald-400 animate-spin mx-auto" />
          <div>
            <h3 className="text-sm font-bold text-white">
              Synthesizing Primary-Source Report for {activeTicker}...
            </h3>
            <p className="text-xs text-zinc-400 mt-1">
              1. Scraping Screener 10Y P&amp;L, Balance Sheet, Cash Flow &amp; Quarterly tables
              <br />
              2. Extracting last 4 Concall Transcripts / Annual Report via PyMuPDF
              <br />
              3. Running Map ({tierModel(2) ?? "Tier 2"}) → Reduce ({tierModel(3) ?? "Tier 3"}) 4Q Audit
            </p>
          </div>
        </div>
      )}

      {/* Error State */}
      {!loading && error && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-950/20 p-4 text-xs text-rose-300 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <div className="font-bold">
              Research Synthesis Error ({activeTicker})
            </div>
            <button
              type="button"
              onClick={() => onSelectTicker(activeTicker, false)}
              className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-lg bg-rose-500/20 hover:bg-rose-500/30 border border-rose-500/40 px-3 text-xs font-mono text-rose-200 transition"
            >
              <RefreshCw className="w-2.5 h-2.5" />
              <span>Retry</span>
            </button>
          </div>
          <p className="leading-relaxed">{error}</p>
        </div>
      )}

      {/* Loaded Report Content (strictly guarded to match activeTicker and have no error) */}
      {!loading && !error && isMatchingReport && pullData && report && (
        <>
          {/* Stale report banner: flagged by the backend, re-run only on an explicit tap */}
          {pullData.stale && (
            <div className="rounded-xl border border-amber-500/40 bg-amber-950/25 p-3 space-y-2.5">
              <div className="flex items-start gap-2">
                <AlertCircle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
                <div className="min-w-0 flex-1">
                  <div className="text-[11px] font-mono font-bold uppercase tracking-wider text-amber-300">
                    Report may be out of date
                  </div>
                  <p className="mt-0.5 text-sm leading-snug text-amber-50 break-words">
                    {pullData.stale_reason}
                  </p>
                  {pullData.screener_lag && (
                    <p className="mt-1.5 text-xs leading-relaxed text-zinc-300">
                      Screener&apos;s tables haven&apos;t updated to the new quarter yet, so a re-run now
                      won&apos;t pick it up. Try again in 1-2 days.
                    </p>
                  )}
                </div>
              </div>
              <button
                type="button"
                onClick={() => handleStaleRerun(pullData.ticker)}
                disabled={rerunning}
                className="w-full sm:w-auto min-h-[44px] inline-flex items-center justify-center gap-1.5 rounded-lg bg-amber-500/20 hover:bg-amber-500/30 border border-amber-500/50 px-4 text-sm font-mono font-semibold text-amber-200 transition disabled:opacity-60 disabled:cursor-wait"
              >
                {rerunning ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <RefreshCw className="w-4 h-4" />
                )}
                <span>
                  {rerunning
                    ? "Re-running…"
                    : `Re-run Report (~$${pullData.cost_usd > 0 ? pullData.cost_usd.toFixed(2) : "0.10"})`}
                </span>
              </button>
              {rerunning && (
                <p className="text-xs text-zinc-400">
                  Scraping Screener and filings, then re-synthesizing. This takes 1-2 minutes.
                </p>
              )}
              {rerunError && (
                <p role="alert" className="rounded-lg border border-rose-500/35 bg-rose-950/40 px-2.5 py-2 text-xs text-rose-300 break-words">
                  Re-run failed: {rerunError}
                </p>
              )}
            </div>
          )}

          {/* Company Header + Cache/Cost Telemetry + Primary Source Pills */}
          <div
            id="pull-report-overview"
            className={`rounded-xl border bg-zinc-900/90 p-3.5 transition ${
              highlightedSection?.id === "pull-report-overview"
                ? "border-emerald-400 ring-2 ring-emerald-400/50"
                : "border-zinc-800"
            }`}
          >
            {highlightedSection?.id === "pull-report-overview" && (
              <div className="mb-2 flex items-center justify-between rounded bg-emerald-500/15 border border-emerald-500/35 px-2 py-1 text-[10px] font-mono text-emerald-300">
                <span>Viewing Cited Report: {highlightedSection.label}</span>
                <button
                  type="button"
                  onClick={() => setHighlightedSection(null)}
                  className="text-zinc-400 hover:text-white"
                >
                  ✕
                </button>
              </div>
            )}
            <div className="flex items-start justify-between gap-2">
              <div>
                <div className="flex items-center gap-2">
                  <span className="text-base font-mono font-extrabold text-white">
                    {pullData.ticker}
                  </span>
                  <span className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-mono text-zinc-300">
                    {pullData.quarter_key}
                  </span>
                </div>
                <h1 className="text-xs font-medium text-zinc-300 mt-0.5">
                  {companyName}
                </h1>
              </div>

              {eod?.close_price && (
                <div className="text-right">
                  <div className="text-sm font-mono font-bold text-white">
                    ₹{Number(eod.close_price).toLocaleString("en-IN")}
                  </div>
                  <div
                    className={`text-[11px] font-mono font-semibold ${
                      Number(eod.pct_change || 0) >= 0
                        ? "text-emerald-400"
                        : "text-rose-400"
                    }`}
                  >
                    {Number(eod.pct_change || 0) >= 0 ? "+" : ""}
                    {Number(eod.pct_change || 0).toFixed(2)}% (1D)
                  </div>
                </div>
              )}
            </div>

            {/* Cost & Cache Telemetry Strip */}
            <div className="mt-2.5 flex items-center justify-between gap-2 pt-2.5 border-t border-zinc-800/80">
              <div className="flex items-center gap-1.5 flex-wrap">
                {pullData.cached ? (
                  <span className="inline-flex items-center gap-1 rounded-md bg-emerald-500/15 border border-emerald-500/30 px-2 py-0.5 text-[10px] font-mono font-semibold text-emerald-300">
                    <Zap className="w-3 h-3" />
                    Cached (&lt;25ms • $0.00)
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1 rounded-md bg-purple-500/15 border border-purple-500/30 px-2 py-0.5 text-[10px] font-mono font-semibold text-purple-300">
                    <Sparkles className="w-3 h-3" />
                    Fresh Synthesis
                  </span>
                )}
                <span className="text-[10px] font-mono text-zinc-400">
                  Gen Cost: ${Number(pullData.cost_usd || 0).toFixed(4)} (
                  {((pullData.tokens_in + pullData.tokens_out) / 1000).toFixed(1)}k tok)
                </span>
                {report.models_used?.reduce && (
                  <span
                    className="text-[10px] font-mono text-purple-300"
                    title={`Map: ${report.models_used.map ?? "not run"} · Reduce: ${report.models_used.reduce}`}
                  >
                    via {report.models_used.reduce}
                  </span>
                )}
              </div>

              <button
                type="button"
                onClick={() => onSelectTicker(pullData.ticker, true)}
                className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded bg-zinc-800 hover:bg-zinc-700 px-2 py-0.5 text-[10px] font-mono text-zinc-300"
                title="Force fresh scrape and Gemini Pro synthesis"
              >
                <RefreshCw className="w-2.5 h-2.5" />
                <span>Re-run</span>
              </button>
            </div>

            {/* Grounded Primary Sources Used */}
            <div className="mt-2.5 pt-2 border-t border-zinc-800/60 flex items-center gap-1.5 flex-wrap">
              <span className="text-[10px] font-mono uppercase text-zinc-500">
                Primary Sources:
              </span>
              {report.sources_used?.screener_url && (
                <a
                  href={report.sources_used.screener_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 rounded bg-zinc-800/90 hover:bg-zinc-700 px-1.5 py-0.5 text-[10px] font-mono text-emerald-300"
                >
                  <span>10Y Financials</span>
                  <ExternalLink className="w-2.5 h-2.5" />
                </a>
              )}
              {(report.sources_used?.documents_parsed || []).map((doc, i) => (
                <a
                  key={i}
                  href={doc.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 rounded bg-zinc-800/90 hover:bg-zinc-700 px-1.5 py-0.5 text-[10px] font-mono text-cyan-300"
                >
                  <FileText className="w-2.5 h-2.5" />
                  <span>
                    {doc.doc_type === "Concall Transcript" ? "Concall" : doc.doc_type}:{" "}
                    {doc.period}
                  </span>
                </a>
              ))}
            </div>
          </div>

          {/* UPPER HERO SPOTLIGHT: PRIMARY-SOURCE COPILOT COMMAND DECK */}
          <div className="relative overflow-hidden rounded-xl border-2 border-cyan-500/45 bg-gradient-to-br from-cyan-950/40 via-zinc-900/95 to-emerald-950/25 p-3.5 shadow-lg shadow-cyan-950/25">
            <div className="flex items-start justify-between gap-2">
              <div className="flex items-center gap-2">
                <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-cyan-500/20 border border-cyan-400/40 text-cyan-300 shadow-inner">
                  <Sparkles className="w-4 h-4" />
                </div>
                <div>
                  <div className="flex items-center gap-1.5">
                    <h2 className="text-xs font-extrabold uppercase tracking-wider text-white">
                      Ask {pullData.ticker} Primary-Source Copilot
                    </h2>
                    <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full bg-cyan-500/20 border border-cyan-400/35 px-1.5 py-0.5 text-[9px] font-mono font-semibold text-cyan-300">
                      <span className="h-1.5 w-1.5 rounded-full bg-cyan-400 animate-pulse" />
                      LIVE Q&amp;A
                    </span>
                  </div>
                  <p className="text-xs text-zinc-300 mt-0.5">
                    Answers from Screener 10Y &amp; quarterly tables, this report, and distilled notes from the concalls &amp; annual report it parsed. Cites only those sources.
                  </p>
                </div>
              </div>
              <span className="shrink-0 rounded bg-zinc-950/90 border border-zinc-800 px-1.5 py-0.5 text-[9px] font-mono text-cyan-300">
                {qaHistory.find((q) => q.model)?.model ?? tierModel(2) ?? "Tier 2"}
              </span>
            </div>

            {/* Command Input Bar */}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                handleAskQuestion(question);
              }}
              className="mt-3 flex items-center gap-2"
            >
              <input
                type="text"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder={`Ask about ${pullData.ticker}...`}
                className="flex-1 rounded-lg border border-cyan-500/35 bg-zinc-950/95 px-3 min-h-11 text-base text-white placeholder-zinc-400 focus:border-cyan-400 focus:outline-none shadow-inner"
              />
              <button
                type="submit"
                disabled={asking || !question.trim()}
                className="inline-flex min-h-11 shrink-0 items-center gap-1.5 rounded-lg bg-cyan-400 hover:bg-cyan-300 px-3.5 text-xs font-extrabold text-zinc-950 shadow-sm transition disabled:opacity-50"
              >
                {asking ? (
                  <>
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                    <span>Auditing...</span>
                  </>
                ) : (
                  <>
                    <Send className="w-3.5 h-3.5" />
                    <span>Ask</span>
                  </>
                )}
              </button>
            </form>

            {/* 1-Tap Institutional Audit Prompts */}
            <div className="mt-2.5 flex items-center gap-1.5 overflow-x-auto no-scrollbar pb-0.5">
              <span className="text-[9.5px] font-mono uppercase text-cyan-300/80 shrink-0">
                1-Tap Audit:
              </span>
              {[
                { label: "OPM & PAT Drivers", q: QUICK_QUESTIONS[0] },
                { label: "CFO vs PAT & Working Cap", q: QUICK_QUESTIONS[1] },
                { label: "Capex & Concall Guidance", q: QUICK_QUESTIONS[2] },
              ].map((preset, i) => (
                <button
                  key={i}
                  type="button"
                  disabled={asking}
                  onClick={() => handleAskQuestion(preset.q)}
                  className="shrink-0 rounded-md border border-cyan-500/30 bg-zinc-950/85 hover:bg-cyan-500/15 hover:border-cyan-400/50 px-2 py-1 text-[10.5px] font-medium text-cyan-200 transition disabled:opacity-50"
                >
                  ⚡ {preset.label}
                </button>
              ))}
            </div>

            {qaError && (
              <div className="mt-2.5 rounded-lg border border-rose-500/35 bg-rose-950/40 px-2.5 py-2 text-xs text-rose-300">
                {qaError}
              </div>
            )}

            {qaHistory.filter((item) => item.ticker === pullData.ticker).length > 0 && (
              <div className="mt-3 space-y-2 border-t border-cyan-500/25 pt-2.5">
                {qaHistory
                  .filter((item) => item.ticker === pullData.ticker)
                  .map((item, idx) => (
                    <div
                      key={idx}
                      className="rounded-lg border border-cyan-500/30 bg-zinc-950/95 p-3 space-y-2 shadow-inner"
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="text-[11px] font-mono font-semibold text-cyan-300">
                          Q: {item.question}
                        </div>
                        <span className="shrink-0 rounded bg-emerald-500/15 border border-emerald-500/30 px-1.5 py-0.5 text-[9px] font-mono text-emerald-300">
                          Cited Filing Answer
                        </span>
                      </div>
                      <div className="text-xs text-zinc-100 leading-relaxed">
                        <FormattedMarkdown content={item.answer} />
                      </div>
                      {((item.citation_links && item.citation_links.length > 0) ||
                        (item.citations && item.citations.length > 0)) && (
                        <div className="flex flex-wrap gap-1.5 pt-1.5 border-t border-zinc-800/80">
                          {(item.citation_links && item.citation_links.length > 0
                            ? item.citation_links
                            : item.citations
                          ).map((cit, ci) => (
                            <SourceBadge
                              key={ci}
                              link={resolveCitationLink(cit, pullData)}
                              onJumpToSection={handleJumpToSection}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
              </div>
            )}
          </div>

          {/* MODULE 1 (HERO WORKED EXAMPLE): NARRATIVE VS. NUMBERS (4-QUARTER AUDIT) */}
          <div
            id="pull-module-narrative"
            className={`rounded-xl border-2 bg-gradient-to-b from-emerald-950/25 via-zinc-900/95 to-zinc-900/95 p-3.5 shadow-lg transition ${
              highlightedSection?.id === "pull-module-narrative"
                ? "border-emerald-400 ring-2 ring-emerald-400/60"
                : "border-emerald-500/35"
            }`}
          >
            {highlightedSection?.id === "pull-module-narrative" && (
              <div className="mb-2 flex items-center justify-between rounded bg-emerald-500/15 border border-emerald-500/35 px-2 py-1 text-[10px] font-mono text-emerald-300">
                <span>Viewing Cited Section: {highlightedSection.label}</span>
                <button
                  type="button"
                  onClick={() => setHighlightedSection(null)}
                  className="text-zinc-400 hover:text-white"
                >
                  ✕
                </button>
              </div>
            )}
            <div className="flex items-start justify-between gap-2 pb-2.5 border-b border-zinc-800">
              <div className="flex items-center gap-2">
                <div className="p-1.5 rounded-lg bg-emerald-500/20 text-emerald-400">
                  <Scale className="w-4 h-4" />
                </div>
                <div>
                  <div className="flex items-center gap-1.5">
                    <h2 className="text-xs font-extrabold uppercase tracking-wider text-white">
                      Narrative vs. Numbers (4Q Audit)
                    </h2>
                  </div>
                  <p className="text-[10px] text-zinc-400">
                    Concall/AR Promises vs. Audited Exchange Quarterly Delivery
                  </p>
                </div>
              </div>
              <VerdictBadge
                verdict={report.narrative_vs_numbers?.credibility_badge || "Aligned"}
              />
            </div>

            {report.narrative_vs_numbers?.synthesis && (
              <p className="mt-2.5 text-xs leading-relaxed text-zinc-200 bg-zinc-950/70 rounded-lg p-2.5 border border-zinc-800/80">
                {renderInlineMarkdown(report.narrative_vs_numbers.synthesis)}
              </p>
            )}

            <div className="mt-3 space-y-2.5">
              {(report.narrative_vs_numbers?.quarters_audit || []).map((q, idx) => (
                <div
                  key={`${q.quarter}-${idx}`}
                  className="rounded-lg border border-zinc-800 bg-zinc-950/90 p-2.5 space-y-2"
                >
                  <div className="flex items-center justify-between border-b border-zinc-800/80 pb-1.5">
                    <span className="text-xs font-mono font-bold text-white">
                      {q.quarter}
                    </span>
                    <VerdictBadge verdict={q.verdict} />
                  </div>

                  <div className="grid grid-cols-1 gap-2 text-xs">
                    <div className="rounded bg-zinc-900/80 p-2 border-l-2 border-purple-400">
                      <div className="text-[10px] font-mono uppercase text-purple-300 font-semibold mb-0.5">
                        Management Narrative / Guidance
                      </div>
                      <p className="text-zinc-300 leading-snug">
                        {renderInlineMarkdown(q.management_narrative)}
                      </p>
                    </div>

                    <div className="rounded bg-zinc-900/80 p-2 border-l-2 border-emerald-400">
                      <div className="text-[10px] font-mono uppercase text-emerald-300 font-semibold mb-0.5">
                        Actual Reported Numbers
                      </div>
                      <p className="text-zinc-200 font-medium leading-snug">
                        {renderInlineMarkdown(q.actual_numbers)}
                      </p>
                    </div>
                  </div>

                  {q.audit_note && (
                    <div className="text-xs text-amber-300/95 pt-1 border-t border-zinc-800/60">
                      <span className="font-semibold">Audit Verdict: </span>
                      {renderInlineMarkdown(q.audit_note)}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          {/* MODULE 2 & 3: FINANCIAL SNAPSHOT & BUSINESS SNAPSHOT */}
          <div
            id="pull-module-snapshot"
            className={`rounded-xl border bg-zinc-900/85 p-3.5 space-y-3 transition ${
              highlightedSection?.id === "pull-module-snapshot"
                ? "border-emerald-400 ring-2 ring-emerald-400/60"
                : "border-zinc-800"
            }`}
          >
            {highlightedSection?.id === "pull-module-snapshot" && (
              <div className="mb-1 flex items-center justify-between rounded bg-emerald-500/15 border border-emerald-500/35 px-2 py-1 text-[10px] font-mono text-emerald-300">
                <span>Viewing Cited Section: {highlightedSection.label}</span>
                <button
                  type="button"
                  onClick={() => setHighlightedSection(null)}
                  className="text-zinc-400 hover:text-white"
                >
                  ✕
                </button>
              </div>
            )}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <BookOpen className="w-4 h-4 text-cyan-400" />
                <h2 className="text-xs font-bold uppercase tracking-wider text-white">
                  Financial &amp; Business Snapshot
                </h2>
              </div>
              <span className="text-[10px] font-mono text-zinc-400">
                {report.financial_snapshot?.latest_quarter_label}
              </span>
            </div>

            {/* 8-KPI Dense Institutional Grid */}
            {report.financial_snapshot?.kpis && (
              <div className="grid grid-cols-4 gap-1.5">
                {[
                  ["Mkt Cap", report.financial_snapshot.kpis.market_cap],
                  ["Stock P/E", report.financial_snapshot.kpis.pe_ratio],
                  ["ROCE %", report.financial_snapshot.kpis.roce_pct],
                  ["ROE %", report.financial_snapshot.kpis.roe_pct],
                  ["Revenue", report.financial_snapshot.kpis.ttm_or_latest_revenue],
                  ["Net Profit", report.financial_snapshot.kpis.ttm_or_latest_pat],
                  ["OPM %", report.financial_snapshot.kpis.latest_opm_pct],
                  ["CMP", report.financial_snapshot.kpis.current_price],
                ].map(([label, val]) => (
                  <div
                    key={label}
                    className="rounded-lg border border-zinc-800/90 bg-zinc-950/80 p-2"
                  >
                    <div className="text-[9px] font-mono uppercase text-zinc-500 truncate">
                      {label}
                    </div>
                    <div className="mt-0.5 text-[11px] font-mono font-bold text-zinc-100 truncate">
                      {val || "—"}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {report.financial_snapshot?.headline_summary && (
              <p className="text-xs text-emerald-300/90 bg-emerald-500/10 border border-emerald-500/20 rounded-lg p-2">
                {renderInlineMarkdown(report.financial_snapshot.headline_summary)}
              </p>
            )}

            {report.business_snapshot?.overview && (
              <p className="text-xs text-zinc-300 leading-relaxed">
                {renderInlineMarkdown(report.business_snapshot.overview)}
              </p>
            )}

            {/* Segments Breakdown */}
            {(report.business_snapshot?.segments || []).length > 0 && (
              <div className="space-y-1.5 pt-1">
                <div className="text-[10px] font-mono uppercase text-zinc-400 font-semibold">
                  Segments &amp; Revenue Drivers
                </div>
                {report.business_snapshot.segments.map((seg, i) => (
                  <div
                    key={i}
                    className="rounded-lg border border-zinc-800/80 bg-zinc-950/70 p-2 text-xs"
                  >
                    <div className="flex items-start justify-between gap-3 font-semibold text-zinc-200">
                      <span>{seg.name}</span>
                      <span className="font-mono text-cyan-400 text-xs text-right">
                        {seg.contribution}
                      </span>
                    </div>
                    <p className="text-zinc-400 mt-0.5">{renderInlineMarkdown(seg.driver)}</p>
                  </div>
                ))}
              </div>
            )}

            {/* Key Operating Levers */}
            {(report.business_snapshot?.key_operating_levers || []).length > 0 && (
              <div className="flex flex-wrap gap-1.5 pt-1">
                {report.business_snapshot.key_operating_levers.map((lev, i) => (
                  <span
                    key={i}
                    className="rounded-md bg-zinc-800/90 border border-zinc-700/70 px-2 py-0.5 text-xs text-zinc-300"
                  >
                    ⚡ {renderInlineMarkdown(lev)}
                  </span>
                ))}
              </div>
            )}
          </div>

          {/* MODULE 4: MULTI-YEAR & QUARTERLY TRAJECTORY */}
          <div
            id="pull-module-trajectory"
            className={`rounded-xl border bg-zinc-900/85 p-3.5 space-y-2.5 transition ${
              highlightedSection?.id === "pull-module-trajectory"
                ? "border-emerald-400 ring-2 ring-emerald-400/60"
                : "border-zinc-800"
            }`}
          >
            {highlightedSection?.id === "pull-module-trajectory" && (
              <div className="mb-1 flex items-center justify-between rounded bg-emerald-500/15 border border-emerald-500/35 px-2 py-1 text-[10px] font-mono text-emerald-300">
                <span>Viewing Cited Section: {highlightedSection.label}</span>
                <button
                  type="button"
                  onClick={() => setHighlightedSection(null)}
                  className="text-zinc-400 hover:text-white"
                >
                  ✕
                </button>
              </div>
            )}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <BarChart3 className="w-4 h-4 text-emerald-400" />
                <h2 className="text-xs font-bold uppercase tracking-wider text-white">
                  Financial Trajectory
                </h2>
              </div>
              <div className="inline-flex rounded-md border border-zinc-800 bg-zinc-950 p-0.5">
                <button
                  type="button"
                  onClick={() => setTrajMode("quarterly")}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono font-semibold ${
                    trajMode === "quarterly"
                      ? "bg-emerald-500 text-zinc-950"
                      : "text-zinc-400"
                  }`}
                >
                  Quarterly (6Q)
                </button>
                <button
                  type="button"
                  onClick={() => setTrajMode("annual")}
                  className={`px-2 py-0.5 rounded text-[10px] font-mono font-semibold ${
                    trajMode === "annual"
                      ? "bg-emerald-500 text-zinc-950"
                      : "text-zinc-400"
                  }`}
                >
                  Annual (5Y)
                </button>
              </div>
            </div>

            <p className="text-xs text-zinc-300 leading-relaxed">
              {renderInlineMarkdown(report.trajectory?.summary)}
            </p>

            <TrajectoryChart
              series={
                trajMode === "quarterly"
                  ? report.trajectory?.quarterly_series || []
                  : report.trajectory?.annual_series || []
              }
            />

            <div className="grid grid-cols-1 gap-2 pt-1">
              <div className="rounded-lg border border-emerald-500/25 bg-emerald-950/15 p-2.5">
                <div className="text-[10px] font-mono uppercase text-emerald-400 font-bold mb-1 flex items-center gap-1">
                  <TrendingUp className="w-3 h-3" />
                  <span>What&apos;s Expanding / Accelerating</span>
                </div>
                <ul className="space-y-1 text-xs text-zinc-300 list-disc list-inside">
                  {(report.trajectory?.whats_growing || []).map((g, i) => (
                    <li key={i}>{renderInlineMarkdown(g)}</li>
                  ))}
                </ul>
              </div>

              <div className="rounded-lg border border-rose-500/25 bg-rose-950/15 p-2.5">
                <div className="text-[10px] font-mono uppercase text-rose-400 font-bold mb-1 flex items-center gap-1">
                  <TrendingDown className="w-3 h-3" />
                  <span>What&apos;s Compressing / Watchlist</span>
                </div>
                <ul className="space-y-1 text-xs text-zinc-300 list-disc list-inside">
                  {(report.trajectory?.whats_compressing || []).map((c, i) => (
                    <li key={i}>{renderInlineMarkdown(c)}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>

          {/* MODULE 5 & 6: CASH QUALITY & BALANCE SHEET */}
          <div
            id="pull-module-cash-bs"
            className={`rounded-xl border bg-zinc-900/85 p-3.5 space-y-3 transition ${
              highlightedSection?.id === "pull-module-cash-bs"
                ? "border-emerald-400 ring-2 ring-emerald-400/60"
                : "border-zinc-800"
            }`}
          >
            {highlightedSection?.id === "pull-module-cash-bs" && (
              <div className="mb-1 flex items-center justify-between rounded bg-emerald-500/15 border border-emerald-500/35 px-2 py-1 text-[10px] font-mono text-emerald-300">
                <span>Viewing Cited Section: {highlightedSection.label}</span>
                <button
                  type="button"
                  onClick={() => setHighlightedSection(null)}
                  className="text-zinc-400 hover:text-white"
                >
                  ✕
                </button>
              </div>
            )}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Coins className="w-4 h-4 text-emerald-400" />
                <h2 className="text-xs font-bold uppercase tracking-wider text-white">
                  Cash Flow Quality &amp; Balance Sheet
                </h2>
              </div>
              <span className="rounded-md bg-emerald-500/15 border border-emerald-500/30 px-2 py-0.5 text-[10px] font-mono font-semibold text-emerald-300">
                {report.cash_quality?.verdict_badge}
              </span>
            </div>

            <div className="grid grid-cols-3 gap-1.5">
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/80 p-2">
                <div className="text-[9px] font-mono uppercase text-zinc-500">
                  CFO / PAT Conv.
                </div>
                <div className="mt-0.5 text-[11px] font-mono font-bold text-emerald-400">
                  {report.cash_quality?.cfo_to_pat_conversion_pct || "—"}
                </div>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/80 p-2">
                <div className="text-[9px] font-mono uppercase text-zinc-500">
                  Latest CFO
                </div>
                <div className="mt-0.5 text-[11px] font-mono font-bold text-zinc-100">
                  {report.cash_quality?.latest_annual_cfo || "—"}
                </div>
              </div>
              <div className="rounded-lg border border-zinc-800 bg-zinc-950/80 p-2">
                <div className="text-[9px] font-mono uppercase text-zinc-500">
                  Latest PAT
                </div>
                <div className="mt-0.5 text-[11px] font-mono font-bold text-zinc-100">
                  {report.cash_quality?.latest_annual_pat || "—"}
                </div>
              </div>
            </div>

            <p className="text-xs text-zinc-300 leading-relaxed">
              {renderInlineMarkdown(report.cash_quality?.summary)}
            </p>

            <CashVsPatChart series={report.cash_quality?.annual_cash_vs_pat || []} />

            {/* Balance Sheet Strip */}
            <div className="pt-2 border-t border-zinc-800 space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-bold text-zinc-200 flex items-center gap-1.5">
                  <Shield className="w-3.5 h-3.5 text-cyan-400" />
                  Balance Sheet Posture
                </span>
                <span className="rounded bg-cyan-500/15 border border-cyan-500/30 px-2 py-0.5 text-[10px] font-mono font-semibold text-cyan-300">
                  {report.balance_sheet?.status_badge}
                </span>
              </div>

              {report.balance_sheet?.metrics && (
                <div className="grid grid-cols-3 gap-1.5">
                  {[
                    ["Borrowings", report.balance_sheet.metrics.latest_borrowings],
                    ["Reserves", report.balance_sheet.metrics.latest_reserves],
                    ["Debt / Equity", report.balance_sheet.metrics.debt_to_equity],
                    ["Work Cap Days", report.balance_sheet.metrics.working_capital_days],
                    [
                      "Cash Conv Cycle",
                      report.balance_sheet.metrics.cash_conversion_cycle_days,
                    ],
                  ].map(([k, v]) => (
                    <div
                      key={k}
                      className="rounded border border-zinc-800/80 bg-zinc-950/70 p-1.5"
                    >
                      <div className="text-[9px] font-mono text-zinc-500">{k}</div>
                      <div className="text-[11px] font-mono font-semibold text-zinc-200 truncate">
                        {v || "—"}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              <p className="text-xs text-zinc-400 leading-relaxed">
                {renderInlineMarkdown(report.balance_sheet?.summary)}
              </p>
            </div>
          </div>

          {/* MODULE 7: FORWARD GUIDANCE */}
          <div
            id="pull-module-guidance"
            className={`rounded-xl border bg-zinc-900/85 p-3.5 space-y-2.5 transition ${
              highlightedSection?.id === "pull-module-guidance"
                ? "border-emerald-400 ring-2 ring-emerald-400/60"
                : "border-zinc-800"
            }`}
          >
            {highlightedSection?.id === "pull-module-guidance" && (
              <div className="mb-1 flex items-center justify-between rounded bg-emerald-500/15 border border-emerald-500/35 px-2 py-1 text-[10px] font-mono text-emerald-300">
                <span>Viewing Cited Section: {highlightedSection.label}</span>
                <button
                  type="button"
                  onClick={() => setHighlightedSection(null)}
                  className="text-zinc-400 hover:text-white"
                >
                  ✕
                </button>
              </div>
            )}
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Target className="w-4 h-4 text-amber-400" />
                <h2 className="text-xs font-bold uppercase tracking-wider text-white">
                  Forward Guidance &amp; Stated Targets
                </h2>
              </div>
              <span className="rounded bg-amber-500/15 border border-amber-500/30 px-2 py-0.5 text-[10px] font-mono font-semibold text-amber-300">
                Tone: {report.guidance?.management_tone}
              </span>
            </div>

            <p className="text-xs text-zinc-300 leading-relaxed">
              {renderInlineMarkdown(report.guidance?.summary)}
            </p>

            <div className="space-y-1.5">
              {(report.guidance?.stated_targets || []).map((t, i) => (
                <div
                  key={i}
                  className="rounded-lg border border-zinc-800/90 bg-zinc-950/80 p-2.5 text-xs"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-semibold text-amber-300">{t.metric}</span>
                    {t.source && (
                      <SourceBadge
                        link={resolveCitationLink(t.source, pullData)}
                        onJumpToSection={handleJumpToSection}
                      />
                    )}
                  </div>
                  <p className="text-zinc-200 mt-1 leading-snug">
                    {renderInlineMarkdown(t.target)}
                  </p>
                </div>
              ))}
            </div>
          </div>

          {/* MODULE 8: BULL VS. BEAR INSTITUTIONAL DEBATE */}
          <div
            id="pull-module-bull-bear"
            className={`rounded-xl border bg-zinc-900/85 p-3.5 space-y-2.5 transition ${
              highlightedSection?.id === "pull-module-bull-bear"
                ? "border-emerald-400 ring-2 ring-emerald-400/60"
                : "border-zinc-800"
            }`}
          >
            {highlightedSection?.id === "pull-module-bull-bear" && (
              <div className="mb-1 flex items-center justify-between rounded bg-emerald-500/15 border border-emerald-500/35 px-2 py-1 text-[10px] font-mono text-emerald-300">
                <span>Viewing Cited Section: {highlightedSection.label}</span>
                <button
                  type="button"
                  onClick={() => setHighlightedSection(null)}
                  className="text-zinc-400 hover:text-white"
                >
                  ✕
                </button>
              </div>
            )}
            <h2 className="text-xs font-bold uppercase tracking-wider text-white">
              Bull vs. Bear Primary-Source Case
            </h2>

            <div className="grid grid-cols-1 gap-2.5">
              <div className="rounded-lg border border-emerald-500/30 bg-emerald-950/20 p-2.5">
                <div className="text-[11px] font-mono font-bold uppercase text-emerald-400 mb-1.5">
                  ▲ Bull Case (Data-Backed)
                </div>
                <ul className="space-y-1.5 text-xs text-zinc-200">
                  {(report.bull_vs_bear?.bull_case || []).map((pt, i) => (
                    <li key={i} className="flex items-start gap-1.5">
                      <span className="text-emerald-400 font-bold">•</span>
                      <span>{renderInlineMarkdown(pt)}</span>
                    </li>
                  ))}
                </ul>
              </div>

              <div className="rounded-lg border border-rose-500/30 bg-rose-950/20 p-2.5">
                <div className="text-[11px] font-mono font-bold uppercase text-rose-400 mb-1.5">
                  ▼ Bear Case / Key Risks
                </div>
                <ul className="space-y-1.5 text-xs text-zinc-200">
                  {(report.bull_vs_bear?.bear_case || []).map((pt, i) => (
                    <li key={i} className="flex items-start gap-1.5">
                      <span className="text-rose-400 font-bold">•</span>
                      <span>{renderInlineMarkdown(pt)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

