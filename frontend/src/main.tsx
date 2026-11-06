import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React, { useEffect } from "react";
import ReactDOM from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";

import { ApiError, apiFetch, refreshAccessToken } from "@/api/client";
import { routes } from "@/routes";
import { useAuthStore, type User } from "@/stores/auth";

import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        // 4xx will not fix itself; only retry transport-level failures.
        if (error instanceof ApiError && error.status < 500) return false;
        return failureCount < 2;
      },
    },
  },
});

const router = createBrowserRouter(routes);

/**
 * On a cold load the access token is gone (it only ever lived in memory), but
 * the httpOnly refresh cookie may still be valid - so try once before deciding
 * the user is signed out.
 */
function Bootstrap({ children }: { children: React.ReactNode }) {
  const setBootstrapping = useAuthStore((s) => s.setBootstrapping);
  const setUser = useAuthStore((s) => s.setUser);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const token = await refreshAccessToken();
      if (token) {
        try {
          const user = await apiFetch<User>("/auth/me");
          if (!cancelled) setUser(user);
        } catch {
          useAuthStore.getState().clear();
        }
      }
      if (!cancelled) setBootstrapping(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [setBootstrapping, setUser]);

  return <>{children}</>;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <Bootstrap>
        <RouterProvider router={router} />
      </Bootstrap>
    </QueryClientProvider>
  </React.StrictMode>,
);
