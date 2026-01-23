"use client";

import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Badge, Button } from "@/components/ui/common";
import { format } from "date-fns";
import { ExpandableText } from "@/components/ui/ExpandableText";
import { useState } from "react";
import { RefreshCw, Filter } from "lucide-react";

const fetcher = (path: string) => fetchApi(path);

export default function LogsPage() {
    const [filterSeverity, setFilterSeverity] = useState<string>("");
    const { data, error, isLoading, mutate } = useSWR(`/admin/events?limit=100${filterSeverity ? `&severity=${filterSeverity}` : ""}`, fetcher);

    return (
        <div className="p-8 space-y-8">
            <div className="flex justify-between items-center">
                <div>
                    <h1 className="text-2xl font-bold text-gray-900">Error Logs</h1>
                    <p className="text-gray-500">システムエラーと警告の全ログ (直近100件)</p>
                </div>
                <div className="flex space-x-2">
                    <select
                        className="border rounded p-2 text-sm"
                        value={filterSeverity}
                        onChange={(e) => setFilterSeverity(e.target.value)}
                    >
                        <option value="">All Levels</option>
                        <option value="ERROR">ERROR</option>
                        <option value="WARN">WARN</option>
                        <option value="INFO">INFO</option>
                    </select>
                    <Button variant="secondary" onClick={() => mutate()} disabled={isLoading}>
                        <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
                    </Button>
                </div>
            </div>

            <Card>
                {error ? (
                    <div className="p-4 text-red-600">Failed to load logs: {error.message}</div>
                ) : (
                    <div className="overflow-x-auto">
                        <table className="min-w-full divide-y divide-gray-200">
                            <thead className="bg-gray-50">
                                <tr>
                                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Time</th>
                                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Level</th>
                                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">User / Request</th>
                                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Type / Code</th>
                                    <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Message</th>
                                </tr>
                            </thead>
                            <tbody className="bg-white divide-y divide-gray-200">
                                {!data?.events?.length ? (
                                    <tr>
                                        <td colSpan={5} className="px-6 py-8 text-center text-gray-500">
                                            {isLoading ? "Loading..." : "No logs found matching criteria."}
                                        </td>
                                    </tr>
                                ) : (
                                    data.events.map((event: any) => (
                                        <tr key={event.id} className="hover:bg-gray-50">
                                            {/* Timestamp */}
                                            <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 align-top">
                                                {event.ts ? format(new Date(event.ts), "MM/dd HH:mm:ss") : "-"}
                                            </td>

                                            {/* Level */}
                                            <td className="px-6 py-4 whitespace-nowrap align-top">
                                                <Badge variant={event.severity === "ERROR" ? "error" : event.severity === "WARN" ? "warning" : "default"}>
                                                    {event.severity}
                                                </Badge>
                                            </td>

                                            {/* User & Request Info */}
                                            <td className="px-6 py-4 text-sm text-gray-900 align-top">
                                                <div className="font-mono text-xs text-blue-600">
                                                    {event.uid ? event.uid.slice(0, 12) + "..." : "Anonymous"}
                                                </div>
                                                {event.endpoint && (
                                                    <div className="text-xs text-gray-500 mt-1">
                                                        {event.props?.method || "REQ"}: {event.endpoint}
                                                    </div>
                                                )}
                                                {event.props?.remoteIp && (
                                                    <div className="text-xs text-gray-400">
                                                        IP: {event.props.remoteIp}
                                                    </div>
                                                )}
                                            </td>

                                            {/* Type & Error Code */}
                                            <td className="px-6 py-4 text-sm text-gray-900 align-top">
                                                <div className="font-medium">{event.type}</div>
                                                {(event.errorCode || event.statusCode) && (
                                                    <div className="text-xs text-red-500 mt-1 font-mono">
                                                        {event.errorCode} {event.statusCode && `(${event.statusCode})`}
                                                    </div>
                                                )}
                                            </td>

                                            {/* Message */}
                                            <td className="px-6 py-4 text-sm text-gray-600 align-top max-w-md">
                                                <ExpandableText text={event.message} limit={80} />
                                                {event.debug && (
                                                    <details className="mt-2">
                                                        <summary className="text-xs text-gray-400 cursor-pointer hover:text-gray-600">Raw Debug Data</summary>
                                                        <pre className="text-xs bg-gray-50 p-2 rounded mt-1 overflow-x-auto">
                                                            {JSON.stringify(event.debug, null, 2)}
                                                        </pre>
                                                    </details>
                                                )}
                                                {event.traceId && (
                                                    <div className="text-xs text-gray-400 mt-1">Trace: {event.traceId}</div>
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
        </div>
    );
}
