import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { RECOMMENDATION_LABELS, useForecasts, useMetrics, useVM } from "@/api/hooks";
import type { Resolution } from "@/api/types";
import { ForecastChart } from "@/components/charts/ForecastChart";
import { MetricChart } from "@/components/charts/MetricChart";
import { PageHeader } from "@/components/Layout";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/primitives";
import { formatDate, formatGb, formatPct, relativeTime, textField } from "@/lib/utils";

const RANGES: { label: string; days: number; resolution: Resolution }[] = [
  { label: "24h", days: 1, resolution: "raw" },
  { label: "7d", days: 7, resolution: "hourly" },
  { label: "30d", days: 30, resolution: "daily" },
  { label: "180d", days: 180, resolution: "daily" },
];

function Spec({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="text-sm font-medium">{value}</p>
    </div>
  );
}

export function VMDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [rangeIndex, setRangeIndex] = useState(1);
  const range = RANGES[rangeIndex]!;

  const detail = useVM(id);
  const metrics = useMetrics("vm", id, range.days, range.resolution);
  const forecasts = useForecasts("vm", id);

  if (detail.isLoading) return <LoadingState label="Loading VM…" />;
  if (detail.isError) return <ErrorState error={detail.error} onRetry={() => detail.refetch()} />;

  const { vm, latest_metrics, open_anomalies, open_recommendations } = detail.data!;
  const points = metrics.data?.points ?? [];
  const cpuForecasts = forecasts.data?.forecasts.filter((f) => f.metric.startsWith("cpu")) ?? [];
  const memForecasts = forecasts.data?.forecasts.filter((f) => f.metric.startsWith("mem")) ?? [];

  return (
    <>
      <PageHeader
        title={vm.name}
        description={[vm.guest_os, vm.cluster_name, vm.host_name].filter(Boolean).join(" · ")}
        actions={
          <>
            <Badge variant={vm.source_type === "vsphere" ? "default" : "secondary"}>
              {vm.source_type}
            </Badge>
            <Badge variant={vm.power_state === "on" ? "success" : "outline"}>{vm.power_state}</Badge>
            {vm.is_deleted ? <Badge variant="destructive">removed from source</Badge> : null}
          </>
        }
      />

      <div className="space-y-6 p-6">
        <Card>
          <CardContent className="grid grid-cols-2 gap-4 p-4 md:grid-cols-4 lg:grid-cols-7">
            <Spec label="vCPUs" value={String(vm.vcpus)} />
            <Spec label="Memory" value={formatGb(vm.memory_mb)} />
            <Spec label="Storage" value={`${vm.provisioned_storage_gb.toFixed(0)} GB`} />
            <Spec label="CPU now" value={formatPct(latest_metrics.cpu_pct)} />
            <Spec label="Memory now" value={formatPct(latest_metrics.mem_pct)} />
            <Spec label="Disk latency" value={`${(latest_metrics.disk_latency_ms ?? 0).toFixed(1)} ms`} />
            <Spec label="Last sample" value={relativeTime(latest_metrics.ts)} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle>Utilisation</CardTitle>
            <div className="flex gap-1">
              {RANGES.map((option, index) => (
                <Button
                  key={option.label}
                  size="sm"
                  variant={index === rangeIndex ? "default" : "ghost"}
                  onClick={() => setRangeIndex(index)}
                >
                  {option.label}
                </Button>
              ))}
            </div>
          </CardHeader>
          <CardContent className="space-y-6">
            {metrics.isLoading ? <LoadingState label="Loading metrics…" /> : null}
            {metrics.isError ? (
              <ErrorState error={metrics.error} onRetry={() => metrics.refetch()} />
            ) : null}
            {metrics.data && points.length === 0 ? (
              <EmptyState
                title="No metrics in this window"
                hint="Run `make seed` to backfill synthetic history, or wait for the collector's next poll."
              />
            ) : null}
            {points.length > 0 ? (
              <>
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">CPU and memory (%)</p>
                  <MetricChart
                    points={points}
                    domain={[0, 100]}
                    series={[
                      { key: "cpu_pct", label: "CPU %" },
                      { key: "mem_pct", label: "Memory %" },
                    ]}
                  />
                </div>
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">Disk</p>
                  <MetricChart
                    points={points}
                    height={160}
                    series={[
                      { key: "disk_iops", label: "IOPS" },
                      { key: "disk_latency_ms", label: "Latency (ms)" },
                    ]}
                  />
                </div>
                <div>
                  <p className="mb-1 text-xs font-medium text-muted-foreground">Network</p>
                  <MetricChart
                    points={points}
                    height={160}
                    series={[{ key: "net_kbps", label: "kbps" }]}
                  />
                </div>
              </>
            ) : null}
          </CardContent>
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>30-day CPU forecast</CardTitle>
            </CardHeader>
            <CardContent>
              {cpuForecasts.length === 0 ? (
                <EmptyState
                  title="No forecast yet"
                  hint="Forecasts regenerate nightly, or run `make train`."
                />
              ) : (
                <>
                  <ForecastChart
                    forecasts={cpuForecasts}
                    threshold={forecasts.data?.threshold_pct ?? 85}
                    preferredModel={forecasts.data?.preferred_model}
                  />
                  <ThresholdNote
                    forecasts={cpuForecasts}
                    preferred={forecasts.data?.preferred_model}
                  />
                </>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>30-day memory forecast</CardTitle>
            </CardHeader>
            <CardContent>
              {memForecasts.length === 0 ? (
                <EmptyState title="No forecast yet" />
              ) : (
                <>
                  <ForecastChart
                    forecasts={memForecasts}
                    threshold={forecasts.data?.threshold_pct ?? 85}
                    preferredModel={forecasts.data?.preferred_model}
                  />
                  <ThresholdNote
                    forecasts={memForecasts}
                    preferred={forecasts.data?.preferred_model}
                  />
                </>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Open recommendations</CardTitle>
            </CardHeader>
            <CardContent>
              {open_recommendations.length === 0 ? (
                <EmptyState title="Right-sized" hint="No changes suggested for this VM." />
              ) : (
                <ul className="space-y-2">
                  {open_recommendations.map((rec) => (
                    <li key={rec.id} className="rounded-md border border-border/60 p-3">
                      <div className="flex items-center justify-between gap-2">
                        <Badge
                          variant={rec.kind.startsWith("upsize") ? "warning" : "success"}
                        >
                          {RECOMMENDATION_LABELS[rec.kind]}
                        </Badge>
                        <span className="text-sm tabular-nums">
                          {rec.current_value} → {rec.kind === "idle" ? "power off" : rec.suggested_value}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {textField(rec.evidence, "rule") ?? ""}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Open anomalies</CardTitle>
            </CardHeader>
            <CardContent>
              {open_anomalies.length === 0 ? (
                <EmptyState title="No anomalies" hint="Nothing unusual detected recently." />
              ) : (
                <ul className="space-y-2">
                  {open_anomalies.map((anomaly) => (
                    <li key={anomaly.id} className="rounded-md border border-border/60 p-3">
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-sm font-medium">{formatDate(anomaly.window_start)}</span>
                        <span className="text-xs tabular-nums text-muted-foreground">
                          score {anomaly.score.toFixed(3)}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {anomaly.summary ?? "No summary available."}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>
        </div>

        <Link to="/inventory/vms" className="inline-block text-sm text-primary hover:underline">
          ← Back to all virtual machines
        </Link>
      </div>
    </>
  );
}

function ThresholdNote({
  forecasts,
  preferred = "naive",
}: {
  forecasts: { model: string; days_to_threshold: number | null }[];
  preferred?: "lstm" | "naive";
}) {
  const chosen =
    forecasts.find((f) => f.model === preferred) ?? forecasts[0] ?? null;
  const days = chosen?.days_to_threshold ?? null;
  const label = preferred === "lstm" ? "LSTM" : "seasonal naive";
  return (
    <p className="mt-2 text-xs text-muted-foreground">
      {days === null
        ? `Not forecast to cross the threshold within 30 days (${label}).`
        : `Crosses the threshold in about ${days} day${days === 1 ? "" : "s"} (${label}).`}
    </p>
  );
}
