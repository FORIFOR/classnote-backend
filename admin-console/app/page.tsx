"use client";

import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Badge } from "@/components/ui/common";
import { 
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, ResponsiveContainer, BarChart, Bar 
} from "recharts";
import { AlertTriangle, Activity, ServerCrash, MicOff, ShieldAlert } from "lucide-react";
import { format } from "date-fns";
import { ja } from "date-fns/locale";
import { ExpandableText } from "@/components/ui/ExpandableText";

const fetcher = (path: string) => fetchApi(path);

export default function DashboardPage() {
  const { data, error, isLoading } = useSWR("/admin/stats/dashboard?period=24h", fetcher);

  if (isLoading) return <div className="p-8">Loading dashboard...</div>;
  if (error) return <div className="p-8 text-red-600">Failed to load dashboard: {error.message}</div>;

  const { kpi, chart, recentAlerts } = data;

  return (
    <div className="p-8 space-y-8">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
        <p className="text-gray-500">システムの状態と直近24時間のアクティビティ</p>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-4">
        <KpiCard 
          title="API Errors (5xx)" 
          value={kpi.error5xx} 
          icon={ServerCrash} 
          trend="Past 24h"
          color="text-red-600"
          bg="bg-red-50"
        />
        <KpiCard 
          title="STT Failures" 
          value={kpi.sttFailures} 
          icon={MicOff} 
          trend="Past 24h" 
          color="text-orange-600"
          bg="bg-orange-50"
        />
        <KpiCard 
          title="Abuse Detected" 
          value={kpi.abuseDetected} 
          icon={ShieldAlert} 
          trend="Past 24h"
          color="text-purple-600"
          bg="bg-purple-50"
        />
        <KpiCard 
          title="Active Jobs" 
          value={kpi.activeJobs || "-"} 
          icon={Activity} 
          trend="Current"
          color="text-blue-600"
          bg="bg-blue-50"
        />
      </div>

      {/* Charts */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        <Card className="lg:col-span-2">
          <h3 className="text-lg font-medium leading-6 text-gray-900 mb-4">Activity Trend</h3>
          <div className="h-72 w-full">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart}>
                <CartesianGrid strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="time" fontSize={12} tickLine={false} axisLine={false} />
                <YAxis fontSize={12} tickLine={false} axisLine={false} />
                <RechartsTooltip />
                <Line type="monotone" dataKey="jobs" stroke="#3b82f6" strokeWidth={2} dot={false} name="Jobs" />
                <Line type="monotone" dataKey="errors" stroke="#ef4444" strokeWidth={2} dot={false} name="Errors" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </Card>

        {/* Quick Stats or Sub Chart */}
        <Card>
           <h3 className="text-lg font-medium leading-6 text-gray-900 mb-4">Endpoint Errors</h3>
           <div className="flex h-72 items-center justify-center text-gray-400">
             (詳細データ未実装)
           </div>
        </Card>
      </div>

      {/* Recent Alerts */}
      <Card>
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-medium leading-6 text-gray-900">Recent Alerts</h3>
          <span className="text-sm text-gray-500">直近の重要イベント</span>
        </div>
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Timestamp</th>
                <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Level</th>
                <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Type</th>
                <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Message</th>
                <th scope="col" className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">UID</th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-gray-200">
              {recentAlerts.length === 0 ? (
                <tr>
                   <td colSpan={5} className="px-6 py-4 text-center text-sm text-gray-500">No alerts found.</td>
                </tr>
              ) : (
                recentAlerts.map((alert: any, index: number) => (
                  <tr key={alert.id || index}>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                      {alert.ts ? format(new Date(alert.ts), "MM/dd HH:mm:ss") : "-"}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                       <Badge variant={alert.severity === "ERROR" ? "error" : "warning"}>
                         {alert.severity}
                       </Badge>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-900">{alert.type}</td>
                    <td className="px-6 py-4 text-sm text-gray-500 max-w-xs">
                      <ExpandableText text={alert.message} limit={40} />
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500 font-mono">
                      {alert.uid ? alert.uid.slice(0, 8) + "..." : "-"}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}

function KpiCard({ title, value, icon: Icon, trend, color, bg }: any) {
  return (
    <Card className="relative overflow-hidden">
      <div className="flex items-baseline">
        <div className={`rounded-md p-3 ${bg}`}>
          <Icon className={`h-6 w-6 ${color}`} />
        </div>
        <div className="ml-4">
          <p className="truncate text-sm font-medium text-gray-500">{title}</p>
          <div className="flex items-baseline">
            <p className="text-2xl font-semibold text-gray-900">{value}</p>
            <p className="ml-2 text-sm text-gray-500">{trend}</p>
          </div>
        </div>
      </div>
    </Card>
  );
}