"use client";

import { useMemo, useState } from "react";
import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Button, Badge } from "@/components/ui/common";
import { RefreshCw, Info } from "lucide-react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip as RechartsTooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Legend,
} from "recharts";

const fetcher = (url: string) => fetchApi(url);

type CostBreakdown = {
  vertexUsd: number;
  firestoreUsd: number;
  cloudRunUsd: number;
  storageUsd: number;
  sttUsd: number;
};

type OverviewResponse = {
  range: { fromDate: string; toDate: string };
  revenueJpy: number;
  estimatedCostUsd: number;
  estimatedCostJpy: number;
  grossProfitJpy: number;
  grossMarginPct: number;
  costBreakdown: CostBreakdown;
  tokens: { input: number; output: number };
  usage: {
    activeUsers: number;
    sessionCount: number;
    recordingSeconds: number;
    avgCostUsdPerSession: number;
  };
  reconciled: boolean;
};

type TimeseriesPoint = {
  date: string;
  costUsd: number;
  vertexUsd: number;
  firestoreUsd: number;
  cloudRunUsd: number;
  storageUsd: number;
  sttUsd: number;
  revenueJpy: number;
  grossProfitJpy: number;
};

type TopUser = {
  userId: string;
  accountId: string | null;
  costUsd: number;
  costJpy: number;
  sessionCount: number;
  eventCount: number;
  inputTokens: number;
  outputTokens: number;
  topFeature: string | null;
};

type TopSession = {
  sessionId: string;
  ownerUid: string | null;
  accountId: string | null;
  costUsd: number;
  costJpy: number;
  eventCount: number;
  inputTokens: number;
  outputTokens: number;
  topFeature: string | null;
};

type FeatureRow = {
  feature: string;
  costUsd: number;
  costJpy: number;
  callCount: number;
  sessionCount: number;
  userCount: number;
  inputTokens: number;
  outputTokens: number;
  avgInputTokens: number;
  avgOutputTokens: number;
  avgCostUsd: number;
  avgDurationMs: number;
};

const BREAKDOWN_COLORS: Record<string, string> = {
  Vertex: "#3b82f6",
  Firestore: "#f59e0b",
  "Cloud Run": "#10b981",
  Storage: "#8b5cf6",
  STT: "#ec4899",
};

