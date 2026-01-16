import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/Providers";
import { Sidebar } from "@/components/Sidebar";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Classnote Admin Console",
  description: "Internal tools for Classnote operations",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ja">
      <body className={inter.className}>
        <Providers>
          <div className="flex h-screen bg-gray-50">
            {/* Sidebar is hidden on login page via logic inside Sidebar or we can handle it here conditionally but CSS hiding is easier or Context check */}
            <SidebarWrapper /> 
            <main className="flex-1 overflow-y-auto">
              {children}
            </main>
          </div>
        </Providers>
      </body>
    </html>
  );
}

// Helper to conditionally render sidebar based on route is hard in Server Component layout without headers hack.
// Easier: Sidebar itself returns null if no user (handled in Sidebar.tsx).
// Just wrap it.
import { SidebarWrapper } from "@/components/SidebarWrapper";