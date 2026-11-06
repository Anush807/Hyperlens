import { useState } from "react";

import { useAcknowledgeAnomaly, useAnomalies } from "@/api/hooks";
import type { AnomalyStatus } from "@/api/types";
import { PageHeader } from "@/components/Layout";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import {
  Badge,
  Button,
  Card,
  CardContent,
  Select,
  Table,
  Td,
  Th,
  Tr,
} from "@/components/ui/primitives";
import { formatDate, numericField, relativeTime } from "@/lib/utils";
import { hasRole, useAuthStore } from "@/stores/auth";

/** Count of anomalies per day, as a compact bar strip above the table. */
function Timeline({ buckets }: { buckets: { day: string; count: number }[] }) {
  const max = Math.max(1, ...buckets.map((b) => b.count));
  return (
    <div className="flex items-end gap-1" style={{ height: 56 }}>
      {buckets.map((bucket) => (
        <div key={bucket.day} className="flex flex-1 flex-col items-center gap-1" title={`${bucket.day}: ${bucket.count}`}>
          <div
            className={bucket.count ? "w-full rounded-sm bg-primary" : "w-full rounded-sm bg-muted"}
            style={{ height: `${Math.max(3, (bucket.count / max) * 44)}px` }}
          />
          <span className="text-[9px] text-muted-foreground">{bucket.day.slice(5)}</span>
        </div>
      ))}
    </div>
  );
}

export function AnomaliesPage() {
  const [status, setStatus] = useState<AnomalyStatus | "">("open");
  const [days, setDays] = useState(14);
  const user = useAuthStore((s) => s.user);
  const canAct = hasRole(user, "operator");

  const anomalies = useAnomalies({ status: status || undefined, days, limit: 100 });
  const acknowledge = useAcknowledgeAnomaly();

  const buckets = (() => {
    const map = new Map<string, number>();
    for (let i = days - 1; i >= 0; i--) {
      map.set(new Date(Date.now() - i * 86_400_000).toISOString().slice(0, 10), 0);
    }
    for (const anomaly of anomalies.data?.items ?? []) {
      const day = anomaly.detected_at.slice(0, 10);
      if (map.has(day)) map.set(day, (map.get(day) ?? 0) + 1);
    }
    return [...map.entries()].map(([day, count]) => ({ day, count }));
  })();

  return (
    <>
      <PageHeader
        title="Anomalies"
        description="Isolation Forest flags hourly windows that deviate from the same hour last week."
      />
      <div className="space-y-4 p-6">
        <div className="flex flex-wrap items-center gap-2">
          <Select value={status} onChange={(e) => setStatus(e.target.value as AnomalyStatus | "")}>
            <option value="">All statuses</option>
            <option value="open">Open</option>
            <option value="acknowledged">Acknowledged</option>
            <option value="resolved">Resolved</option>
          </Select>
          <Select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={7}>Last 7 days</option>
            <option value={14}>Last 14 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
          </Select>
        </div>

        <Card>
          <CardContent className="p-4">
            <Timeline buckets={buckets} />
          </CardContent>
        </Card>

        <div className="rounded-lg border border-border bg-card">
          {anomalies.isLoading ? <LoadingState /> : null}
          {anomalies.isError ? (
            <ErrorState error={anomalies.error} onRetry={() => anomalies.refetch()} />
          ) : null}
          {anomalies.data && anomalies.data.items.length === 0 ? (
            <EmptyState
              title="Nothing flagged"
              hint="Detection runs hourly. If you have just seeded data, run `make train` first."
            />
          ) : null}
          {anomalies.data && anomalies.data.items.length > 0 ? (
            <Table>
              <thead>
                <tr>
                  <Th>Entity</Th>
                  <Th>Window</Th>
                  <Th>Detected</Th>
                  <Th>Score</Th>
                  <Th>CPU</Th>
                  <Th>Latency</Th>
                  <Th>Summary</Th>
                  <Th>Status</Th>
                  {canAct ? <Th>Actions</Th> : null}
                </tr>
              </thead>
              <tbody>
                {anomalies.data.items.map((anomaly) => (
                  <Tr key={anomaly.id}>
                    <Td className="font-medium">{anomaly.entity_name ?? "—"}</Td>
                    <Td className="whitespace-nowrap text-xs text-muted-foreground">
                      {formatDate(anomaly.window_start)}
                    </Td>
                    <Td className="text-muted-foreground">{relativeTime(anomaly.detected_at)}</Td>
                    <Td className="tabular-nums">{anomaly.score.toFixed(3)}</Td>
                    <Td className="tabular-nums">
                      {numericField(anomaly.features, "cpu_mean")?.toFixed(1) ?? "—"}%
                    </Td>
                    <Td className="tabular-nums">
                      {numericField(anomaly.features, "lat_mean")?.toFixed(1) ?? "—"} ms
                    </Td>
                    <Td className="max-w-sm text-xs text-muted-foreground">
                      {anomaly.summary ?? "—"}
                    </Td>
                    <Td>
                      <Badge
                        variant={
                          anomaly.status === "open"
                            ? "warning"
                            : anomaly.status === "acknowledged"
                              ? "default"
                              : "success"
                        }
                      >
                        {anomaly.status}
                      </Badge>
                    </Td>
                    {canAct ? (
                      <Td>
                        <div className="flex gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={anomaly.status !== "open" || acknowledge.isPending}
                            onClick={() =>
                              acknowledge.mutate({ id: anomaly.id, status: "acknowledged" })
                            }
                          >
                            Acknowledge
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={anomaly.status === "resolved" || acknowledge.isPending}
                            onClick={() => acknowledge.mutate({ id: anomaly.id, status: "resolved" })}
                          >
                            Resolve
                          </Button>
                        </div>
                      </Td>
                    ) : null}
                  </Tr>
                ))}
              </tbody>
            </Table>
          ) : null}
        </div>
      </div>
    </>
  );
}
