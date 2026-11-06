import {
  Activity,
  Boxes,
  Database,
  Gauge,
  LayoutDashboard,
  LogOut,
  MessageSquare,
  Server,
  Settings,
  Sparkles,
} from "lucide-react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { logout } from "@/api/client";
import { Badge, Button } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import { hasRole, useAuthStore, type Role } from "@/stores/auth";

interface NavItem {
  to: string;
  label: string;
  icon: typeof Gauge;
  minRole?: Role;
  end?: boolean;
}

const NAV: NavItem[] = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard, end: true },
  { to: "/inventory/vms", label: "Virtual machines", icon: Boxes },
  { to: "/inventory/hosts", label: "Hosts", icon: Server },
  { to: "/inventory/clusters", label: "Clusters", icon: Database },
  { to: "/anomalies", label: "Anomalies", icon: Activity },
  { to: "/capacity", label: "Capacity", icon: Gauge },
  { to: "/recommendations", label: "Recommendations", icon: Sparkles },
  { to: "/assistant", label: "Assistant", icon: MessageSquare },
  { to: "/settings/sources", label: "Sources", icon: Settings, minRole: "admin" },
];

export function Layout() {
  const user = useAuthStore((s) => s.user);
  const navigate = useNavigate();

  // Role-gated items are hidden here and enforced again by the API (spec 11.2).
  const items = NAV.filter((item) => !item.minRole || hasRole(user, item.minRole));

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="hidden w-60 shrink-0 flex-col border-r border-border bg-card/40 lg:flex">
        <div className="flex h-14 items-center gap-2 border-b border-border px-4">
          <div className="flex h-7 w-7 items-center justify-center rounded bg-primary text-xs font-bold text-primary-foreground">
            HV
          </div>
          <div>
            <p className="text-sm font-semibold leading-none">HVOps</p>
            <p className="text-[10px] text-muted-foreground">Hybrid virtualization ops</p>
          </div>
        </div>

        <nav className="flex-1 space-y-0.5 p-2">
          {items.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors",
                  isActive
                    ? "bg-primary/10 font-medium text-primary"
                    : "text-muted-foreground hover:bg-accent hover:text-foreground",
                )
              }
            >
              <Icon className="h-4 w-4" aria-hidden />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="border-t border-border p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <p className="truncate text-xs text-muted-foreground" title={user?.email}>
              {user?.email}
            </p>
            <Badge variant="outline">{user?.role}</Badge>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="w-full justify-start"
            onClick={async () => {
              await logout();
              navigate("/login", { replace: true });
            }}
          >
            <LogOut className="h-4 w-4" aria-hidden />
            Sign out
          </Button>
        </div>
      </aside>

      <main className="min-w-0 flex-1">
        <Outlet />
      </main>
    </div>
  );
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: React.ReactNode;
}) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-6 py-4">
      <div>
        <h1 className="text-lg font-semibold">{title}</h1>
        {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </header>
  );
}
