"use client";

import { use, useState } from "react";
import useSWR from "swr";
import { fetchApi } from "@/lib/api";
import { Card, Button, Badge } from "@/components/ui/common";
import { format } from "date-fns";
import { useRouter } from "next/navigation";

const fetcher = (url: string) => fetchApi(url);

export default function SessionDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, error, isLoading } = useSWR(`/admin/sessions/${id}`, fetcher);
  const router = useRouter();

  if (isLoading) return <div className="p-8">Loading session...</div>;
  if (error) return <div className="p-8 text-red-600">Failed to load session: {error.message}</div>;

  const { session, jobs, events } = data;

  return (
    <div className="p-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Session Detail</h1>
        <Button variant="secondary" onClick={() => router.back()}>Back</Button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <Card className="md:col-span-1 space-y-4">
          <h2 className="text-lg font-bold text-gray-900 border-b pb-2">Meta</h2>
          <div>
            <label className="block text-xs text-gray-500 uppercase">Title</label>
            <div>{session.title || "(No Title)"}</div>
          </div>
          <div>
            <label className="block text-xs text-gray-500 uppercase">Status</label>
            <Badge>{session.status}</Badge>
          </div>
          <div>
            <label className="block text-xs text-gray-500 uppercase">Owner UID</label>
            <div className="font-mono text-xs">{session.ownerUserId}</div>
          </div>
           <div>
            <label className="block text-xs text-gray-500 uppercase">Created</label>
            <div>{session.createdAt ? format(new Date(session.createdAt), "yyyy/MM/dd HH:mm") : "-"}</div>
          </div>
        </Card>

        <Card className="md:col-span-2">
            <h2 className="text-lg font-bold text-gray-900 border-b pb-2 mb-4">Job History</h2>
             <table className="min-w-full divide-y divide-gray-200">
            <thead>
                <tr>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Updated</th>
                </tr>
            </thead>
            <tbody>
                {jobs.map((j: any) => (
                    <tr key={j.jobId || Math.random()}>
                        <td className="px-4 py-2 text-sm font-medium">{j.type}</td>
                        <td className="px-4 py-2 text-sm">
                             <Badge variant={j.status === "failed" ? "error" : j.status === "completed" ? "success" : "warning"}>
                                {j.status}
                             </Badge>
                        </td>
                         <td className="px-4 py-2 text-sm text-gray-500">
                            {j.updatedAt ? format(new Date(j.updatedAt), "MM/dd HH:mm:ss") : "-"}
                         </td>
                    </tr>
                ))}
            </tbody>
        </table>
        </Card>
      </div>
      
       <Card>
        <h2 className="text-lg font-bold text-gray-900 border-b pb-2 mb-4">Related Ops Events</h2>
        <table className="min-w-full divide-y divide-gray-200">
            <thead>
                <tr>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Time</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                    <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Message</th>
                </tr>
            </thead>
            <tbody>
                {events.map((e: any) => (
                    <tr key={e.id || Math.random()}>
                        <td className="px-4 py-2 text-sm text-gray-500">
                            {e.ts ? format(new Date(e.ts), "MM/dd HH:mm:ss") : "-"}
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
