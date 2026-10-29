"use client";

import React, { useState, useEffect, useCallback } from "react";
import {
  Radio,
  Bell,
  FileSearch,
  Briefcase,
  Cpu,
  Terminal,
} from "lucide-react";
import {
  AlertsResponse,
  CachedReportMeta,
  CostSummaryResponse,
  DigestResponse,
  PortfolioItem,
  PullResponse,
  TimeWindow,
} from "@/types/finn";
import { finnApi } from "@/lib/api";
import { PushTab } from "@/components/PushTab";
import { AlertsTab } from "@/components/AlertsTab";
import { PullTab } from "@/components/PullTab";
import { PortfolioTab } from "@/components/PortfolioTab";
import { CostDrawer } from "@/components/CostDrawer";

type ActiveTab = "push" | "alerts" | "pull" | "portfolio";

export default function FinnAppPage() {
  const [activeTab, setActiveTab] = useState<ActiveTab>("push");
  const [backendOnline, setBackendOnline] = useState<boolean>(true);

  // Shared state
  const [portfolio, setPortfolio] = useState<PortfolioItem[]>([]);
  const [portfolioLoading, setPortfolioLoading] = useState<boolean>(true);

  // Push Digest state
  const [digestWindow, setDigestWindow] = useState<TimeWindow>("14d");
  const [digestTicker, setDigestTicker] = useState<string | null>(null);
  const [digest, setDigest] = useState<DigestResponse | null>(null);
  const [digestLoading, setDigestLoading] = useState<boolean>(true);
  const [refreshingPush, setRefreshingPush] = useState<boolean>(false);

  // Alerts state
  const [alertsData, setAlertsData] = useState<AlertsResponse | null>(null);
  const [alertsLoading, setAlertsLoading] = useState<boolean>(true);
  const [savingRules, setSavingRules] = useState<boolean>(false);

  // Pull Research state
  const [activePullTicker, setActivePullTicker] = useState<string>("TRENT");
  const [pullData, setPullData] = useState<PullResponse | null>(null);
  const [pullLoading, setPullLoading] = useState<boolean>(false);
  const [pullError, setPullError] = useState<string | null>(null);
  const [cachedReports, setCachedReports] = useState<CachedReportMeta[]>([]);

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

  const loadAlerts = useCallback(async (win: TimeWindow) => {
    try {
      setAlertsLoading(true);
      const res = await finnApi.getAlerts(win);
      setAlertsData(res);
    } catch {
      // ignore
    } finally {
      setAlertsLoading(false);
    }
  }, []);

  const loadPullReport = useCallback(
    async (ticker: string, force = false) => {
      const clean = ticker.trim().toUpperCase();
      if (!clean) return;
      setActivePullTicker(clean);
      try {
        setPullLoading(true);
        setPullError(null);
        const res = await finnApi.getPullReport(clean, force);
        setPullData(res);
        await loadTelemetryAndCache();
        await loadPortfolio();
      } catch (err) {
        setPullError(
          err instanceof Error ? err.message : "Failed to load Pull research"
        );
      } finally {
        setPullLoading(false);
      }
    },
    [loadTelemetryAndCache, loadPortfolio]
  );

  // Initial boot
  useEffect(() => {
    loadPortfolio();
    loadTelemetryAndCache();
    loadAlerts("14d");
    loadPullReport("TRENT", false);
  }, [loadPortfolio, loadTelemetryAndCache, loadAlerts, loadPullReport]);

  useEffect(() => {
    loadDigest(digestWindow, digestTicker);
  }, [digestWindow, digestTicker, loadDigest]);

  async function handleRefreshLivePush() {
    try {
      setRefreshingPush(true);
      await finnApi.refreshPush(
        digestTicker ? [digestTicker] : undefined,
        14,
        3
      );
      await Promise.all([
        loadDigest(digestWindow, digestTicker),
        loadAlerts(digestWindow),
        loadPortfolio(),
        loadTelemetryAndCache(),
      ]);
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
      await finnApi.updateAlertRules(rules);
      await Promise.all([loadAlerts(digestWindow), loadPortfolio()]);
    } finally {
      setSavingRules(false);
    }
  }

  function handleOpenPullFromAnywhere(ticker: string) {
    setActiveTab("pull");
    if (
      !pullData ||
      pullData.ticker.toUpperCase() !== ticker.trim().toUpperCase()
    ) {
      loadPullReport(ticker, false);
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
      loadAlerts(digestWindow),
    ]);
  }

  async function handleRemovePortfolioStock(ticker: string) {
    await finnApi.removePortfolioStock(ticker);
    await Promise.all([
      loadPortfolio(),
      loadDigest(digestWindow, digestTicker),
      loadAlerts(digestWindow),
    ]);
  }

  function handleFilingEnriched() {
    loadDigest(digestWindow, digestTicker);
    loadTelemetryAndCache();
  }

  const totalCostUsd = costData?.totals?.cost_usd || 0;
  const materialCount = digest?.stats?.material_count || 0;
  const totalAlerts = alertsData?.total_alerts || 0;

  return (
    <div className="min-h-screen w-full bg-[#050507] flex justify-center">
      {/* Mobile-First Institutional Shell (430px max width on desktop, 100% on phone) */}
      <div className="w-full max-w-[430px] min-h-screen flex flex-col border-x border-zinc-800/80 bg-zinc-950 shadow-2xl relative">
        {/* Sticky Top Header */}
        <header className="sticky top-0 z-30 border-b border-zinc-800/90 bg-zinc-950/90 backdrop-blur-md px-3.5 py-2.5 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="h-7 w-7 rounded-lg bg-emerald-500 flex items-center justify-center text-zinc-950 font-mono font-black text-sm shadow-sm">
              <Terminal className="w-4 h-4 stroke-[2.5]" />
            </div>
            <div>
              <div className="flex items-center gap-1.5">
                <span className="text-sm font-black tracking-tight text-white">
                  FINN
                </span>
                <span className="inline-flex items-center gap-1 rounded-full bg-zinc-900 border border-zinc-800 px-1.5 py-0.5 text-[9px] font-mono text-zinc-300">
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      backendOnline
                        ? "bg-emerald-400 animate-pulse"
                        : "bg-rose-500"
                    }`}
                  />
                  {backendOnline ? "NSE/BSE LIVE" : "OFFLINE"}
                </span>
              </div>
              <p className="text-[10px] font-mono text-zinc-500">
                Primary-Source Equity Intelligence
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
            className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/35 bg-emerald-500/10 hover:bg-emerald-500/20 px-2.5 py-1 text-[11px] font-mono text-emerald-300 transition"
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
              onUpdateRules={handleUpdateAlertRules}
              onOpenPull={handleOpenPullFromAnywhere}
              window={digestWindow}
              onChangeWindow={setDigestWindow}
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
              className={`flex flex-col items-center justify-center rounded-xl py-1.5 transition relative ${
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
              onClick={() => setActiveTab("alerts")}
              className={`flex flex-col items-center justify-center rounded-xl py-1.5 transition relative ${
                activeTab === "alerts"
                  ? "bg-amber-500/15 text-amber-400"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              <div className="relative">
                <Bell className="w-4 h-4" />
                {totalAlerts > 0 && (
                  <span className="absolute -top-1.5 -right-3 rounded-full bg-amber-400 px-1 text-[9px] font-mono font-bold text-zinc-950">
                    {totalAlerts}
                  </span>
                )}
              </div>
              <span className="mt-1 text-[10px] font-semibold">Alerts</span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("pull")}
              className={`flex flex-col items-center justify-center rounded-xl py-1.5 transition relative ${
                activeTab === "pull"
                  ? "bg-cyan-500/15 text-cyan-400"
                  : "text-zinc-400 hover:text-zinc-200"
              }`}
            >
              <div className="relative">
                <FileSearch className="w-4 h-4" />
                <span className="absolute -top-1.5 -right-4 rounded-full bg-cyan-400/20 border border-cyan-400/40 px-1 text-[8px] font-mono text-cyan-300">
                  {activePullTicker.slice(0, 4)}
                </span>
              </div>
              <span className="mt-1 text-[10px] font-semibold">Pull Deep</span>
            </button>

            <button
              type="button"
              onClick={() => setActiveTab("portfolio")}
              className={`flex flex-col items-center justify-center rounded-xl py-1.5 transition relative ${
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

