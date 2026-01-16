"use client";

import { usePathname } from "next/navigation";
import { Sidebar } from "@/components/Sidebar";

export function SidebarWrapper() {
  const pathname = usePathname();
  if (pathname === "/login") return null;
  return <Sidebar />;
}
