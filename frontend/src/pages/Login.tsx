import { useState } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { apiFetch, login } from "@/api/client";
import { Button, Card, CardContent, Input } from "@/components/ui/primitives";
import { useAuthStore, type User } from "@/stores/auth";

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { accessToken, setSession, bootstrapping } = useAuthStore();
  const [email, setEmail] = useState("admin@hvops.local");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (accessToken && !bootstrapping) {
    const from = (location.state as { from?: string } | null)?.from ?? "/";
    return <Navigate to={from} replace />;
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const tokens = await login(email, password);
      useAuthStore.getState().setAccessToken(tokens.access_token);
      const user = await apiFetch<User>("/auth/me");
      setSession(tokens.access_token, user);
      navigate((location.state as { from?: string } | null)?.from ?? "/", { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign in failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4">
      <Card className="w-full max-w-sm">
        <CardContent className="p-6">
          <div className="mb-6 flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded bg-primary text-sm font-bold text-primary-foreground">
              HV
            </div>
            <div>
              <h1 className="text-base font-semibold leading-none">HVOps</h1>
              <p className="text-xs text-muted-foreground">Sign in to continue</p>
            </div>
          </div>

          <form onSubmit={onSubmit} className="space-y-3">
            <div className="space-y-1">
              <label htmlFor="email" className="text-xs font-medium text-muted-foreground">
                Email
              </label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <label htmlFor="password" className="text-xs font-medium text-muted-foreground">
                Password
              </label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>

            {error ? (
              <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive" role="alert">
                {error}
              </p>
            ) : null}

            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
          </form>

          <p className="mt-4 text-center text-[11px] text-muted-foreground">
            Default seed account: admin@hvops.local / the SEED_ADMIN_PASSWORD in your .env
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
