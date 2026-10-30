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
  const fetchInit: RequestInit = {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
    cache: "no-store",
  };

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, fetchInit);
  } catch (err) {
    if (err instanceof Error && err.name === "AbortError") {
      throw err;
    }
    throw new Error(
      "Cannot reach FINN backend server. Ensure FastAPI is running on port 8000."
    );
  }

  // ponytail: if Next.js dev rewrite proxy drops a long-running synthesis with a bare non-JSON 500/502/504 on localhost, transparently retry direct against FastAPI (127.0.0.1:8000).
  if (
    !res.ok &&
    res.status >= 500 &&
    !API_BASE &&
    typeof window !== "undefined" &&
    (window.location.hostname === "127.0.0.1" || window.location.hostname === "localhost")
  ) {
    const contentType = res.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      try {
        res = await fetch(`http://127.0.0.1:8000${path}`, fetchInit);
      } catch (retryErr) {
        if (retryErr instanceof Error && retryErr.name === "AbortError") {
          throw retryErr;
        }
      }
    }
  }

  if (!res.ok) {
    let detail = "";
    try {
      const err = await res.json();
      if (typeof err?.detail === "string") {
        detail = err.detail;
      } else if (Array.isArray(err?.detail)) {
        detail = err.detail.map((d: { msg?: string }) => d.msg || "").filter(Boolean).join("; ");
      }
    } catch {
      // non-JSON error body
    }
    if (!detail) {
      if (res.status === 502 || res.status === 503 || res.status === 504) {
        detail = `Research service is temporarily unavailable (HTTP ${res.status}). Verify backend connectivity and try again.`;
      } else if (res.status >= 500) {
        detail = `Backend synthesis failed or timed out (HTTP ${res.status}). Check backend logs and try again.`;
      } else {
        detail = `Request failed (HTTP ${res.status}).`;
      }
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

  getPullReport: (ticker: string, force = false, signal?: AbortSignal) =>
    request<PullResponse>(
      `/api/pull/${encodeURIComponent(ticker)}?force=${force ? "true" : "false"}`,
      signal ? { signal } : undefined
    ),

  askPullCopilot: (ticker: string, question: string, signal?: AbortSignal) =>
    request<CopilotQAResponse>(`/api/pull/${encodeURIComponent(ticker)}/ask`, {
      method: "POST",
      body: JSON.stringify({ question }),
      ...(signal ? { signal } : {}),
    }),

  getCostSummary: () => request<CostSummaryResponse>("/api/cost"),
};

