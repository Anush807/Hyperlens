import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { LoadingState } from "@/components/States";
import { hasRole, useAuthStore, type Role } from "@/stores/auth";

export function RequireAuth({ children }: { children: ReactNode }) {
  const { accessToken, bootstrapping } = useAuthStore();
  const location = useLocation();

  // Wait for the silent refresh: redirecting first would log the user out on
  // every page reload, since the access token lives in memory only.
  if (bootstrapping) return <LoadingState label="Restoring session…" />;
  if (!accessToken) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <>{children}</>;
}

export function RequireRole({ role, children }: { role: Role; children: ReactNode }) {
  const user = useAuthStore((s) => s.user);
  if (!hasRole(user, role)) return <Navigate to="/" replace />;
  return <>{children}</>;
}
