"use client";

import { useState, useCallback } from "react";
import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Badge, Button } from "@/components/ui/common";
import { format } from "date-fns";
import { ja } from "date-fns/locale";
import { ExpandableText } from "@/components/ui/ExpandableText";
import { RefreshCw, ChevronLeft, ChevronRight } from "lucide-react";
import Link from "next/link";

const fetcher = (path: string) => fetchApi(path);
const PAGE_SIZE = 50;

function buildQuery(severity: string, cursor?: string) {
  const params = new URLSearchParams();
  params.set("limit", String(PAGE_SIZE));
  // Default to errors/warnings only
  if (severity) params.set("severity", severity);
  if (cursor) params.set("cursor", cursor);
  return `/dashboard/events?${params.toString()}`;
}

export default function LogsPage() {
  const [severity, setSeverity] = useState<string>("ERROR");
  const [cursors, setCursors] = useState<string[]>([]);
  const currentCursor = cursors[cursors.length - 1] || "";

  const { data, error, isLoading, mutate } = useSWR(
    buildQuery(severity, currentCursor), fetcher, { revalidateOnFocus: false }
  );

  const handleSeverityChange = (v: string) => {
    setSeverity(v);
    setCursors([]);
  };

  const goNext = useCallback(() => {
    if (data?.nextCursor) setCursors(prev => [...prev, data.nextCursor]);
  }, [data?.nextCursor]);

  const goPrev = useCallback(() => {
    setCursors(prev => prev.slice(0, -1));
  }, []);

  const pageNum = cursors.length + 1;

  return (
    <div className="p-8 space-y-6">
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Error Logs</h1>
          <p className="text-sm text-gray-500">エラー・警告ログの確認</p>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex rounded-lg border border-gray-200 bg-white overflow-hidden">
            {["ERROR", "WARN", ""].map(s => (
              <button
                key={s || "ALL"}
                onClick={() => handleSeverityChange(s)}
                className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                  severity === s
                    ? "bg-slate-800 text-white"
                    : "text-gray-600 hover:bg-gray-100"
                }`}
              >
                {s || "ALL"}
              </button>
            ))}
          </div>
          <Button variant="ghost" onClick={() => mutate()} disabled={isLoading}>
            <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
          </Button>
        </div>
      </div>

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
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">User / Request</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type / Code</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Message</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 bg-white">
                {isLoading ? (
                  <tr><td colSpan={5} className="px-4 py-12 text-center text-gray-400">Loading...</td></tr>
                ) : !data?.events?.length ? (
                  <tr><td colSpan={5} className="px-4 py-12 text-center text-gray-400">
                    {severity ? `No ${severity} logs found` : "No logs found"}
                  </td></tr>
                ) : (
                  data.events.map((ev: any) => (
                    <tr key={ev.id} className="hover:bg-gray-50 text-sm">
                      <td className="px-4 py-3 whitespace-nowrap text-gray-500 tabular-nums align-top">
                        {ev.ts ? format(new Date(ev.ts), "MM/dd HH:mm:ss", { locale: ja }) : "-"}
                      </td>
                      <td className="px-4 py-3 whitespace-nowrap align-top">
                        <Badge variant={ev.severity === "ERROR" ? "error" : ev.severity === "WARN" ? "warning" : "default"}>
                          {ev.severity}
                        </Badge>
                      </td>
                      <td className="px-4 py-3 text-gray-900 align-top">
                        {ev.uid ? (
                          <Link href={`/users/${ev.uid}`} className="font-mono text-xs text-blue-600 hover:underline">
                            {ev.uid.slice(0, 12)}...
                          </Link>
                        ) : (
                          <span className="text-xs text-gray-400">Anonymous</span>
                        )}
                        {ev.endpoint && (
                          <div className="text-xs text-gray-500 mt-0.5">
                            {ev.props?.method || "REQ"}: {ev.endpoint}
                          </div>
                        )}
                        {ev.props?.remoteIp && (
                          <div className="text-xs text-gray-400">IP: {ev.props.remoteIp}</div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-gray-900 align-top">
                        <div className="font-medium text-sm">{ev.type}</div>
                        {(ev.errorCode || ev.statusCode) && (
                          <div className="text-xs text-red-500 mt-0.5 font-mono">
                            {ev.errorCode}{ev.statusCode && ` (${ev.statusCode})`}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-gray-600 align-top max-w-md">
                        <ExpandableText text={ev.message || "-"} limit={80} />
                        {ev.debug && (
                          <details className="mt-1">
                            <summary className="text-xs text-gray-400 cursor-pointer hover:text-gray-600">Debug</summary>
                            <pre className="text-xs bg-gray-50 p-2 rounded mt-1 overflow-x-auto max-h-40">
                              {JSON.stringify(ev.debug, null, 2)}
                            </pre>
                          </details>
                        )}
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
        <div>Page {pageNum} &middot; {data?.events?.length ?? 0} entries</div>
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
