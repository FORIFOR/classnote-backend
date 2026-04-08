"use client";

import { useState } from "react";
import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Button, Badge } from "@/components/ui/common";
import { RefreshCw } from "lucide-react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend } from "recharts";

const fetcher = (url: string) => fetchApi(url);

export default function DailyPage() {
  const [days, setDays] = useState(14);
  const { data, error, isLoading, mutate } = useSWR(
    `/dashboard/daily-sessions?days=${days}`, fetcher, { revalidateOnFocus: false }
  );

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Daily Sessions</h1>
          <p className="text-sm text-gray-500">
            日別録音統計
            {data && <span className="ml-2 font-medium text-gray-700">合計 {data.totalSessions} セッション</span>}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex rounded-lg border border-gray-200 bg-white overflow-hidden">
            {[7, 14, 30].map(d => (
              <button
                key={d}
                onClick={() => setDays(d)}
                className={`px-3 py-1.5 text-sm font-medium transition-colors ${
                  days === d ? "bg-slate-800 text-white" : "text-gray-600 hover:bg-gray-100"
                }`}
              >
                {d}日
              </button>
            ))}
          </div>
          <Button variant="ghost" onClick={() => mutate()} disabled={isLoading}>
            <RefreshCw className={`h-4 w-4 ${isLoading ? "animate-spin" : ""}`} />
          </Button>
        </div>
      </div>

      {error ? (
        <Card className="p-8 text-center text-red-600">Failed to load: {error.message}</Card>
      ) : isLoading ? (
        <Card className="p-12 text-center text-gray-400">Loading...</Card>
      ) : data?.days?.length ? (
        <>
          {/* Chart */}
          <Card>
            <h2 className="text-sm font-medium text-gray-700 mb-4">Session Volume</h2>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={data.days} margin={{ top: 0, right: 0, left: -20, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />
                <XAxis dataKey="date" tick={{ fontSize: 12 }} />
                <YAxis tick={{ fontSize: 12 }} />
                <Tooltip
                  contentStyle={{ fontSize: 13 }}
                  formatter={(value: any, name: any) => {
                    const labels: Record<string, string> = {
                      cloud: "Cloud", device: "Device", uniqueUsers: "Users"
                    };
                    return [value, labels[name] || name];
                  }}
                />
                <Legend
                  formatter={(value: string) => {
                    const labels: Record<string, string> = {
                      cloud: "Cloud STT", device: "On-device", uniqueUsers: "Users"
                    };
                    return labels[value] || value;
                  }}
                />
                <Bar dataKey="cloud" stackId="sessions" fill="#3b82f6" radius={[0, 0, 0, 0]} />
                <Bar dataKey="device" stackId="sessions" fill="#93c5fd" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </Card>

          {/* Table */}
          <Card className="!p-0 overflow-hidden">
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Sessions</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Users</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Cloud</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Device</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Transcript</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Summary</th>
                    <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">Minutes</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 bg-white">
                  {[...data.days].reverse().map((day: any, i: number) => (
                    <tr key={i} className="hover:bg-gray-50 text-sm">
                      <td className="px-4 py-3 font-medium text-gray-900">{day.date}</td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-900 font-semibold">{day.sessions}</td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-600">{day.uniqueUsers}</td>
                      <td className="px-4 py-3 text-right tabular-nums">
                        {day.cloud > 0 ? (
                          <Badge variant="info">{day.cloud}</Badge>
                        ) : (
                          <span className="text-gray-300">0</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-600">{day.device}</td>
                      <td className="px-4 py-3 text-right tabular-nums">
                        <span className={day.withTranscript === day.sessions ? "text-green-600" : "text-gray-600"}>
                          {day.withTranscript}
                        </span>
                        <span className="text-gray-300">/{day.sessions}</span>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums">
                        <span className={day.withSummary === day.sessions ? "text-green-600" : "text-gray-600"}>
                          {day.withSummary}
                        </span>
                        <span className="text-gray-300">/{day.sessions}</span>
                      </td>
                      <td className="px-4 py-3 text-right tabular-nums text-gray-600">
                        {day.totalMinutes > 0 ? `${day.totalMinutes}` : "-"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        </>
      ) : (
        <Card className="p-12 text-center text-gray-400">No data for this period</Card>
      )}
    </div>
  );
}
