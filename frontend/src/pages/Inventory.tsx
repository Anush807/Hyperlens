import { useState } from "react";
import { Link } from "react-router-dom";

import { useClusters, useHosts, useVMs } from "@/api/hooks";
import type { PowerState } from "@/api/types";
import { PageHeader } from "@/components/Layout";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { Badge, Button, Input, Select, Table, Td, Th, Tr } from "@/components/ui/primitives";
import { formatGb, formatNumber, relativeTime } from "@/lib/utils";

const PAGE_SIZE = 25;

function PowerBadge({ state }: { state: PowerState }) {
  const variant =
    state === "on" ? "success" : state === "off" ? "outline" : state === "suspended" ? "warning" : "secondary";
  return <Badge variant={variant}>{state}</Badge>;
}

function Pagination({
  total,
  offset,
  onChange,
}: {
  total: number;
  offset: number;
  onChange: (offset: number) => void;
}) {
  const page = Math.floor(offset / PAGE_SIZE) + 1;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return (
    <div className="flex items-center justify-between border-t border-border px-3 py-2 text-xs text-muted-foreground">
      <span>
        {total === 0 ? "No results" : `${offset + 1}–${Math.min(offset + PAGE_SIZE, total)} of ${formatNumber(total)}`}
      </span>
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={offset === 0}
          onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}
        >
          Previous
        </Button>
        <span>
          Page {page} of {pages}
        </span>
        <Button
          size="sm"
          variant="outline"
          disabled={offset + PAGE_SIZE >= total}
          onClick={() => onChange(offset + PAGE_SIZE)}
        >
          Next
        </Button>
      </div>
    </div>
  );
}

