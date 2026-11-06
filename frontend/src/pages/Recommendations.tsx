import { useState } from "react";
import { Link } from "react-router-dom";

import { RECOMMENDATION_LABELS, useDismissRecommendation, useRecommendations } from "@/api/hooks";
import type { Recommendation, RecommendationKind } from "@/api/types";
import { PageHeader } from "@/components/Layout";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Select,
  Table,
  Td,
  Th,
  Tr,
} from "@/components/ui/primitives";
import { formatGb, formatNumber, numericField, textField } from "@/lib/utils";
import { hasRole, useAuthStore } from "@/stores/auth";

const ORDER: RecommendationKind[] = [
  "idle",
  "downsize_cpu",
  "downsize_mem",
  "upsize_cpu",
  "upsize_mem",
];

function Group({
  kind,
  items,
  canAct,
  onDismiss,
  dismissing,
}: {
  kind: RecommendationKind;
  items: Recommendation[];
  canAct: boolean;
  onDismiss: (id: string) => void;
  dismissing: boolean;
}) {
  if (items.length === 0) return null;
  const reclaimsResources = kind === "idle" || kind.startsWith("downsize");

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle>
          {RECOMMENDATION_LABELS[kind]}{" "}
          <span className="ml-1 text-xs text-muted-foreground">({items.length})</span>
        </CardTitle>
        <Badge variant={reclaimsResources ? "success" : "warning"}>
          {reclaimsResources ? "frees capacity" : "needs capacity"}
        </Badge>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <thead>
            <tr>
              <Th>VM</Th>
              <Th>Cluster</Th>
              <Th>Current</Th>
              <Th>Suggested</Th>
              <Th>Evidence</Th>
              {canAct ? <Th>Actions</Th> : null}
            </tr>
          </thead>
          <tbody>
            {items.map((rec) => (
              <Tr key={rec.id}>
                <Td>
                  <Link
                    to={`/inventory/vms/${rec.vm_id}`}
                    className="font-medium text-primary hover:underline"
                  >
                    {rec.vm_name ?? "—"}
                  </Link>
                </Td>
                <Td className="text-muted-foreground">{rec.cluster_name ?? "—"}</Td>
                <Td className="tabular-nums">
                  {kind.endsWith("mem") ? formatGb(rec.current_value) : rec.current_value}
                </Td>
                <Td className="tabular-nums">
                  {kind === "idle"
                    ? "power off"
                    : kind.endsWith("mem")
                      ? formatGb(rec.suggested_value)
                      : rec.suggested_value}
                </Td>
                <Td className="max-w-md text-xs text-muted-foreground">
                  {textField(rec.evidence, "rule") ?? ""}
                  {numericField(rec.evidence, "window_days")
                    ? ` · ${numericField(rec.evidence, "window_days")} days of data`
                    : ""}
                </Td>
                {canAct ? (
                  <Td>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={dismissing}
                      onClick={() => onDismiss(rec.id)}
                    >
                      Dismiss
                    </Button>
                  </Td>
                ) : null}
              </Tr>
            ))}
          </tbody>
        </Table>
      </CardContent>
    </Card>
  );
}

export function RecommendationsPage() {
  const [kind, setKind] = useState<RecommendationKind | "">("");
  const user = useAuthStore((s) => s.user);
  const canAct = hasRole(user, "operator");

  const recommendations = useRecommendations({
    kind: kind || undefined,
    status: "open",
    limit: 200,
  });
  const dismiss = useDismissRecommendation();

  if (recommendations.isLoading) return <LoadingState label="Loading recommendations…" />;
  if (recommendations.isError)
    return <ErrorState error={recommendations.error} onRetry={() => recommendations.refetch()} />;

  const data = recommendations.data!;
  const grouped = ORDER.map((k) => ({ kind: k, items: data.items.filter((r) => r.kind === k) }));

  return (
    <>
      <PageHeader
        title="Recommendations"
        description="Rule-based right-sizing over the last 14 days of daily p95 utilisation."
        actions={
          <Select value={kind} onChange={(e) => setKind(e.target.value as RecommendationKind | "")}>
            <option value="">All kinds</option>
            {ORDER.map((k) => (
              <option key={k} value={k}>
                {RECOMMENDATION_LABELS[k]}
              </option>
            ))}
          </Select>
        }
      />

      <div className="space-y-4 p-6">
        <div className="grid gap-3 sm:grid-cols-3">
          <Card>
            <CardHeader>
              <CardTitle>Open recommendations</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-semibold">{formatNumber(data.total)}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Reclaimable vCPU</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-semibold text-success">
                {formatNumber(data.total_reclaimable_vcpus)}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Reclaimable memory</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-semibold text-success">
                {formatGb(data.total_reclaimable_memory_mb)}
              </p>
            </CardContent>
          </Card>
        </div>

        {data.items.length === 0 ? (
          <EmptyState
            title="No open recommendations"
            hint="Either everything is right-sized, or the right-sizing job has not run yet (`make train`)."
          />
        ) : (
          <div className="space-y-4">
            {grouped.map((group) => (
              <Group
                key={group.kind}
                kind={group.kind}
                items={group.items}
                canAct={canAct}
                dismissing={dismiss.isPending}
                onDismiss={(id) => dismiss.mutate(id)}
              />
            ))}
          </div>
        )}
      </div>
    </>
  );
}
