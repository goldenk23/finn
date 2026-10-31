"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import {
  Radio,
  Bell,
  FileSearch,
  Briefcase,
  Cpu,
  Terminal,
  X,
} from "lucide-react";
import {
  AlertsResponse,
  CachedReportMeta,
  CostSummaryResponse,
  DigestResponse,
  FilingItem,
  PortfolioItem,
  PullResponse,
  TimeWindow,
} from "@/types/finn";
import { finnApi } from "@/lib/api";
import { PushTab, formatIst, pushDataStatus } from "@/components/PushTab";
import { AlertsTab, sendBrowserNotification } from "@/components/AlertsTab";
import { PullTab } from "@/components/PullTab";
import { PortfolioTab } from "@/components/PortfolioTab";
import { CostDrawer } from "@/components/CostDrawer";

type ActiveTab = "push" | "alerts" | "pull" | "portfolio";
type AlertCard = PortfolioItem | FilingItem;

// Seen alert IDs, stored with the alerts window they were built from
const SEEN_KEY = "finn.seenAlerts";
const ALERTS_POLL_MS = 60_000;

function alertCards(data: AlertsResponse | null): AlertCard[] {
  return data ? [...(data.market_alerts || []), ...(data.filing_alerts || [])] : [];
}

function alertLabel(a: AlertCard): string {
  return "title" in a ? `${a.ticker}: ${a.title}` : `${a.ticker}: ${(a.trigger_reasons || []).join(" • ")}`;
}