export function VMsPage() {
  const [search, setSearch] = useState("");
  const [powerState, setPowerState] = useState<string>("");
  const [offset, setOffset] = useState(0);

  const query = {
    q: search || undefined,
    power_state: powerState || undefined,
    limit: PAGE_SIZE,
    offset,
  };
  const vms = useVMs(query);

  return (
    <>
      <PageHeader
        title="Virtual machines"
        description="Everything discovered across both hypervisor sources."
      />
      <div className="p-6">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <Input
            placeholder="Search by name or guest OS…"
            className="max-w-xs"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setOffset(0);
            }}
          />
          <Select
            value={powerState}
            onChange={(e) => {
              setPowerState(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">All power states</option>
            <option value="on">Powered on</option>
            <option value="off">Powered off</option>
            <option value="suspended">Suspended</option>
          </Select>
        </div>

        <div className="rounded-lg border border-border bg-card">
          {vms.isLoading ? <LoadingState label="Loading virtual machines…" /> : null}
          {vms.isError ? <ErrorState error={vms.error} onRetry={() => vms.refetch()} /> : null}
          {vms.data && vms.data.items.length === 0 ? (
            <EmptyState
              title="No virtual machines match"
              hint="Clear the filters, or run a source sync from Settings → Sources."
            />
          ) : null}
          {vms.data && vms.data.items.length > 0 ? (
            <>
              <Table>
                <thead>
                  <tr>
                    <Th>Name</Th>
                    <Th>Source</Th>
                    <Th>Cluster</Th>
                    <Th>Host</Th>
                    <Th>vCPU</Th>
                    <Th>Memory</Th>
                    <Th>Storage</Th>
                    <Th>Power</Th>
                    <Th>Last seen</Th>
                  </tr>
                </thead>
                <tbody>
                  {vms.data.items.map((vm) => (
                    <Tr key={vm.id}>
                      <Td>
                        <Link
                          to={`/inventory/vms/${vm.id}`}
                          className="font-medium text-primary hover:underline"
                        >
                          {vm.name}
                        </Link>
                        {vm.guest_os ? (
                          <p className="text-[11px] text-muted-foreground">{vm.guest_os}</p>
                        ) : null}
                      </Td>
                      <Td>
                        <Badge variant={vm.source_type === "vsphere" ? "default" : "secondary"}>
                          {vm.source_type ?? "—"}
                        </Badge>
                      </Td>
                      <Td className="text-muted-foreground">{vm.cluster_name ?? "—"}</Td>
                      <Td className="text-muted-foreground">{vm.host_name ?? "—"}</Td>
                      <Td className="tabular-nums">{vm.vcpus}</Td>
                      <Td className="tabular-nums">{formatGb(vm.memory_mb)}</Td>
                      <Td className="tabular-nums">{vm.provisioned_storage_gb.toFixed(0)} GB</Td>
                      <Td>
                        <PowerBadge state={vm.power_state} />
                      </Td>
                      <Td className="text-muted-foreground">{relativeTime(vm.last_seen_at)}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
              <Pagination total={vms.data.total} offset={offset} onChange={setOffset} />
            </>
          ) : null}
        </div>
      </div>
    </>
  );
}

export function HostsPage() {
  const [offset, setOffset] = useState(0);
  const hosts = useHosts({ limit: PAGE_SIZE, offset });

  return (
    <>
      <PageHeader title="Hosts" description="Physical capacity backing the estate." />
      <div className="p-6">
        <div className="rounded-lg border border-border bg-card">
          {hosts.isLoading ? <LoadingState /> : null}
          {hosts.isError ? <ErrorState error={hosts.error} onRetry={() => hosts.refetch()} /> : null}
          {hosts.data && hosts.data.items.length === 0 ? (
            <EmptyState title="No hosts discovered yet" hint="Run a source sync to populate." />
          ) : null}
          {hosts.data && hosts.data.items.length > 0 ? (
            <>
              <Table>
                <thead>
                  <tr>
                    <Th>Name</Th>
                    <Th>Cluster</Th>
                    <Th>Cores</Th>
                    <Th>MHz/core</Th>
                    <Th>Memory</Th>
                    <Th>VMs</Th>
                    <Th>Power</Th>
                    <Th>Last seen</Th>
                  </tr>
                </thead>
                <tbody>
                  {hosts.data.items.map((host) => (
                    <Tr key={host.id}>
                      <Td className="font-medium">{host.name}</Td>
                      <Td>
                        {host.cluster_id ? (
                          <Link
                            to={`/capacity?cluster=${host.cluster_id}`}
                            className="text-primary hover:underline"
                          >
                            {host.cluster_name}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </Td>
                      <Td className="tabular-nums">{host.cpu_cores}</Td>
                      <Td className="tabular-nums text-muted-foreground">
                        {host.cpu_mhz_per_core ?? "—"}
                      </Td>
                      <Td className="tabular-nums">{formatGb(host.memory_mb)}</Td>
                      <Td>
                        <Link
                          to={`/inventory/vms?host=${host.id}`}
                          className="text-primary hover:underline"
                        >
                          {host.vm_count}
                        </Link>
                      </Td>
                      <Td>
                        <PowerBadge state={host.power_state} />
                      </Td>
                      <Td className="text-muted-foreground">{relativeTime(host.last_seen_at)}</Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
              <Pagination total={hosts.data.total} offset={offset} onChange={setOffset} />
            </>
          ) : null}
        </div>
      </div>
    </>
  );
}

export function ClustersPage() {
  const clusters = useClusters({ limit: 100 });

  return (
    <>
      <PageHeader title="Clusters" description="Drill into capacity per cluster." />
      <div className="p-6">
        <div className="rounded-lg border border-border bg-card">
          {clusters.isLoading ? <LoadingState /> : null}
          {clusters.isError ? (
            <ErrorState error={clusters.error} onRetry={() => clusters.refetch()} />
          ) : null}
          {clusters.data && clusters.data.items.length === 0 ? (
            <EmptyState
              title="No clusters discovered"
              hint="Nutanix hosts report through a cluster reference; vSphere needs a DRS cluster."
            />
          ) : null}
          {clusters.data && clusters.data.items.length > 0 ? (
            <Table>
              <thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Hosts</Th>
                  <Th>VMs</Th>
                  <Th>Capacity</Th>
                </tr>
              </thead>
              <tbody>
                {clusters.data.items.map((cluster) => (
                  <Tr key={cluster.id}>
                    <Td className="font-medium">{cluster.name}</Td>
                    <Td className="tabular-nums">{cluster.host_count}</Td>
                    <Td className="tabular-nums">{cluster.vm_count}</Td>
                    <Td>
                      <Link
                        to={`/capacity?cluster=${cluster.id}`}
                        className="text-primary hover:underline"
                      >
                        View forecast
                      </Link>
                    </Td>
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
