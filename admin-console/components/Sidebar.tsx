"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { LayoutDashboard, AlertTriangle, Users, FileAudio, LogOut } from "lucide-react";
import { auth } from "@/lib/firebase";
import { cn } from "@/components/ui/common";

const navItems = [
  { name: "ダッシュボード", href: "/", icon: LayoutDashboard },
  { name: "アラート・ログ", href: "/events", icon: AlertTriangle },
  // { name: "ユーザー管理", href: "/users", icon: Users }, // Not implemented list yet
  // { name: "セッション管理", href: "/sessions", icon: FileAudio }, // Not implemented list yet
];

export function Sidebar() {
  const pathname = usePathname();
  const { user, isAdmin } = useAuth();

  if (!user) return null;

  return (
    <div className="flex h-screen w-64 flex-col bg-slate-900 text-white">
      <div className="flex h-16 items-center px-6 font-bold text-xl tracking-tight border-b border-slate-800">
        Classnote Admin
      </div>
      
      <div className="flex-1 overflow-y-auto py-4">
        <nav className="space-y-1 px-3">
          {navItems.map((item) => {
            const Icon = item.icon;
            const isActive = pathname === item.href;
            return (
              <Link
                key={item.name}
                href={item.href}
                className={cn(
                  "flex items-center px-3 py-2.5 text-sm font-medium rounded-md transition-colors",
                  isActive
                    ? "bg-slate-800 text-white"
                    : "text-slate-400 hover:bg-slate-800 hover:text-white"
                )}
              >
                <Icon className="mr-3 h-5 w-5" />
                {item.name}
              </Link>
            );
          })}
        </nav>
      </div>

      <div className="border-t border-slate-800 p-4">
        <div className="flex items-center">
           <div className="ml-3">
             <p className="text-sm font-medium text-white">{user.email}</p>
             <p className="text-xs text-slate-500">{isAdmin ? "Admin Access" : "Read Only"}</p>
           </div>
        </div>
        <button
          onClick={() => auth.signOut()}
          className="mt-4 flex w-full items-center justify-center rounded-md border border-slate-600 px-4 py-2 text-sm font-medium text-slate-300 hover:bg-slate-800 hover:text-white transition-colors"
        >
          <LogOut className="mr-2 h-4 w-4" />
          ログアウト
        </button>
      </div>
    </div>
  );
}
