/** Loading / empty / error states. Every page renders all three (spec 11.2). */
import { AlertTriangle, Inbox, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/primitives";

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div
      className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground"
      role="status"
    >
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      {label}
    </div>
  );
}

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
      <Inbox className="h-6 w-6 text-muted-foreground" aria-hidden />
      <p className="text-sm font-medium">{title}</p>
      {hint ? <p className="max-w-md text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong";
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12 text-center" role="alert">
      <AlertTriangle className="h-6 w-6 text-destructive" aria-hidden />
      <p className="text-sm font-medium">{message}</p>
      {onRetry ? (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}
