import { AlertTriangle, CheckCircle2, Clock } from "lucide-react";
import { Link } from "react-router-dom";

import { useAnomalies, useInventorySummary, useSources } from "@/api/hooks";
import type { ClusterAllocation } from "@/api/types";
import { PageHeader } from "@/components/Layout";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Badge, Card, CardContent, CardHeader, CardTitle, Table, Td, Th, Tr } from "@/components/ui/primitives";
import { formatGb, formatNumber, relativeTime } from "@/lib/utils";

function Kpi({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: "default" | "warning" | "success";
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{label}</CardTitle>
      </CardHeader>
      <CardContent>
        <p
          className={
            tone === "warning"
              ? "text-2xl font-semibold text-warning"
              : tone === "success"
                ? "text-2xl font-semibold text-success"
                : "text-2xl font-semibold"
          }
        >
          {value}
        </p>
        {hint ? <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p> : null}
      </CardContent>
    </Card>
  );
}

function AllocationBar({ allocation }: { allocation: ClusterAllocation }) {
  // Ratios above 1.0 are overcommit, which is normal; above 4.0 is worth a look.
  const cpuWidth = Math.min(100, (allocation.vcpu_overcommit_ratio / 4) * 100);
  const memWidth = Math.min(100, allocation.memory_overcommit_ratio * 100);
  return (
    <div className="space-y-2 rounded-md border border-border/60 p-3">
      <div className="flex items-center justify-between gap-2">
        <p className="truncate text-sm font-medium">{allocation.cluster_name}</p>
        <Badge variant="outline">{allocation.source_type}</Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        {allocation.host_count} hosts · {allocation.vm_count} VMs
      </p>
      <div className="space-y-1">
        <div className="flex justify-between text-[11px] text-muted-foreground">
          <span>vCPU : core</span>
          <span>
            {allocation.allocated_vcpus} / {allocation.physical_cores} ={" "}
            {allocation.vcpu_overcommit_ratio.toFixed(2)}×
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-muted">
          <div
            className={allocation.vcpu_overcommit_ratio > 4 ? "h-1.5 rounded-full bg-warning" : "h-1.5 rounded-full bg-primary"}
            style={{ width: `${cpuWidth}%` }}
          />
        </div>
      </div>
      <div className="space-y-1">
        <div className="flex justify-between text-[11px] text-muted-foreground">
          <span>Memory</span>
          <span>
            {formatGb(allocation.allocated_memory_mb)} / {formatGb(allocation.physical_memory_mb)} ={" "}
            {allocation.memory_overcommit_ratio.toFixed(2)}×
          </span>
        </div>
        <div className="h-1.5 rounded-full bg-muted">
          <div
            className={allocation.memory_overcommit_ratio > 1 ? "h-1.5 rounded-full bg-destructive" : "h-1.5 rounded-full bg-success"}
            style={{ width: `${memWidth}%` }}
          />
        </div>
      </div>
    </div>
  );
}

