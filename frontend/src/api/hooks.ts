import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/api/client";
import type {
  Anomaly,
  Capacity,
  Cluster,
  Conversation,
  Datastore,
  EntityForecasts,
  Host,
  InventorySummary,
  MetricSeries,
  MlStatus,
  Page,
  Recommendation,
  RecommendationKind,
  RecommendationPage,
  Resolution,
  Source,
  VM,
  VMDetail,
} from "@/api/types";
import type { User } from "@/stores/auth";

type Query = Record<string, string | number | boolean | null | undefined>;

export const keys = {
  me: ["me"] as const,
  summary: ["inventory", "summary"] as const,
  sources: ["sources"] as const,
  clusters: (q: Query) => ["clusters", q] as const,
  hosts: (q: Query) => ["hosts", q] as const,
  vms: (q: Query) => ["vms", q] as const,
  vm: (id: string) => ["vm", id] as const,
  datastores: (q: Query) => ["datastores", q] as const,
  metrics: (type: string, id: string, q: Query) => ["metrics", type, id, q] as const,
  anomalies: (q: Query) => ["anomalies", q] as const,
  forecasts: (type: string, id: string) => ["forecasts", type, id] as const,
  capacity: (type: string, id: string) => ["capacity", type, id] as const,
  recommendations: (q: Query) => ["recommendations", q] as const,
  conversations: ["conversations"] as const,
  conversation: (id: string) => ["conversation", id] as const,
  mlStatus: ["ml", "status"] as const,
};

export function useMe(enabled = true) {
  return useQuery({
    queryKey: keys.me,
    queryFn: () => apiFetch<User>("/auth/me"),
    enabled,
    retry: false,
  });
}

export function useInventorySummary() {
  return useQuery({
    queryKey: keys.summary,
    queryFn: () => apiFetch<InventorySummary>("/inventory/summary"),
    // The server caches this for 60s; polling more often just burns requests.
    refetchInterval: 60_000,
  });
}

export function useSources() {
  return useQuery({ queryKey: keys.sources, queryFn: () => apiFetch<Source[]>("/sources") });
}

export function useClusters(query: Query = {}) {
  return useQuery({
    queryKey: keys.clusters(query),
    queryFn: () => apiFetch<Page<Cluster>>("/clusters", { query }),
  });
}

export function useHosts(query: Query = {}) {
  return useQuery({
    queryKey: keys.hosts(query),
    queryFn: () => apiFetch<Page<Host>>("/hosts", { query }),
  });
}

export function useVMs(query: Query = {}) {
  return useQuery({
    queryKey: keys.vms(query),
    queryFn: () => apiFetch<Page<VM>>("/vms", { query }),
    placeholderData: (previous) => previous,
  });
}

export function useVM(id: string | undefined) {
  return useQuery({
    queryKey: keys.vm(id ?? ""),
    queryFn: () => apiFetch<VMDetail>(`/vms/${id}`),
    enabled: Boolean(id),
  });
}

export function useDatastores(query: Query = {}) {
  return useQuery({
    queryKey: keys.datastores(query),
    queryFn: () => apiFetch<Page<Datastore>>("/datastores", { query }),
  });
}

export function useMetrics(
  entityType: "vm" | "host",
  entityId: string | undefined,
  days: number,
  resolution: Resolution,
) {
  const query: Query = {
    from: new Date(Date.now() - days * 86_400_000).toISOString(),
    resolution,
  };
  return useQuery({
    queryKey: keys.metrics(entityType, entityId ?? "", query),
    queryFn: () => apiFetch<MetricSeries>(`/metrics/${entityType}/${entityId}`, { query }),
    enabled: Boolean(entityId),
  });
}

export function useAnomalies(query: Query = {}) {
  return useQuery({
    queryKey: keys.anomalies(query),
    queryFn: () => apiFetch<Page<Anomaly>>("/anomalies", { query }),
  });
}

export function useForecasts(entityType: string, entityId: string | undefined) {
  return useQuery({
    queryKey: keys.forecasts(entityType, entityId ?? ""),
    queryFn: () => apiFetch<EntityForecasts>(`/forecasts/${entityType}/${entityId}`),
    enabled: Boolean(entityId),
  });
}

export function useClusterCapacity(clusterId: string | undefined) {
  return useQuery({
    queryKey: keys.capacity("cluster", clusterId ?? ""),
    queryFn: () => apiFetch<Capacity>(`/capacity/clusters/${clusterId}`),
    enabled: Boolean(clusterId),
  });
}

export function useDatastoreCapacity(datastoreId: string | undefined) {
  return useQuery({
    queryKey: keys.capacity("datastore", datastoreId ?? ""),
    queryFn: () => apiFetch<Capacity>(`/capacity/datastores/${datastoreId}`),
    enabled: Boolean(datastoreId),
  });
}

export function useRecommendations(query: Query = {}) {
  return useQuery({
    queryKey: keys.recommendations(query),
    queryFn: () => apiFetch<RecommendationPage>("/recommendations", { query }),
  });
}

export function useMlStatus(enabled = false) {
  return useQuery({
    queryKey: keys.mlStatus,
    queryFn: () => apiFetch<MlStatus>("/ml/status"),
    enabled,
    retry: false,
  });
}

// --- Mutations ------------------------------------------------------------

export function useAcknowledgeAnomaly() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: Anomaly["status"] }) =>
      apiFetch<Anomaly>(`/anomalies/${id}`, { method: "PATCH", body: { status } }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["anomalies"] });
      client.invalidateQueries({ queryKey: keys.summary });
    },
  });
}

export function useDismissRecommendation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<Recommendation>(`/recommendations/${id}`, {
        method: "PATCH",
        body: { status: "dismissed" },
      }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["recommendations"] });
      client.invalidateQueries({ queryKey: keys.summary });
    },
  });
}

export function useCreateSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      apiFetch<Source>("/sources", { method: "POST", body }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.sources }),
  });
}

export function useUpdateSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      apiFetch<Source>(`/sources/${id}`, { method: "PATCH", body }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.sources }),
  });
}

export function useSyncSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<{ triggered: boolean; detail: string }>(`/sources/${id}/sync`, { method: "POST" }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: keys.sources });
      client.invalidateQueries({ queryKey: keys.summary });
    },
  });
}

export function useConversations() {
  return useQuery({
    queryKey: keys.conversations,
    queryFn: () => apiFetch<Conversation[]>("/assistant/conversations"),
  });
}

export function useConversation(id: string | undefined) {
  return useQuery({
    queryKey: keys.conversation(id ?? ""),
    queryFn: () => apiFetch<Conversation>(`/assistant/conversations/${id}`),
    enabled: Boolean(id),
  });
}

export function useCreateConversation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (title?: string) =>
      apiFetch<Conversation>("/assistant/conversations", {
        method: "POST",
        body: { title: title ?? null },
      }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.conversations }),
  });
}

export const RECOMMENDATION_LABELS: Record<RecommendationKind, string> = {
  idle: "Idle",
  downsize_cpu: "Downsize CPU",
  downsize_mem: "Downsize memory",
  upsize_cpu: "Upsize CPU",
  upsize_mem: "Upsize memory",
};
