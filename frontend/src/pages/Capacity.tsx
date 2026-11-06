import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { useClusterCapacity, useClusters, useDatastoreCapacity, useDatastores } from "@/api/hooks";
import { ForecastChart } from "@/components/charts/ForecastChart";
import { PageHeader } from "@/components/Layout";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import {
  Badge,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Table,
  Td,
  Th,
  Tr,
} from "@/components/ui/primitives";
import { formatGb, formatNumber } from "@/lib/utils";

function DaysBadge({ days }: { days: number | null | undefined }) {
  if (days === null || days === undefined) return <Badge variant="success">&gt; 30 days</Badge>;
  if (days <= 7) return <Badge variant="destructive">{days} days</Badge>;
  if (days <= 21) return <Badge variant="warning">{days} days</Badge>;
  return <Badge variant="outline">{days} days</Badge>;
}

export function CapacityPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const clusters = useClusters({ limit: 100 });
  const datastores = useDatastores({ limit: 100 });

  const selectedCluster = searchParams.get("cluster") ?? clusters.data?.items[0]?.id;
  const [selectedDatastore, setSelectedDatastore] = useState<string | undefined>();

  const capacity = useClusterCapacity(selectedCluster);
  const datastoreCapacity = useDatastoreCapacity(
    selectedDatastore ?? datastores.data?.items[0]?.id,
  );

  // Most urgent first: that is the only ordering an operator wants here.
  const sortedDatastores = useMemo(
    () => [...(datastores.data?.items ?? [])].sort((a, b) => b.used_pct - a.used_pct),
    [datastores.data],
  );

  if (clusters.isLoading) return <LoadingState label="Loading capacity…" />;
  if (clusters.isError) return <ErrorState error={clusters.error} onRetry={() => clusters.refetch()} />;

  return (
    <>
      <PageHeader
        title="Capacity"
        description="30-day forecasts per cluster and datastore, sorted by how soon they cross the threshold."
      />

      <div className="space-y-6 p-6">
        <Card>
          <CardHeader>
            <CardTitle>Clusters</CardTitle>
          </CardHeader>
          <CardContent>
            {clusters.data?.items.length === 0 ? (
              <EmptyState title="No clusters discovered" />
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>Cluster</Th>
                    <Th>Hosts</Th>
                    <Th>VMs</Th>
                    <Th>CPU days to 85%</Th>
                    <Th>Memory days to 85%</Th>
                  </tr>
                </thead>
                <tbody>
                  {clusters.data?.items.map((cluster) => (
                    <Tr
                      key={cluster.id}
                      className={
                        cluster.id === selectedCluster ? "cursor-pointer bg-accent/50" : "cursor-pointer"
                      }
                      onClick={() => setSearchParams({ cluster: cluster.id })}
                    >
                      <Td className="font-medium">{cluster.name}</Td>
                      <Td className="tabular-nums">{cluster.host_count}</Td>
                      <Td className="tabular-nums">{cluster.vm_count}</Td>
                      <Td>
                        {cluster.id === selectedCluster ? (
                          <DaysBadge days={capacity.data?.metrics.cpu_pct_p95} />
                        ) : (
                          <span className="text-xs text-muted-foreground">select to load</span>
                        )}
                      </Td>
                      <Td>
                        {cluster.id === selectedCluster ? (
                          <DaysBadge days={capacity.data?.metrics.mem_pct_p95} />
                        ) : null}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </CardContent>
        </Card>

        {selectedCluster ? (
          <Card>
            <CardHeader>
              <CardTitle>{capacity.data?.entity_name ?? "Cluster"} — 30-day forecast</CardTitle>
            </CardHeader>
            <CardContent>
              {capacity.isLoading ? <LoadingState /> : null}
              {capacity.data && capacity.data.forecasts.length === 0 ? (
                <EmptyState
                  title="No forecast for this cluster yet"
                  hint="Forecasts need at least 14 days of daily metrics. Run `make seed` then `make train`."
                />
              ) : null}
              {capacity.data && capacity.data.forecasts.length > 0 ? (
                <>
                  <ForecastChart
                    forecasts={capacity.data.forecasts.filter((f) => f.metric.startsWith("cpu"))}
                    threshold={85}
                    preferredModel={capacity.data.preferred_model}
                  />
                  <p className="mt-2 text-xs text-muted-foreground">
                    Days-to-threshold is read from the{" "}
                    {capacity.data.preferred_model === "lstm" ? "LSTM" : "seasonal-naive"}{" "}
                    forecast, which is the model that won the held-out comparison - see{" "}
                    <code>docs/ml-evaluation.md</code>.
                  </p>
                  <div className="mt-3 grid grid-cols-2 gap-3 text-xs md:grid-cols-3">
                    <div>
                      <p className="text-muted-foreground">Physical cores</p>
                      <p className="font-medium tabular-nums">
                        {formatNumber(capacity.data.headroom.physical_cores)}
                      </p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Allocated vCPU</p>
                      <p className="font-medium tabular-nums">
                        {formatNumber(capacity.data.headroom.allocated_vcpus)} (
                        {capacity.data.headroom.vcpu_overcommit_ratio?.toFixed(2)}×)
                      </p>
                    </div>
                    <div>
                      <p className="text-muted-foreground">Allocated memory</p>
                      <p className="font-medium tabular-nums">
                        {formatGb(capacity.data.headroom.allocated_memory_mb)} (
                        {capacity.data.headroom.memory_overcommit_ratio?.toFixed(2)}×)
                      </p>
                    </div>
                  </div>
                </>
              ) : null}
            </CardContent>
          </Card>
        ) : null}

        <Card>
          <CardHeader>
            <CardTitle>Datastores</CardTitle>
          </CardHeader>
          <CardContent>
            {datastores.isLoading ? <LoadingState /> : null}
            {sortedDatastores.length === 0 ? (
              <EmptyState title="No datastores discovered" />
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>Name</Th>
                    <Th>Capacity</Th>
                    <Th>Free</Th>
                    <Th>Used</Th>
                    <Th>Days to 90%</Th>
                  </tr>
                </thead>
                <tbody>
                  {sortedDatastores.map((datastore) => (
                    <Tr
                      key={datastore.id}
                      className="cursor-pointer"
                      onClick={() => setSelectedDatastore(datastore.id)}
                    >
                      <Td className="font-medium">{datastore.name}</Td>
                      <Td className="tabular-nums">{formatNumber(datastore.capacity_gb)} GB</Td>
                      <Td className="tabular-nums">{formatNumber(datastore.free_gb)} GB</Td>
                      <Td>
                        <div className="flex items-center gap-2">
                          <div className="h-1.5 w-20 rounded-full bg-muted">
                            <div
                              className={
                                datastore.used_pct >= 90
                                  ? "h-1.5 rounded-full bg-destructive"
                                  : datastore.used_pct >= 75
                                    ? "h-1.5 rounded-full bg-warning"
                                    : "h-1.5 rounded-full bg-success"
                              }
                              style={{ width: `${Math.min(100, datastore.used_pct)}%` }}
                            />
                          </div>
                          <span className="tabular-nums">{datastore.used_pct.toFixed(1)}%</span>
                        </div>
                      </Td>
                      <Td>
                        {(selectedDatastore ?? sortedDatastores[0]?.id) === datastore.id ? (
                          <DaysBadge days={datastoreCapacity.data?.metrics.used_pct} />
                        ) : (
                          <span className="text-xs text-muted-foreground">select to load</span>
                        )}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
