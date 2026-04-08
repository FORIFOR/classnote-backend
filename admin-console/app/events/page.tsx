"use client";

import { useState, useCallback } from "react";
import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Button, Badge } from "@/components/ui/common";
import { format } from "date-fns";
import { ja } from "date-fns/locale";
import { RefreshCw, ChevronRight, ChevronLeft, X } from "lucide-react";
import { ExpandableText } from "@/components/ui/ExpandableText";
import Link from "next/link";

const fetcher = (url: string) => fetchApi(url);
const PAGE_SIZE = 50;

const EVENT_TYPES = [
  "API_ERROR", "AUTH_FAILED", "SESSION_CREATE", "SESSION_UPDATE", "SESSION_DELETE",
  "JOB_QUEUED", "JOB_STARTED", "JOB_COMPLETED", "JOB_FAILED",
  "STT_STARTED", "STT_COMPLETED", "STT_FAILED",
  "LLM_STARTED", "LLM_COMPLETED", "LLM_FAILED",
  "UPLOAD_SIGNED_URL", "UPLOAD_CHECK",
  "LIMIT_REACHED", "PAYMENT_REQUIRED", "ABUSE_DETECTED",
];

function buildQuery(filter: Record<string, string>, cursor?: string) {
  const params = new URLSearchParams();
  params.set("limit", String(PAGE_SIZE));
  if (filter.severity) params.set("severity", filter.severity);
  if (filter.type) params.set("type", filter.type);
  if (filter.uid) params.set("uid", filter.uid);
  if (filter.sessionId) params.set("sessionId", filter.sessionId);
  if (cursor) params.set("cursor", cursor);
  return `/dashboard/events?${params.toString()}`;
}

function SeverityBadge({ severity }: { severity: string }) {
  const v = severity === "ERROR" ? "error" : severity === "WARN" ? "warning" : "default";
  return <Badge variant={v}>{severity}</Badge>;
}

export default function EventsPage() {
  const [filter, setFilter] = useState<Record<string, string>>({
    severity: "", type: "", uid: "", sessionId: "",
  });
  const [cursors, setCursors] = useState<string[]>([]);
  const currentCursor = cursors[cursors.length - 1] || "";

  const { data, error, isLoading, mutate } = useSWR(
    buildQuery(filter, currentCursor), fetcher, { revalidateOnFocus: false }
  );

  const handleFilterChange = (key: string, value: string) => {
    setFilter(prev => ({ ...prev, [key]: value }));
    setCursors([]);
  };

  const clearFilters = () => {
    setFilter({ severity: "", type: "", uid: "", sessionId: "" });
    setCursors([]);
  };

  const hasActiveFilters = Object.values(filter).some(v => v);

  const goNext = useCallback(() => {
    if (data?.nextCursor) setCursors(prev => [...prev, data.nextCursor]);
  }, [data?.nextCursor]);

  const goPrev = useCallback(() => {
    setCursors(prev => prev.slice(0, -1));
  }, []);

  const pageNum = cursors.length + 1;

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Events</h1>
          <p className="text-sm text-gray-500">システムイベントの検索・閲覧</p>
        </div>
        <Button variant="ghost" onClick={() => mutate()} disabled={isLoading}>
          <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
        </Button>
      </div>

      {/* Filters */}
      <Card className="!p-4">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Severity</label>
            <select
              className="block w-full rounded-md border border-gray-300 text-sm p-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              value={filter.severity}
              onChange={(e) => handleFilterChange("severity", e.target.value)}
            >
              <option value="">All</option>
              <option value="ERROR">ERROR</option>
              <option value="WARN">WARN</option>
              <option value="INFO">INFO</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Type</label>
            <select
              className="block w-full rounded-md border border-gray-300 text-sm p-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              value={filter.type}
              onChange={(e) => handleFilterChange("type", e.target.value)}
            >
              <option value="">All</option>
              {EVENT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">User ID</label>
            <input
              type="text"
              placeholder="UID..."
              className="block w-full rounded-md border border-gray-300 text-sm p-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              value={filter.uid}
              onChange={(e) => handleFilterChange("uid", e.target.value)}
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-500 mb-1">Session ID</label>
            <input
              type="text"
              placeholder="Session ID..."
              className="block w-full rounded-md border border-gray-300 text-sm p-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              value={filter.sessionId}
              onChange={(e) => handleFilterChange("sessionId", e.target.value)}
            />
          </div>
          <div className="flex items-end">
            {hasActiveFilters && (
              <button onClick={clearFilters} className="flex items-center text-sm text-gray-500 hover:text-gray-700 p-2">
                <X className="h-4 w-4 mr-1" /> クリア
              </button>
            )}
          </div>
        </div>
      </Card>

      {/* Table */}
      <Card className="!p-0 overflow-hidden">
        {error ? (
          <div className="p-8 text-center text-red-600">Failed to load: {error.message}</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Time</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Level</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Message</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">User / Session</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 bg-white">
                {isLoading ? (
                  <tr><td colSpan={5} className="px-4 py-12 text-center text-gray-400">Loading...</td></tr>
                ) : !data?.events?.length ? (
                  <tr><td colSpan={5} className="px-4 py-12 text-center text-gray-400">No events found</td></tr>
                ) : (
                  data.events.map((ev: any) => (
                    <tr key={ev.id} className="hover:bg-gray-50 text-sm">
                      <td className="px-4 py-3 whitespace-nowrap text-gray-500 tabular-nums">
                        {ev.ts ? format(new Date(ev.ts), "MM/dd HH:mm:ss", { locale: ja }) : "-"}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap">
                        <SeverityBadge severity={ev.severity} />
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap font-medium text-gray-900">
                        {ev.type}
                        {ev.statusCode && <span className="ml-1 text-xs text-gray-400">({ev.statusCode})</span>}
                      </td>
                      <td className="px-4 py-3 text-gray-600 max-w-sm">
                        <ExpandableText text={ev.message || "-"} limit={80} />
                        {ev.errorCode && <span className="text-xs text-red-500 font-mono block mt-0.5">{ev.errorCode}</span>}
                      </td>
                      <td className="px-4 py-3 text-xs text-gray-500 space-y-0.5">
                        {ev.uid && (
                          <Link href={`/users/${ev.uid}`} className="font-mono text-blue-600 hover:underline block">
                            {ev.uid.slice(0, 10)}...
                          </Link>
                        )}
                        {ev.serverSessionId && (
                          <Link href={`/sessions/${ev.serverSessionId}`} className="font-mono text-gray-500 hover:underline block">
                            {ev.serverSessionId.slice(0, 8)}...
                          </Link>
                        )}
                        {ev.endpoint && <div className="text-gray-400">{ev.endpoint}</div>}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Pagination */}
      <div className="flex justify-between items-center text-sm text-gray-500">
        <div>
          Page {pageNum} &middot; {data?.events?.length ?? 0} events
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={goPrev} disabled={pageNum <= 1}>
            <ChevronLeft className="h-4 w-4 mr-1" /> Prev
          </Button>
          <Button variant="secondary" onClick={goNext} disabled={!data?.nextCursor}>
            Next <ChevronRight className="h-4 w-4 ml-1" />
          </Button>
        </div>
      </div>
    </div>
  );
}