export function DashboardPage() {
  const summary = useInventorySummary();
  const sources = useSources();
  const anomalies = useAnomalies({ status: "open", limit: 6 });

  if (summary.isLoading) return <LoadingState label="Loading dashboard…" />;
  if (summary.isError) return <ErrorState error={summary.error} onRetry={() => summary.refetch()} />;

  const data = summary.data!;
  const poweredOnPct = data.vms ? (data.vms_powered_on / data.vms) * 100 : 0;

  return (
    <>
      <PageHeader
        title="Dashboard"
        description="Estate-wide inventory, capacity and health across vSphere and Nutanix."
      />

      <div className="space-y-6 p-6">
        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          <Kpi
            label="Virtual machines"
            value={formatNumber(data.vms)}
            hint={`${formatNumber(data.vms_powered_on)} powered on (${poweredOnPct.toFixed(0)}%)`}
          />
          <Kpi label="Hosts" value={formatNumber(data.hosts)} hint={`${data.clusters} clusters`} />
          <Kpi
            label="Open anomalies"
            value={formatNumber(data.open_anomalies)}
            tone={data.open_anomalies > 0 ? "warning" : "success"}
            hint={data.open_anomalies ? "Needs triage" : "Nothing outstanding"}
          />
          <Kpi
            label="Reclaimable vCPU"
            value={formatNumber(data.reclaimable_vcpus)}
            tone="success"
            hint={`${data.open_recommendations} open recommendations`}
          />
          <Kpi
            label="Reclaimable RAM"
            value={formatGb(data.reclaimable_memory_mb)}
            tone="success"
            hint="From idle and downsize recommendations"
          />
        </section>

        <section className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Cluster allocation</CardTitle>
            </CardHeader>
            <CardContent>
              {data.per_cluster.length === 0 ? (
                <EmptyState
                  title="No clusters yet"
                  hint="Add a hypervisor source and run a sync to populate inventory."
                />
              ) : (
                <div className="grid gap-3 md:grid-cols-2">
                  {data.per_cluster.map((allocation) => (
                    <AllocationBar
                      key={allocation.cluster_id ?? allocation.cluster_name}
                      allocation={allocation}
                    />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Source health</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {sources.isLoading ? <LoadingState label="Loading sources…" /> : null}
              {sources.data?.length === 0 ? (
                <EmptyState title="No sources configured" hint="Settings → Sources → Add source." />
              ) : null}
              {sources.data?.map((source) => (
                <div
                  key={source.id}
                  className="flex items-start justify-between gap-2 rounded-md border border-border/60 p-2.5"
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{source.name}</p>
                    <p className="flex items-center gap-1 text-[11px] text-muted-foreground">
                      <Clock className="h-3 w-3" aria-hidden />
                      {relativeTime(source.last_inventory_at)}
                    </p>
                    {source.last_error ? (
                      <p className="mt-1 line-clamp-2 text-[11px] text-destructive">
                        {source.last_error}
                      </p>
                    ) : null}
                  </div>
                  <Badge
                    variant={
                      source.last_status === "ok"
                        ? "success"
                        : source.last_status === "failed"
                          ? "destructive"
                          : "outline"
                    }
                  >
                    {source.last_status === "ok" ? (
                      <CheckCircle2 className="mr-1 h-3 w-3" aria-hidden />
                    ) : source.last_status === "failed" ? (
                      <AlertTriangle className="mr-1 h-3 w-3" aria-hidden />
                    ) : null}
                    {source.last_status}
                  </Badge>
                </div>
              ))}
            </CardContent>
          </Card>
        </section>

        <Card>
          <CardHeader className="flex-row items-center justify-between space-y-0">
            <CardTitle>Recent anomalies</CardTitle>
            <Link to="/anomalies" className="text-xs text-primary hover:underline">
              View all
            </Link>
          </CardHeader>
          <CardContent>
            {anomalies.isLoading ? <LoadingState /> : null}
            {anomalies.data && anomalies.data.items.length === 0 ? (
              <EmptyState
                title="No open anomalies"
                hint="Detection runs hourly. Train the model with `make train` if you have just seeded data."
              />
            ) : null}
            {anomalies.data && anomalies.data.items.length > 0 ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Entity</Th>
                    <Th>Detected</Th>
                    <Th>Score</Th>
                    <Th>Summary</Th>
                  </tr>
                </thead>
                <tbody>
                  {anomalies.data.items.map((anomaly) => (
                    <Tr key={anomaly.id}>
                      <Td className="font-medium">{anomaly.entity_name ?? "—"}</Td>
                      <Td className="text-muted-foreground">{relativeTime(anomaly.detected_at)}</Td>
                      <Td className="tabular-nums">{anomaly.score.toFixed(3)}</Td>
                      <Td className="max-w-md text-muted-foreground">
                        {anomaly.summary ?? "No summary (the assistant model was unavailable)."}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
