import {
  AlertRules,
  AlertsResponse,
  CachedReportMeta,
  CopilotQAResponse,
  CostSummaryResponse,
  DigestResponse,
  PortfolioItem,
  PullResponse,
  TimeWindow,
} from "@/types/finn";

// ponytail: use Next.js /api rewrite proxy by default so browser calls same-origin, with direct localhost:8000 fallback if needed.
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const err = await res.json();
      if (err.detail) detail = err.detail;
    } catch {
      // ignore parse error
    }
    throw new Error(detail);
  }
  return res.json();
}

export const finnApi = {
  getHealth: () => request<{ status: string; service: string }>("/api/health"),

  getPortfolio: () =>
    request<{ portfolio: PortfolioItem[]; count: number; alert_rules: AlertRules }>(
      "/api/portfolio"
    ),

  addPortfolioStock: (ticker: string, sector = "Equities") =>
    request<{ status: string; ticker: string }>("/api/portfolio", {
      method: "POST",
      body: JSON.stringify({ ticker, sector }),
    }),

  removePortfolioStock: (ticker: string) =>
    request<{ status: string; ticker: string }>(`/api/portfolio/${encodeURIComponent(ticker)}`, {
      method: "DELETE",
    }),

  getDigest: (window: TimeWindow = "24h", ticker?: string | null) => {
    const params = new URLSearchParams({ window });
    if (ticker) params.set("ticker", ticker);
    return request<DigestResponse>(`/api/push/digest?${params.toString()}`);
  },

  refreshPush: (tickers?: string[], days = 14, max_enrich = 4) =>
    request<{
      stocks_refreshed: number;
      eod_updated: number;
      filings_checked: number;
      filings_triaged_by_llm: number;
      filings_deep_enriched: number;
    }>("/api/push/refresh", {
      method: "POST",
      body: JSON.stringify({ tickers: tickers || null, days, max_enrich }),
    }),

  enrichFiling: (filingId: string) =>
    request<{ filing_id: string; enriched: Record<string, unknown> }>(
      `/api/push/enrich/${encodeURIComponent(filingId)}`,
      { method: "POST" }
    ),

  getAlerts: (window: TimeWindow = "14d", ticker?: string | null) => {
    const params = new URLSearchParams({ window });
    if (ticker) params.set("ticker", ticker);
    return request<AlertsResponse>(`/api/push/alerts?${params.toString()}`);
  },

  updateAlertRules: (rules: {
    price_move_pct: number;
    volume_multiplier: number;
    watched_categories: string[];
  }) =>
    request<AlertRules>("/api/push/alerts/rules", {
      method: "PUT",
      body: JSON.stringify(rules),
    }),

  getCachedPullReports: () =>
    request<{ cached_reports: CachedReportMeta[] }>("/api/pull-cached"),

  getPullReport: (ticker: string, force = false) =>
    request<PullResponse>(
      `/api/pull/${encodeURIComponent(ticker)}?force=${force ? "true" : "false"}`
    ),

  askPullCopilot: (ticker: string, question: string) =>
    request<CopilotQAResponse>(`/api/pull/${encodeURIComponent(ticker)}/ask`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),

  getCostSummary: () => request<CostSummaryResponse>("/api/cost"),
};

