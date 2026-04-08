"use client";

import React, { createContext, useContext } from "react";

interface AuthContextType {
  user: { email: string } | null;
  loading: boolean;
  isAdmin: boolean;
  claims: null;
}

const AuthContext = createContext<AuthContextType>({
  user: { email: "public" },
  loading: false,
  isAdmin: true,
  claims: null,
});

export const useAuth = () => useContext(AuthContext);

export const AuthProvider = ({ children }: { children: React.ReactNode }) => {
  return (
    <AuthContext.Provider value={{ user: { email: "public" }, loading: false, isAdmin: true, claims: null }}>
      {children}
    </AuthContext.Provider>
  );
};