export default function FinnAppPage() {
  const [activeTab, setActiveTab] = useState<ActiveTab>("push");
  const [backendOnline, setBackendOnline] = useState<boolean>(true);

  // Shared state
  const [portfolio, setPortfolio] = useState<PortfolioItem[]>([]);
  const [portfolioLoading, setPortfolioLoading] = useState<boolean>(true);

  // Push Digest state (defaults to 24h per challenge brief)
  const [digestWindow, setDigestWindow] = useState<TimeWindow>("24h");
  const [digestTicker, setDigestTicker] = useState<string | null>(null);
  const [digest, setDigest] = useState<DigestResponse | null>(null);
  const [digestLoading, setDigestLoading] = useState<boolean>(true);
  const [refreshingPush, setRefreshingPush] = useState<boolean>(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  // Alerts state: its own window (default 14d), so rule saves and refreshes never reload Alerts with the digest window
  const [alertsWindow, setAlertsWindow] = useState<TimeWindow>("14d");
  const [alertsData, setAlertsData] = useState<AlertsResponse | null>(null);
  const [alertsLoading, setAlertsLoading] = useState<boolean>(true);
  const [savingRules, setSavingRules] = useState<boolean>(false);
  const [rulesError, setRulesError] = useState<string | null>(null);
  const [seenIds, setSeenIds] = useState<Set<string> | null>(null);
  // Unseen when the Alerts tab was opened: keeps their NEW pills until the next open or "Mark all read"
  const [openedUnseen, setOpenedUnseen] = useState<Set<string>>(new Set());
  const [toast, setToast] = useState<{ text: string; more: number; at: number } | null>(null);
  const knownAlertsRef = useRef<{ window: string; ids: Set<string> } | null>(null);

  // Pull Research state
  const [activePullTicker, setActivePullTicker] = useState<string>("TRENT");
  const [pullData, setPullData] = useState<PullResponse | null>(null);
  const [pullLoading, setPullLoading] = useState<boolean>(false);
  const [pullError, setPullError] = useState<string | null>(null);
  const [cachedReports, setCachedReports] = useState<CachedReportMeta[]>([]);
  const pullReqIdRef = useRef<number>(0);
  const pullAbortRef = useRef<AbortController | null>(null);

  // Cost Telemetry Drawer state
  const [costData, setCostData] = useState<CostSummaryResponse | null>(null);
  const [costDrawerOpen, setCostDrawerOpen] = useState<boolean>(false);

  const loadTelemetryAndCache = useCallback(async () => {
    try {
      const [costRes, cachedRes] = await Promise.all([
        finnApi.getCostSummary(),
        finnApi.getCachedPullReports(),
      ]);
      setCostData(costRes);
      setCachedReports(cachedRes.cached_reports || []);
      setBackendOnline(true);
    } catch {
      setBackendOnline(false);
    }
  }, []);

  const loadPortfolio = useCallback(async () => {
    try {
      setPortfolioLoading(true);
      const res = await finnApi.getPortfolio();
      setPortfolio(res.portfolio || []);
      setBackendOnline(true);
    } catch {
      setBackendOnline(false);
    } finally {
      setPortfolioLoading(false);
    }
  }, []);

  const loadDigest = useCallback(
    async (win: TimeWindow, ticker: string | null) => {
      try {
        setDigestLoading(true);
        const res = await finnApi.getDigest(win, ticker);
        setDigest(res);
      } catch {
        // handled via online indicator
      } finally {
        setDigestLoading(false);
      }
    },
    []
  );

  // quiet: background poll, no loading skeleton
  const loadAlerts = useCallback(async (win: TimeWindow, quiet = false) => {
    try {
      if (!quiet) setAlertsLoading(true);
      const res = await finnApi.getAlerts(win);
      setAlertsData(res);
    } catch {
      // ignore
    } finally {
      if (!quiet) setAlertsLoading(false);
    }
  }, []);

  const saveSeen = useCallback((win: string, ids: Set<string>) => {
    setSeenIds(ids);
    try {
      localStorage.setItem(SEEN_KEY, JSON.stringify({ window: win, ids: Array.from(ids) }));
    } catch {
      // storage full or disabled: unseen tracking lasts for this page view only
    }
  }, []);

  const loadPullReport = useCallback(
    async (ticker: string, force = false) => {
      const clean = ticker.trim().toUpperCase().replace(".NS", "").replace(".BO", "");
      if (!clean) return;

      const reqId = ++pullReqIdRef.current;
      if (pullAbortRef.current) {
        pullAbortRef.current.abort();
      }
      const controller = new AbortController();
      pullAbortRef.current = controller;

      setActivePullTicker(clean);
      setPullLoading(true);
      setPullError(null);
      setPullData(null);

      try {
        const res = await finnApi.getPullReport(clean, force, controller.signal);
        if (reqId !== pullReqIdRef.current) return;
        if ((res.ticker || "").toUpperCase() !== clean) {
          throw new Error(`Mismatched report ticker returned (${res.ticker} vs ${clean})`);
        }
        setPullData(res);
        void loadTelemetryAndCache();
        void loadPortfolio();
      } catch (err) {
        if (reqId !== pullReqIdRef.current) return;
        if (err instanceof Error && err.name === "AbortError") return;
        setPullData(null);
        setPullError(
          err instanceof Error ? err.message : "Failed to load Pull research"
        );
      } finally {
        if (reqId === pullReqIdRef.current) {
          setPullLoading(false);
        }
      }
    },
    [loadTelemetryAndCache, loadPortfolio]
  );

  // Initial boot
  useEffect(() => {
    loadPortfolio();
    loadTelemetryAndCache();
    loadPullReport("TRENT", false);
  }, [loadPortfolio, loadTelemetryAndCache, loadPullReport]);

  useEffect(() => {
    loadAlerts(alertsWindow);
  }, [alertsWindow, loadAlerts]);

  // /api/push/alerts only reads SQLite (no scraping, no LLM), so polling costs nothing. Paused while the tab is hidden.
  useEffect(() => {
    const poll = () => {
      if (document.visibilityState === "visible") void loadAlerts(alertsWindow, true);
    };
    const timer = setInterval(poll, ALERTS_POLL_MS);
    document.addEventListener("visibilitychange", poll); // catch up as soon as the tab is visible again
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", poll);
    };
  }, [alertsWindow, loadAlerts]);

  // Unseen tracking: update the seen set from each alerts response and announce alerts that just appeared
  useEffect(() => {
    if (!alertsData) return;
    const cards = alertCards(alertsData);
    const current = new Set(cards.flatMap((c) => c.alert_ids || []));
    let stored: { window: string; ids: string[] } | null = null;
    try {
      stored = JSON.parse(localStorage.getItem(SEEN_KEY) || "null");
    } catch {
      stored = null;
    }
    // First visit, or a set built from another window: start with everything seen so old alerts don't all show NEW.
    // Otherwise drop stored IDs that left the response so the set stays small.
    const seen =
      !stored || stored.window !== alertsData.window || !Array.isArray(stored.ids)
        ? current
        : new Set(stored.ids.filter((id) => current.has(id)));

    const prev = knownAlertsRef.current;
    knownAlertsRef.current = { window: alertsData.window, ids: current };
    if (prev && prev.window === alertsData.window) {
      const fresh = cards.filter((c) => (c.alert_ids || []).some((id) => !prev.ids.has(id) && !seen.has(id)));
      if (fresh.length > 0) {
        const text = alertLabel(fresh[0]);
        setToast({ text, more: fresh.length - 1, at: Date.now() });
        sendBrowserNotification(fresh.length > 1 ? `${text} (+${fresh.length - 1} more)` : text);
      }
    }
    saveSeen(alertsData.window, seen);
  }, [alertsData, saveSeen]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 8000);
    return () => clearTimeout(t);
  }, [toast]);

  useEffect(() => {
    loadDigest(digestWindow, digestTicker);
  }, [digestWindow, digestTicker, loadDigest]);

  async function handleRefreshLivePush() {
    try {
      setRefreshingPush(true);
      setRefreshError(null);
      const res = await finnApi.refreshPush(
        digestTicker ? [digestTicker] : undefined,
        14,
        3,
        digestWindow
      );
      const failed = res.enrichment_errors || 0;
      if (failed > 0) {
        setRefreshError(
          `Refreshed, but ${failed} ${failed === 1 ? "card" : "cards"} couldn't be enriched. Try Deep Enrich on ${failed === 1 ? "it" : "them"} later.`
        );
      }
      await Promise.all([
        loadDigest(digestWindow, digestTicker),
        loadAlerts(alertsWindow),
        loadPortfolio(),
        loadTelemetryAndCache(),
      ]);
    } catch (err) {
      setRefreshError(err instanceof Error ? err.message : "Live refresh failed.");
    } finally {
      setRefreshingPush(false);
    }
  }

  async function handleUpdateAlertRules(rules: {
    price_move_pct: number;
    volume_multiplier: number;
    watched_categories: string[];
  }) {
    try {
      setSavingRules(true);
      setRulesError(null);
      await finnApi.updateAlertRules(rules);
      await Promise.all([loadAlerts(alertsWindow), loadPortfolio()]);
    } catch (err) {
      setRulesError(err instanceof Error ? err.message : "Couldn't save alert thresholds.");
    } finally {
      setSavingRules(false);
    }
  }

  function handleOpenPullFromAnywhere(ticker: string) {
    const clean = ticker.trim().toUpperCase().replace(".NS", "").replace(".BO", "");
    setActiveTab("pull");
    if (
      activePullTicker.toUpperCase() !== clean ||
      !pullData ||
      pullData.ticker.toUpperCase() !== clean ||
      Boolean(pullError)
    ) {
      loadPullReport(clean, false);
    }
  }

  function handleFilterDigestFromPortfolio(ticker: string) {
    setDigestTicker(ticker);
    setActiveTab("push");
  }

  async function handleAddPortfolioStock(ticker: string, sector: string) {
    await finnApi.addPortfolioStock(ticker, sector);
    await Promise.all([
      loadPortfolio(),
      loadDigest(digestWindow, digestTicker),
      loadAlerts(alertsWindow),
    ]);
  }

  async function handleRemovePortfolioStock(ticker: string) {
    await finnApi.removePortfolioStock(ticker);
    await Promise.all([
      loadPortfolio(),
      loadDigest(digestWindow, digestTicker),
      loadAlerts(alertsWindow),
    ]);
  }

  function handleFilingEnriched() {
    loadDigest(digestWindow, digestTicker);
    loadTelemetryAndCache();
  }

  const totalCostUsd = costData?.totals?.cost_usd || 0;
  const materialCount = digest?.stats?.material_count || 0;
  const totalAlerts = alertsData?.total_alerts || 0;
  const dataStatus = pushDataStatus(digest);

  const cards = alertCards(alertsData);
  const allAlertIds = cards.flatMap((c) => c.alert_ids || []);
  const unseenIds = seenIds ? allAlertIds.filter((id) => !seenIds.has(id)) : [];
  const unseenCount = seenIds ? cards.filter((c) => (c.alert_ids || []).some((id) => !seenIds.has(id))).length : 0;
  const newAlertIds = new Set([...unseenIds, ...Array.from(openedUnseen)]);

  function markAllAlertsSeen() {
    if (alertsData) saveSeen(alertsData.window, new Set(allAlertIds));
  }

  function openTab(tab: ActiveTab) {
    if (tab === "alerts" && activeTab !== "alerts") {
      // Opening the tab marks everything seen and clears the badge; the NEW pills stay while you look
      setOpenedUnseen(new Set(unseenIds));
      markAllAlertsSeen();
    }
    setActiveTab(tab);
  }

  function handleMarkAllRead() {
    setOpenedUnseen(new Set());
    markAllAlertsSeen();
  }

  return (
    <div className="min-h-screen w-full bg-[#050507] flex justify-center">
      {/* Mobile-First Institutional Shell (430px max width on desktop, 100% on phone) */}
      <div className="w-full max-w-[430px] min-h-screen flex flex-col border-x border-zinc-800/80 bg-zinc-950 shadow-2xl relative">
        {/* New-alert banner: top of screen, auto-hides after 8s */}
        {toast && (
          <div
            role="status"
            aria-live="polite"
            className="fixed top-2 left-1/2 -translate-x-1/2 z-50 w-[calc(100%-1rem)] max-w-[414px] flex items-start gap-2 rounded-xl border border-rose-500/50 bg-zinc-900/95 backdrop-blur-md p-2.5 shadow-2xl"
          >
            <button
              type="button"
              onClick={() => {
                setToast(null);
                openTab("alerts");
              }}
              className="flex min-w-0 flex-1 items-start gap-2 text-left"
            >
              <Bell className="mt-0.5 w-4 h-4 shrink-0 text-rose-400" />
              <span className="min-w-0">
                <span className="block text-[11px] font-black uppercase tracking-wide text-rose-300">
                  New alert{toast.more > 0 ? ` (+${toast.more} more)` : ""}
                </span>
                <span className="block text-xs text-zinc-200 line-clamp-2">{toast.text}</span>
              </span>
            </button>
            <button
              type="button"
              aria-label="Dismiss alert banner"
              onClick={() => setToast(null)}
              className="shrink-0 -m-1 p-2 rounded-lg text-zinc-400 hover:text-white hover:bg-zinc-800"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        )}

        {/* Sticky Top Header */}
        <header className="sticky top-0 z-30 border-b border-zinc-800/90 bg-zinc-950/90 backdrop-blur-md px-3.5 py-2.5 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <div className="h-7 w-7 shrink-0 rounded-lg bg-emerald-500 flex items-center justify-center text-zinc-950 font-mono font-black text-sm shadow-sm">
              <Terminal className="w-4 h-4 stroke-[2.5]" />
            </div>
            <div className="min-w-0">
              <div className="flex items-center gap-1.5 min-w-0">
                <span className="text-sm font-black tracking-tight text-white">
                  FINN
                </span>
                <span
                  title={dataStatus.detail}
                  className="inline-flex min-w-0 items-center gap-1 rounded-full bg-zinc-900 border border-zinc-800 px-1.5 py-0.5 text-[9px] font-mono text-zinc-300"
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                      !backendOnline
                        ? "bg-rose-500"
                        : dataStatus.live
                          ? "bg-emerald-400 animate-pulse"
                          : "bg-amber-400"
                    }`}
                  />
                  <span className="truncate">{backendOnline ? dataStatus.label : "OFFLINE"}</span>
                </span>
              </div>
              <p
                className={`truncate text-[10px] font-mono ${
                  dataStatus.asOf && dataStatus.stale ? "text-amber-400/90" : "text-zinc-500"
                }`}
              >
                {dataStatus.asOf
                  ? `Data as of ${formatIst(dataStatus.asOf)}${dataStatus.stale ? " • stale" : ""}`
                  : digest
                    ? "Never refreshed • cached data"
                    : "Primary-Source Equity Intelligence"}
              </p>
            </div>
          </div>

          {/* AI Cost & 3-Tier Routing Pill */}
          <button
            type="button"
            onClick={() => {
              loadTelemetryAndCache();
              setCostDrawerOpen(true);
            }}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-emerald-500/35 bg-emerald-500/10 hover:bg-emerald-500/20 px-2.5 py-1 text-[11px] font-mono text-emerald-300 transition"
            title="View Vertex AI 3-Tier Model Routing & Token Spend"
          >
            <Cpu className="w-3 h-3 text-emerald-400" />
            <span className="font-semibold">${totalCostUsd.toFixed(3)}</span>
            <span className="text-[9px] text-emerald-400/80 hidden xs:inline">
              • 3-Tier
            </span>
          </button>
        </header>

        {/* Active Tab Viewport */}
        <main className="flex-1 px-3.5 pt-3 pb-20 overflow-x-hidden">
          {activeTab === "push" && (
            <PushTab
              digest={digest}
              loading={digestLoading}
              refreshing={refreshingPush}
              refreshError={refreshError}
              onDismissRefreshError={() => setRefreshError(null)}
              window={digestWindow}
              selectedTicker={digestTicker}
              portfolio={portfolio}
              onChangeWindow={setDigestWindow}
              onChangeTicker={setDigestTicker}
              onRefreshLive={handleRefreshLivePush}
              onOpenPull={handleOpenPullFromAnywhere}
              onFilingEnriched={handleFilingEnriched}
            />
          )}

          {activeTab === "alerts" && (
            <AlertsTab
              alertsData={alertsData}
              loading={alertsLoading}
              savingRules={savingRules}
              rulesError={rulesError}
              onDismissRulesError={() => setRulesError(null)}
              onUpdateRules={handleUpdateAlertRules}
              onOpenPull={handleOpenPullFromAnywhere}
              window={alertsWindow}
              onChangeWindow={setAlertsWindow}
              newAlertIds={newAlertIds}
              onMarkAllRead={handleMarkAllRead}
            />
          )}

          {activeTab === "pull" && (
            <PullTab
              pullData={pullData}
              loading={pullLoading}
              error={pullError}
              activeTicker={activePullTicker}
              portfolio={portfolio}
              cachedReports={cachedReports}
              onSelectTicker={loadPullReport}
              onCostUpdated={loadTelemetryAndCache}
              routing={costData?.routing}
            />
          )}

          {activeTab === "portfolio" && (
            <PortfolioTab
              portfolio={portfolio}
              loading={portfolioLoading}
              onAddStock={handleAddPortfolioStock}
              onRemoveStock={handleRemovePortfolioStock}
              onOpenPull={handleOpenPullFromAnywhere}
              onFilterDigest={handleFilterDigestFromPortfolio}
            />
          )}
        </main>

        {/* Sticky Bottom Mobile Tab Navigation */}
        <nav className="fixed bottom-0 left-1/2 -translate-x-1/2 w-full max-w-[430px] z-30 border-t border-zinc-800/95 bg-zinc-950/95 backdrop-blur-md px-2 py-1.5">
          <div className="grid grid-cols-4 gap-1">
            <button
              type="button"
              onClick={() => setActiveTab("push")}
              className={`flex flex-col items-center justify-center min-h-11 rounded-xl py-1.5 transition relative ${
                activeTab === "push"
                  ? "bg-emerald-500/15 text-emerald-400"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              <div className="relative">
                <Radio className="w-4 h-4" />
                {materialCount > 0 && (
                  <span className="absolute -top-1.5 -right-3 rounded-full bg-emerald-500 px-1 text-[9px] font-mono font-bold text-zinc-950">
                    {materialCount}
                  </span>
                )}
              </div>
              <span className="mt-1 text-[10px] font-semibold">Push Digest</span>
            </button>

            <button
              type="button"
              onClick={() => openTab("alerts")}
              aria-label={unseenCount > 0 ? `Alerts, ${unseenCount} new` : "Alerts"}
              className={`flex flex-col items-center justify-center min-h-11 rounded-xl py-1.5 transition relative ${
                activeTab === "alerts"
                  ? "bg-amber-500/15 text-amber-400"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              <div className="relative">
                <Bell className="w-4 h-4" />
                {unseenCount > 0 ? (
                  <span
                    data-badge="unseen"
                    className="absolute -top-2 -right-3.5 rounded-full bg-rose-500 px-1.5 text-[10px] font-mono font-black text-white ring-2 ring-zinc-950 shadow-[0_0_8px_rgba(244,63,94,0.8)]"
                  >
                    {unseenCount}
                  </span>
                ) : (
                  totalAlerts > 0 && (
                    <span
                      data-badge="total"
                      className="absolute -top-1.5 -right-3 rounded-full bg-zinc-800 px-1 text-[9px] font-mono text-zinc-400"
                    >
                      {totalAlerts}
                    </span>
                  )
                )}
              </div>
              <span className="mt-1 text-[10px] font-semibold">Alerts</span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("pull")}
              className={`flex flex-col items-center justify-center min-h-11 rounded-xl py-1.5 transition relative ${
                activeTab === "pull"
                  ? "bg-cyan-500/15 text-cyan-400"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              <div className="relative">
                <FileSearch className="w-4 h-4" />
                {/* Starts at the icon's right edge so a full ticker fits; only 9+ char tickers get an ellipsis */}
                <span className="absolute -top-1.5 left-2.5 max-w-[3.25rem] truncate whitespace-nowrap rounded-full bg-cyan-400/20 border border-cyan-400/40 px-1 text-[8px] font-mono text-cyan-300">
                  {activePullTicker}
                </span>
              </div>
              <span className="mt-1 text-[10px] font-semibold">Pull Deep</span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("portfolio")}
              className={`flex flex-col items-center justify-center min-h-11 rounded-xl py-1.5 transition relative ${
                activeTab === "portfolio"
                  ? "bg-emerald-500/15 text-emerald-400"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              <div className="relative">
                <Briefcase className="w-4 h-4" />
                <span className="absolute -top-1.5 -right-3 rounded-full bg-zinc-800 px-1 text-[9px] font-mono text-zinc-300">
                  {portfolio.length}
                </span>
              </div>
              <span className="mt-1 text-[10px] font-semibold">Portfolio</span>
            </button>
          </div>
        </nav>

        {/* AI Cost & Routing Modal/Drawer */}
        <CostDrawer
          open={costDrawerOpen}
          onClose={() => setCostDrawerOpen(false)}
          costData={costData}
          cachedReports={cachedReports}
        />
      </div>
    </div>
  );
}

