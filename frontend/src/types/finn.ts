export type TimeWindow = "24h" | "48h" | "7d" | "14d" | "30d";

export interface MetricDelta {
  value: string;
  qoq_pct: string;
  yoy_pct: string;
}

export interface ResultsEnrichment {
  has_numbers?: boolean;
  period?: string;
  unit?: string;
  source?: string;
  metrics?: {
    revenue?: MetricDelta;
    ebitda?: MetricDelta;
    pbt?: MetricDelta;
    pat?: MetricDelta;
  };
  takeaway?: string;
}

export interface ManagementEnrichment {
  person_name?: string;
  role?: string;
  action?: string;
  effective_date?: string;
  two_line_bio?: string;
  source_url?: string;
}

export interface FilingItem {
  filing_id: string;
  ticker: string;
  company_name?: string;
  title: string;
  pdf_url?: string;
  published_at: string;
  exchange: string;
  category: string;
  materiality: number;
  summary?: string;
  priority_rank: number;
  enriched?: ResultsEnrichment & ManagementEnrichment & Record<string, unknown>;
  trigger_reason?: string;
}

export interface DigestResponse {
  window: TimeWindow;
  cutoff_timestamp: string;
  stats: {
    total_filings: number;
    material_count: number;
    routine_collapsed_count: number;
    noise_filtered_pct: number;
    /** Model that served the latest LLM triage batch; null if only the regex classifier has run. */
    triage_model?: string | null;
  };
  material: FilingItem[];
  routine: FilingItem[];
}

export interface AlertRules {
  id?: number;
  price_move_pct: number;
  volume_multiplier: number;
  watched_categories: string[];
  updated_at?: string;
}

export interface PortfolioItem {
  ticker: string;
  bse_code: string;
  company_name: string;
  sector: string;
  cap_tier: string;
  trade_date?: string | null;
  close_price?: number | null;
  prev_close?: number | null;
  pct_change?: number | null;
  volume?: number | null;
  avg_volume_20d?: number | null;
  volume_ratio?: number | null;
  price_alert?: boolean;
  volume_alert?: boolean;
  material_filings_count?: number;
  has_cached_pull?: boolean;
  trigger_reasons?: string[];
}

export interface AlertsResponse {
  rules: AlertRules;
  window: string;
  total_alerts: number;
  market_alerts: PortfolioItem[];
  filing_alerts: FilingItem[];
}

export interface QuarterAuditItem {
  quarter: string;
  management_narrative: string;
  actual_numbers: string;
  verdict: "Beat" | "Delivered" | "Mixed" | "Missed" | string;
  audit_note: string;
}

export interface TrajectoryPoint {
  period: string;
  revenue: number;
  pat: number;
  opm_pct: number;
}

export interface CashVsPatPoint {
  period: string;
  cfo: number;
  pat: number;
}

export interface PullReport {
  business_snapshot: {
    overview: string;
    segments: { name: string; contribution: string; driver: string }[];
    key_operating_levers: string[];
  };
  financial_snapshot: {
    latest_quarter_label: string;
    kpis: {
      market_cap: string;
      current_price: string;
      pe_ratio: string;
      roce_pct: string;
      roe_pct: string;
      ttm_or_latest_revenue: string;
      ttm_or_latest_pat: string;
      latest_opm_pct: string;
    };
    headline_summary: string;
  };
  trajectory: {
    summary: string;
    whats_growing: string[];
    whats_compressing: string[];
    annual_series: TrajectoryPoint[];
    quarterly_series: TrajectoryPoint[];
  };
  balance_sheet: {
    status_badge: string;
    summary: string;
    metrics: {
      latest_borrowings: string;
      latest_reserves: string;
      debt_to_equity: string;
      working_capital_days: string;
      cash_conversion_cycle_days: string;
    };
  };
  cash_quality: {
    verdict_badge: string;
    summary: string;
    cfo_to_pat_conversion_pct: string;
    latest_annual_cfo: string;
    latest_annual_pat: string;
    annual_cash_vs_pat: CashVsPatPoint[];
  };
  narrative_vs_numbers: {
    credibility_badge: string;
    synthesis: string;
    quarters_audit: QuarterAuditItem[];
  };
  guidance: {
    management_tone: string;
    summary: string;
    stated_targets: {
      metric: string;
      target: string;
      source: string;
    }[];
  };
  bull_vs_bear: {
    bull_case: string[];
    bear_case: string[];
  };
  /** Models that actually served the Map and Reduce steps (map is null when no PDFs were mapped). */
  models_used?: { map: string | null; reduce: string };
  sources_used?: {
    screener_url?: string;
    documents_parsed?: { period: string; doc_type: string; url: string }[];
    concalls_available?: { period: string; transcript_url?: string; ppt_url?: string; audio_url?: string }[];
    annual_reports_available?: { title: string; url: string }[];
    material_filings_count?: number;
    material_filings?: { date: string; category: string; title: string; pdf_url: string }[];
  };
}

export interface CopilotCitationLink {
  label: string;
  kind: "webpage" | "document" | "internal_report" | "unavailable";
  url?: string | null;
  section_id?: string | null;
  available: boolean;
}

export interface PullResponse {
  ticker: string;
  quarter_key: string;
  cached: boolean;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  created_at?: string;
  eod?: PortfolioItem | null;
  fundamentals: {
    company_name?: string;
    screener_url?: string;
    about?: string;
    top_ratios?: Record<string, string>;
    concalls?: { period: string; transcript_url?: string; ppt_url?: string; audio_url?: string }[];
    annual_reports?: { title: string; url: string }[];
  };
  report: PullReport;
}

export interface CachedReportMeta {
  ticker: string;
  quarter_key: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  created_at: string;
  company_name?: string;
  sector?: string;
}

export interface CopilotQAResponse {
  ticker: string;
  question: string;
  answer: string;
  citations: string[];
  citation_links?: CopilotCitationLink[];
  model?: string;
}

export interface CostWorkflowRow {
  workflow: string;
  model: string;
  calls: number;
  total_tokens_in: number;
  total_tokens_out: number;
  total_cost_usd: number;
}

export interface CostSummaryResponse {
  totals: {
    total_calls: number;
    tokens_in: number;
    tokens_out: number;
    cost_usd: number;
  };
  by_workflow: CostWorkflowRow[];
  routing?: RoutingTier[];
}

export interface RoutingTier {
  tier: 1 | 2 | 3;
  model: string;
  purpose: string;
  fallbacks: string[];
  /** [input, output] USD per 1M tokens; output includes thinking tokens. */
  price_per_1m: [number, number];
}