function ymd(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function fmtUsd(n: number, digits = 4): string {
  if (!Number.isFinite(n)) return "$0";
  return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: digits })}`;
}

function fmtJpy(n: number): string {
  if (!Number.isFinite(n)) return "¥0";
  return `¥${Math.round(n).toLocaleString("ja-JP")}`;
}

function fmtInt(n: number): string {
  if (!Number.isFinite(n)) return "0";
  return Math.round(n).toLocaleString("en-US");
}

export default function CostsPage() {
  const today = new Date();
  const firstOfMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  const [fromDate, setFromDate] = useState(ymd(firstOfMonth));
  const [toDate, setToDate] = useState(ymd(today));

  const qs = `from_date=${fromDate}&to_date=${toDate}`;
  const overview = useSWR<OverviewResponse>(`/admin/costs/overview?${qs}`, fetcher, { revalidateOnFocus: false });
  const timeseries = useSWR<{ items: TimeseriesPoint[] }>(`/admin/costs/timeseries?${qs}`, fetcher, { revalidateOnFocus: false });
  const topUsers = useSWR<{ items: TopUser[] }>(`/admin/costs/top-users?${qs}&limit=10`, fetcher, { revalidateOnFocus: false });
  const topSessions = useSWR<{ items: TopSession[] }>(`/admin/costs/top-sessions?${qs}&limit=10`, fetcher, { revalidateOnFocus: false });
  const features = useSWR<{ items: FeatureRow[] }>(`/admin/costs/features?${qs}`, fetcher, { revalidateOnFocus: false });

  const refreshing = overview.isValidating || timeseries.isValidating || topUsers.isValidating || topSessions.isValidating || features.isValidating;

  const refreshAll = () => {
    overview.mutate();
    timeseries.mutate();
    topUsers.mutate();
    topSessions.mutate();
    features.mutate();
  };

  const pieData = useMemo(() => {
    const b = overview.data?.costBreakdown;
    if (!b) return [];
    return [
      { name: "Vertex", value: b.vertexUsd },
      { name: "Firestore", value: b.firestoreUsd },
      { name: "Cloud Run", value: b.cloudRunUsd },
      { name: "Storage", value: b.storageUsd },
      { name: "STT", value: b.sttUsd },
    ].filter((e) => e.value > 0);
  }, [overview.data]);

  const totalCostUsd = overview.data?.estimatedCostUsd ?? 0;
  const allZero = totalCostUsd === 0 && !overview.isLoading && !overview.error;

  return (
    <div className="p-8 space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Costs</h1>
          <p className="text-sm text-gray-500">原価・粗利の可視化（Vertex / Firestore / Cloud Run / Storage / STT）</p>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <DateRangeControl
            fromDate={fromDate}
            toDate={toDate}
            onFromChange={setFromDate}
            onToChange={setToDate}
          />
          <Button variant="ghost" onClick={refreshAll} disabled={refreshing}>
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />
          </Button>
        </div>
      </div>

      {/* Empty-state banner */}
      {allZero && (
        <Card className="flex items-start gap-3 border-amber-200 bg-amber-50">
          <Info className="h-5 w-5 text-amber-600 shrink-0 mt-0.5" />
          <div className="text-sm text-amber-900">
            <div className="font-medium">この期間のコストデータはまだ集計されていません。</div>
            <div className="text-amber-800 mt-1">
              <code>usage_events</code> コレクションにコスト計測フィールド（<code>costBreakdown</code>, <code>estimatedCostUsd</code>）が書き込まれると、ここに数値が反映されます。
            </div>
          </div>
        </Card>
      )}

      {/* KPI */}
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-4">
        <KpiCard
          title="Total Cost"
          primary={fmtUsd(overview.data?.estimatedCostUsd ?? 0, 4)}
          secondary={fmtJpy(overview.data?.estimatedCostJpy ?? 0)}
          loading={overview.isLoading}
        />
        <KpiCard
          title="Active Users"
          primary={fmtInt(overview.data?.usage.activeUsers ?? 0)}
          secondary={`${fmtInt(overview.data?.usage.sessionCount ?? 0)} sessions`}
          loading={overview.isLoading}
        />
        <KpiCard
          title="Avg Cost / Session"
          primary={fmtUsd(overview.data?.usage.avgCostUsdPerSession ?? 0, 6)}
          loading={overview.isLoading}
        />
        <KpiCard
          title="LLM Tokens (in / out)"
          primary={`${fmtInt(overview.data?.tokens.input ?? 0)} / ${fmtInt(overview.data?.tokens.output ?? 0)}`}
          loading={overview.isLoading}
        />
        <KpiCard
          title="Reconciled"
          primary={overview.data?.reconciled ? "Yes" : "No"}
          secondary={overview.data?.reconciled ? "月次実請求で補正済" : "Phase 1 estimate"}
          badge={
            overview.data ? (
              <Badge variant={overview.data.reconciled ? "success" : "warning"}>
                {overview.data.reconciled ? "final" : "estimate"}
              </Badge>
            ) : undefined
          }
          loading={overview.isLoading}
        />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6">
        <Card className="lg:col-span-2">
          <h2 className="text-sm font-medium text-gray-700 mb-4">Cost Composition</h2>
          <div className="h-72">
            {pieData.length === 0 ? (
              <div className="h-full flex items-center justify-center text-gray-400 text-sm">データなし</div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={pieData}
                    dataKey="value"
                    nameKey="name"
                    outerRadius={96}
                    label={(e: { name?: string; percent?: number }) =>
                      `${e.name ?? ""} ${((e.percent ?? 0) * 100).toFixed(1)}%`
                    }
                  >
                    {pieData.map((entry) => (
                      <Cell key={entry.name} fill={BREAKDOWN_COLORS[entry.name] ?? "#9ca3af"} />
                    ))}
                  </Pie>
                  <RechartsTooltip formatter={(v: number | undefined) => fmtUsd(Number(v ?? 0), 6)} />
                </PieChart>
              </ResponsiveContainer>
            )}
          </div>
        </Card>

        <Card className="lg:col-span-3">
          <h2 className="text-sm font-medium text-gray-700 mb-4">Daily Cost Trend (USD)</h2>
          <div className="h-72">
            {(timeseries.data?.items?.length ?? 0) === 0 ? (
              <div className="h-full flex items-center justify-center text-gray-400 text-sm">データなし</div>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={timeseries.data?.items ?? []} margin={{ top: 0, right: 8, left: -16, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <RechartsTooltip
                    contentStyle={{ fontSize: 12 }}
                    formatter={(v: number | undefined, name: string | undefined) => [fmtUsd(Number(v ?? 0), 6), name ?? ""]}
                  />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Line type="monotone" dataKey="costUsd" name="Total" stroke="#0f172a" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="vertexUsd" name="Vertex" stroke={BREAKDOWN_COLORS.Vertex} strokeWidth={1.5} dot={false} />
                  <Line type="monotone" dataKey="firestoreUsd" name="Firestore" stroke={BREAKDOWN_COLORS.Firestore} strokeWidth={1.5} dot={false} />
                  <Line type="monotone" dataKey="cloudRunUsd" name="Cloud Run" stroke={BREAKDOWN_COLORS["Cloud Run"]} strokeWidth={1.5} dot={false} />
                  <Line type="monotone" dataKey="storageUsd" name="Storage" stroke={BREAKDOWN_COLORS.Storage} strokeWidth={1.5} dot={false} />
                  <Line type="monotone" dataKey="sttUsd" name="STT" stroke={BREAKDOWN_COLORS.STT} strokeWidth={1.5} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>
        </Card>
      </div>

      {/* Feature breakdown */}
      <Card>
        <h2 className="text-sm font-medium text-gray-700 mb-4">By Feature</h2>
        <DataTable
          loading={features.isLoading}
          error={features.error}
          rows={features.data?.items ?? []}
          empty="機能別のコストデータがありません"
          columns={[
            { key: "feature", label: "Feature", render: (r) => <span className="font-medium text-gray-900">{r.feature}</span> },
            { key: "costUsd", label: "Cost (USD)", render: (r) => fmtUsd(r.costUsd, 6), align: "right" },
            { key: "costJpy", label: "Cost (JPY)", render: (r) => fmtJpy(r.costJpy), align: "right" },
            { key: "callCount", label: "Calls", render: (r) => fmtInt(r.callCount), align: "right" },
            { key: "sessionCount", label: "Sessions", render: (r) => fmtInt(r.sessionCount), align: "right" },
            { key: "userCount", label: "Users", render: (r) => fmtInt(r.userCount), align: "right" },
            { key: "avgInputTokens", label: "Avg In", render: (r) => fmtInt(r.avgInputTokens), align: "right" },
            { key: "avgOutputTokens", label: "Avg Out", render: (r) => fmtInt(r.avgOutputTokens), align: "right" },
            { key: "avgCostUsd", label: "Avg Cost", render: (r) => fmtUsd(r.avgCostUsd, 8), align: "right" },
            { key: "avgDurationMs", label: "Avg ms", render: (r) => fmtInt(r.avgDurationMs), align: "right" },
          ]}
        />
      </Card>

      {/* Top users / sessions */}
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <Card>
          <h2 className="text-sm font-medium text-gray-700 mb-4">Top Cost Users (10)</h2>
          <DataTable
            loading={topUsers.isLoading}
            error={topUsers.error}
            rows={topUsers.data?.items ?? []}
            empty="高コストユーザーがいません"
            columns={[
              { key: "userId", label: "UID", render: (r) => <span className="font-mono text-xs text-gray-600">{(r.userId ?? "-").slice(0, 10)}…</span> },
              { key: "costUsd", label: "USD", render: (r) => fmtUsd(r.costUsd, 6), align: "right" },
              { key: "costJpy", label: "JPY", render: (r) => fmtJpy(r.costJpy), align: "right" },
              { key: "sessionCount", label: "Sessions", render: (r) => fmtInt(r.sessionCount), align: "right" },
              { key: "eventCount", label: "Events", render: (r) => fmtInt(r.eventCount), align: "right" },
              { key: "topFeature", label: "Top Feature", render: (r) => r.topFeature ?? "-" },
            ]}
          />
        </Card>

        <Card>
          <h2 className="text-sm font-medium text-gray-700 mb-4">Top Cost Sessions (10)</h2>
          <DataTable
            loading={topSessions.isLoading}
            error={topSessions.error}
            rows={topSessions.data?.items ?? []}
            empty="高コストセッションがありません"
            columns={[
              { key: "sessionId", label: "Session", render: (r) => <span className="font-mono text-xs text-gray-600">{(r.sessionId ?? "-").slice(0, 12)}…</span> },
              { key: "ownerUid", label: "Owner", render: (r) => <span className="font-mono text-xs text-gray-600">{(r.ownerUid ?? "-").slice(0, 10)}…</span> },
              { key: "costUsd", label: "USD", render: (r) => fmtUsd(r.costUsd, 6), align: "right" },
              { key: "costJpy", label: "JPY", render: (r) => fmtJpy(r.costJpy), align: "right" },
              { key: "eventCount", label: "Events", render: (r) => fmtInt(r.eventCount), align: "right" },
              { key: "topFeature", label: "Top Feature", render: (r) => r.topFeature ?? "-" },
            ]}
          />
        </Card>
      </div>
    </div>
  );
}

function DateRangeControl({
  fromDate,
  toDate,
  onFromChange,
  onToChange,
}: {
  fromDate: string;
  toDate: string;
  onFromChange: (v: string) => void;
  onToChange: (v: string) => void;
}) {
  const setPreset = (days: number) => {
    const to = new Date();
    const from = new Date();
    from.setDate(to.getDate() - days + 1);
    onFromChange(ymd(from));
    onToChange(ymd(to));
  };
  const setThisMonth = () => {
    const now = new Date();
    onFromChange(ymd(new Date(now.getFullYear(), now.getMonth(), 1)));
    onToChange(ymd(now));
  };
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <div className="flex rounded-lg border border-gray-200 bg-white overflow-hidden">
        {[
          { label: "7日", fn: () => setPreset(7) },
          { label: "30日", fn: () => setPreset(30) },
          { label: "今月", fn: setThisMonth },
        ].map((p) => (
          <button
            key={p.label}
            onClick={p.fn}
            className="px-3 py-1.5 text-sm font-medium text-gray-600 hover:bg-gray-100"
          >
            {p.label}
          </button>
        ))}
      </div>
      <input
        type="date"
        value={fromDate}
        onChange={(e) => onFromChange(e.target.value)}
        className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm bg-white"
      />
      <span className="text-gray-400 text-sm">→</span>
      <input
        type="date"
        value={toDate}
        onChange={(e) => onToChange(e.target.value)}
        className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm bg-white"
      />
    </div>
  );
}

function KpiCard({
  title,
  primary,
  secondary,
  badge,
  loading,
}: {
  title: string;
  primary: string;
  secondary?: string;
  badge?: React.ReactNode;
  loading?: boolean;
}) {
  return (
    <Card className="relative overflow-hidden">
      <div className="flex items-start justify-between gap-2">
        <p className="truncate text-xs font-medium text-gray-500 uppercase tracking-wide">{title}</p>
        {badge}
      </div>
      <div className="mt-2 text-2xl font-semibold text-gray-900 tabular-nums">
        {loading ? <span className="inline-block h-6 w-24 bg-gray-100 rounded animate-pulse" /> : primary}
      </div>
      {secondary && (
        <div className="mt-1 text-xs text-gray-500 tabular-nums">{secondary}</div>
      )}
    </Card>
  );
}

type Column<T> = {
  key: string;
  label: string;
  render: (r: T) => React.ReactNode;
  align?: "left" | "right";
};

function DataTable<T>({
  loading,
  error,
  rows,
  columns,
  empty,
}: {
  loading?: boolean;
  error?: Error | undefined;
  rows: T[];
  columns: Column<T>[];
  empty: string;
}) {
  if (error) {
    return <div className="text-sm text-red-600">Failed to load: {(error as Error).message}</div>;
  }
  if (loading) {
    return <div className="text-sm text-gray-400">Loading...</div>;
  }
  if (rows.length === 0) {
    return <div className="text-sm text-gray-400">{empty}</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-gray-200">
        <thead>
          <tr>
            {columns.map((c) => (
              <th
                key={c.key}
                className={`px-3 py-2 text-xs font-medium text-gray-500 uppercase tracking-wider ${
                  c.align === "right" ? "text-right" : "text-left"
                }`}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {rows.map((r, i) => (
            <tr key={i} className="hover:bg-gray-50">
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={`px-3 py-2 text-sm text-gray-700 whitespace-nowrap tabular-nums ${
                    c.align === "right" ? "text-right" : "text-left"
                  }`}
                >
                  {c.render(r)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
