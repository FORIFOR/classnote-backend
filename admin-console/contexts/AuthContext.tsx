"use client";

import React, { createContext, useContext, useEffect, useState } from "react";
import { onAuthStateChanged, User, getIdTokenResult, ParsedToken } from "firebase/auth";
import { auth } from "@/lib/firebase";
import { useRouter, usePathname } from "next/navigation";

interface AuthContextType {
  user: User | null;
  loading: boolean;
  isAdmin: boolean;
  claims: ParsedToken | null;
}

const AuthContext = createContext<AuthContextType>({
  user: null,
  loading: true,
  isAdmin: false,
  claims: null,
});

export const useAuth = () => useContext(AuthContext);

export const AuthProvider = ({ children }: { children: React.ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [isAdmin, setIsAdmin] = useState(false);
  const [claims, setClaims] = useState<ParsedToken | null>(null);
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    const unsubscribe = onAuthStateChanged(auth, async (currentUser) => {
      setUser(currentUser);
      if (currentUser) {
        try {
          const tokenResult = await getIdTokenResult(currentUser, true);
          setClaims(tokenResult.claims);
          const adminCheck = !!tokenResult.claims.admin;
          setIsAdmin(adminCheck);

          // Redirect to dashboard if logged in and on login page
          if (pathname === "/login") {
             router.push("/");
          }
        } catch (e) {
          console.error("Failed to get token result:", e);
          setIsAdmin(false);
        }
      } else {
        setClaims(null);
        setIsAdmin(false);
        // Redirect to login if not logged in and strictly on protected pages (simple check)
        if (pathname !== "/login") {
            router.push("/login");
        }
      }
      setLoading(false);
    });

    return () => unsubscribe();
  }, [pathname, router]);

  return (
    <AuthContext.Provider value={{ user, loading, isAdmin, claims }}>
      {!loading && children}
    </AuthContext.Provider>
  );
};
