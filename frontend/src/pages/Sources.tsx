import { RefreshCw } from "lucide-react";
import { useState } from "react";

import { useCreateSource, useSources, useSyncSource, useUpdateSource } from "@/api/hooks";
import type { SourceType } from "@/api/types";
import { PageHeader } from "@/components/Layout";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Input,
  Select,
  Table,
  Td,
  Th,
  Tr,
} from "@/components/ui/primitives";
import { relativeTime } from "@/lib/utils";

const BLANK = {
  name: "",
  type: "vsphere" as SourceType,
  endpoint: "",
  username: "",
  password: "",
  verify_ssl: false,
  is_enabled: true,
};

export function SourcesPage() {
  const sources = useSources();
  const create = useCreateSource();
  const update = useUpdateSource();
  const sync = useSyncSource();

  const [form, setForm] = useState({ ...BLANK });
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function onCreate(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setMessage(null);
    try {
      await create.mutateAsync({ ...form });
      setForm({ ...BLANK });
      setMessage(`Added ${form.name}. Trigger a sync to pull inventory.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the source");
    }
  }

  return (
    <>
      <PageHeader
        title="Hypervisor sources"
        description="Credentials are encrypted at rest and only decrypted for the collector."
      />

      <div className="space-y-6 p-6">
        <Card>
          <CardHeader>
            <CardTitle>Configured sources</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {sources.isLoading ? <LoadingState /> : null}
            {sources.isError ? (
              <ErrorState error={sources.error} onRetry={() => sources.refetch()} />
            ) : null}
            {sources.data?.length === 0 ? (
              <EmptyState
                title="No sources yet"
                hint="Add vcsim and the Nutanix mock below to populate the demo estate."
              />
            ) : null}
            {sources.data && sources.data.length > 0 ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Name</Th>
                    <Th>Type</Th>
                    <Th>Endpoint</Th>
                    <Th>Status</Th>
                    <Th>Last inventory</Th>
                    <Th>Actions</Th>
                  </tr>
                </thead>
                <tbody>
                  {sources.data.map((source) => (
                    <Tr key={source.id}>
                      <Td className="font-medium">{source.name}</Td>
                      <Td>
                        <Badge variant={source.type === "vsphere" ? "default" : "secondary"}>
                          {source.type}
                        </Badge>
                      </Td>
                      <Td className="font-mono text-xs text-muted-foreground">{source.endpoint}</Td>
                      <Td>
                        <Badge
                          variant={
                            source.last_status === "ok"
                              ? "success"
                              : source.last_status === "failed"
                                ? "destructive"
                                : "outline"
                          }
                        >
                          {source.last_status}
                        </Badge>
                        {source.last_error ? (
                          <p className="mt-1 max-w-xs text-[11px] text-destructive">
                            {source.last_error}
                          </p>
                        ) : null}
                      </Td>
                      <Td className="text-muted-foreground">
                        {relativeTime(source.last_inventory_at)}
                      </Td>
                      <Td>
                        <div className="flex gap-1">
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={sync.isPending}
                            onClick={async () => {
                              setError(null);
                              try {
                                const result = await sync.mutateAsync(source.id);
                                setMessage(result.detail);
                              } catch (err) {
                                setError(err instanceof Error ? err.message : "Sync failed");
                              }
                            }}
                          >
                            <RefreshCw className="h-3 w-3" aria-hidden />
                            Sync now
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={update.isPending}
                            onClick={() =>
                              update.mutate({
                                id: source.id,
                                body: { is_enabled: !source.is_enabled },
                              })
                            }
                          >
                            {source.is_enabled ? "Disable" : "Enable"}
                          </Button>
                        </div>
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Add a source</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={onCreate} className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
              <label className="space-y-1">
                <span className="text-xs text-muted-foreground">Name</span>
                <Input
                  required
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="vcsim-lab"
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-muted-foreground">Type</span>
                <Select
                  className="w-full"
                  value={form.type}
                  onChange={(e) => setForm({ ...form, type: e.target.value as SourceType })}
                >
                  <option value="vsphere">vSphere</option>
                  <option value="nutanix">Nutanix</option>
                </Select>
              </label>
              <label className="space-y-1">
                <span className="text-xs text-muted-foreground">Endpoint</span>
                <Input
                  required
                  value={form.endpoint}
                  onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
                  placeholder={
                    form.type === "vsphere" ? "vcsim:8989" : "http://nutanix-mock:9440"
                  }
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-muted-foreground">Username</span>
                <Input
                  required
                  value={form.username}
                  onChange={(e) => setForm({ ...form, username: e.target.value })}
                />
              </label>
              <label className="space-y-1">
                <span className="text-xs text-muted-foreground">Password</span>
                <Input
                  required
                  type="password"
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                />
              </label>
              <div className="flex items-end gap-3">
                <label className="flex items-center gap-2 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={form.verify_ssl}
                    onChange={(e) => setForm({ ...form, verify_ssl: e.target.checked })}
                  />
                  Verify TLS
                </label>
                <Button type="submit" disabled={create.isPending}>
                  {create.isPending ? "Adding…" : "Add source"}
                </Button>
              </div>
            </form>

            {message ? <p className="mt-3 text-xs text-success">{message}</p> : null}
            {error ? (
              <p className="mt-3 text-xs text-destructive" role="alert">
                {error}
              </p>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
