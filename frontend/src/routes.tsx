import { Navigate, type RouteObject } from "react-router-dom";

import { Layout } from "@/components/Layout";
import { AnomaliesPage } from "@/pages/Anomalies";
import { AssistantPage } from "@/pages/Assistant";
import { CapacityPage } from "@/pages/Capacity";
import { DashboardPage } from "@/pages/Dashboard";
import { ClustersPage, HostsPage, VMsPage } from "@/pages/Inventory";
import { LoginPage } from "@/pages/Login";
import { RecommendationsPage } from "@/pages/Recommendations";
import { SourcesPage } from "@/pages/Sources";
import { VMDetailPage } from "@/pages/VMDetail";
import { RequireAuth, RequireRole } from "@/components/Guards";

export const routes: RouteObject[] = [
  { path: "/login", element: <LoginPage /> },
  {
    element: (
      <RequireAuth>
        <Layout />
      </RequireAuth>
    ),
    children: [
      { path: "/", element: <DashboardPage /> },
      { path: "/inventory/vms", element: <VMsPage /> },
      { path: "/inventory/vms/:id", element: <VMDetailPage /> },
      { path: "/inventory/hosts", element: <HostsPage /> },
      { path: "/inventory/clusters", element: <ClustersPage /> },
      { path: "/anomalies", element: <AnomaliesPage /> },
      { path: "/capacity", element: <CapacityPage /> },
      { path: "/recommendations", element: <RecommendationsPage /> },
      { path: "/assistant", element: <AssistantPage /> },
      {
        path: "/settings/sources",
        element: (
          <RequireRole role="admin">
            <SourcesPage />
          </RequireRole>
        ),
      },
      { path: "*", element: <Navigate to="/" replace /> },
    ],
  },
];
