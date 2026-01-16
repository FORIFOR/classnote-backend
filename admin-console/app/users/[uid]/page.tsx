"use client";

import { use, useState } from "react";
import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Button, Badge } from "@/components/ui/common";
import { format } from "date-fns";
import { useRouter } from "next/navigation";

const fetcher = (url: string) => fetchApi(url);

export default function UserDetailPage({ params }: { params: Promise<{ uid: string }> }) {
  const { uid } = use(params);
  const { data, error, isLoading } = useSWR(`/admin/users/${uid}`, fetcher);
  const router = useRouter();

  if (isLoading) return <div className="p-8">Loading user...</div>;
  if (error) return <div className="p-8 text-red-600">Failed to load user: {error.message}</div>;

  const { profile, stats, recentEvents } = data;

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">User Detail</h1>
        <Button variant="secondary" onClick={() => router.back()}>Back</Button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Profile Card */}
        <Card className="md:col-span-1 space-y-4">
          <h2 className="text-lg font-bold text-gray-900 border-b pb-2">Profile</h2>
          <div>
            <label className="block text-xs text-gray-500 uppercase">UID</label>
            <div className="font-mono text-sm break-all">{uid}</div>
          </div>
          <div>
            <label className="block text-xs text-gray-500 uppercase">Email</label>
            <div>{profile.email || "N/A"}</div>
          </div>
          <div>
            <label className="block text-xs text-gray-500 uppercase">Plan</label>
            <Badge variant={profile.plan === "pro" ? "info" : "default"}>{profile.plan || "free"}</Badge>
          </div>
          <div>
            <label className="block text-xs text-gray-500 uppercase">Status</label>
            <Badge variant={profile.securityState === "quarantined" ? "error" : "success"}>
                {profile.securityState || "active"}
            </Badge>
          </div>
        </Card>

        {/* Stats Card */}
        <Card className="md:col-span-2">
            <h2 className="text-lg font-bold text-gray-900 border-b pb-2 mb-4">Statistics</h2>
            <div className="grid grid-cols-2 gap-4">
                <div className="bg-gray-50 p-4 rounded">
                    <div className="text-gray-500 text-sm">Session Count</div>
                    <div className="text-2xl font-bold">{stats.sessionCount}</div>
                </div>
                {/* Add more stats placeholders */}
                <div className="bg-gray-50 p-4 rounded">
                    <div className="text-gray-500 text-sm">Total Recording</div>
                    <div className="text-2xl font-bold">- min</div>
                </div>
            </div>
        </Card>
      </div>

      {/* Recent Events */}
      <Card>
        <h2 className="text-lg font-bold text-gray-900 border-b pb-2 mb-4">Recent Events</h2>
        <table className="min-w-full divide-y divide-gray-200">
            <thead>
                <tr>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Time</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Message</th>
                </tr>
            </thead>
            <tbody>
                {recentEvents.map((e: any) => (
                    <tr key={e.id || Math.random()}>
                        <td className="px-4 py-2 text-sm text-gray-500">
                            {e.ts ? format(new Date(e.ts), "MM/dd HH:mm") : "-"}
                        </td>
                        <td className="px-4 py-2 text-sm">
                            <Badge variant={e.severity === "ERROR" ? "error" : "default"}>{e.type}</Badge>
                        </td>
                        <td className="px-4 py-2 text-sm text-gray-700">{e.message}</td>
                    </tr>
                ))}
            </tbody>
        </table>
      </Card>
    </div>
  );
}
