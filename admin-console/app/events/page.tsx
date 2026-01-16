"use client";

import { useState } from "react";
import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Button, Badge } from "@/components/ui/common";
import { format } from "date-fns";
import { Filter, Search, ChevronRight, ChevronLeft } from "lucide-react";
import { ExpandableText } from "@/components/ui/ExpandableText";

const fetcher = (url: string) => fetchApi(url);

export default function EventsPage() {
  const [filter, setFilter] = useState({
    severity: "",
    type: "",
    uid: "",
    sessionId: "",
  });

  // Query String construction
  const queryParams = new URLSearchParams();
  if (filter.severity) queryParams.append("severity", filter.severity);
  if (filter.type) queryParams.append("type", filter.type);
  if (filter.uid) queryParams.append("uid", filter.uid);
  if (filter.sessionId) queryParams.append("sessionId", filter.sessionId);
  
  const { data, error, isLoading, mutate } = useSWR(`/admin/events?limit=50&${queryParams.toString()}`, fetcher);

  const handleFilterChange = (key: string, value: string) => {
    setFilter(prev => ({ ...prev, [key]: value }));
  };

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Events & Alerts</h1>
          <p className="text-gray-500">システムログとアラート履歴の検索</p>
        </div>
        <Button variant="ghost" onClick={() => mutate()}>Refresh</Button>
      </div>

      {/* Filters */}
      <Card className="p-4">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Severity</label>
            <select 
              className="block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 sm:text-sm p-2 border"
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
            <label className="block text-sm font-medium text-gray-700 mb-1">Type</label>
            <input 
              type="text" 
              placeholder="e.g. STT_FAILED"
              className="block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 sm:text-sm p-2 border"
              value={filter.type}
              onChange={(e) => handleFilterChange("type", e.target.value)}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">UID</label>
            <input 
              type="text" 
              placeholder="User ID"
              className="block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 sm:text-sm p-2 border"
              value={filter.uid}
              onChange={(e) => handleFilterChange("uid", e.target.value)}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Session ID</label>
            <input 
              type="text" 
              placeholder="Session ID"
              className="block w-full rounded-md border-gray-300 shadow-sm focus:border-blue-500 focus:ring-blue-500 sm:text-sm p-2 border"
              value={filter.sessionId}
              onChange={(e) => handleFilterChange("sessionId", e.target.value)}
            />
          </div>
        </div>
      </Card>

      {/* Events Table */}
      <Card className="overflow-hidden">
        {isLoading ? (
          <div className="p-8 text-center text-gray-500">Loading events...</div>
        ) : error ? (
          <div className="p-8 text-center text-red-600">Failed to load events: {error.message}</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Time</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Level</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Type</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Message</th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Detail</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {data?.events.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-6 py-12 text-center text-gray-500">
                      No events found matching criteria.
                    </td>
                  </tr>
                ) : (
                  data?.events.map((event: any) => (
                    <tr key={event.id || Math.random()} className="hover:bg-gray-50">
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                        {event.ts ? format(new Date(event.ts), "MM/dd HH:mm:ss") : "-"}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <Badge variant={
                          event.severity === "ERROR" ? "error" : 
                          event.severity === "WARN" ? "warning" : "default"
                        }>
                          {event.severity}
                        </Badge>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900 font-medium">
                        {event.type}
                      </td>
                      <td className="px-6 py-4 text-sm text-gray-500">
                        <div className="max-w-md">
                           <ExpandableText text={event.message} limit={80} />
                        </div>
                        {event.errorCode && (
                           <span className="text-xs text-red-600 font-mono mt-1 block">{event.errorCode}</span>
                        )}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                         {/* Simple Detail View (Expandable in future) */}
                         <div className="space-y-1 text-xs">
                            {event.uid && <div>UID: <span className="font-mono text-gray-700">{event.uid.slice(0,6)}...</span></div>}
                            {event.serverSessionId && <div>SID: <span className="font-mono text-gray-700">{event.serverSessionId.slice(0,6)}...</span></div>}
                         </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      
      {/* Pagination (Simple Next/Prev using cursor - placeholder UI) */}
      <div className="flex justify-between items-center text-sm text-gray-500">
        <div>Showing up to 50 events</div>
        <div className="flex gap-2">
            <Button variant="secondary" disabled>Previous</Button>
            <Button variant="secondary" disabled={!data?.nextCursor}>Next</Button>
        </div>
      </div>
    </div>
  );
}
