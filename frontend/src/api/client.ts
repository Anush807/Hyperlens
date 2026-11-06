import { useAuthStore } from "@/stores/auth";

export const API_URL = (import.meta.env.VITE_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export interface ApiErrorBody {
  error?: { code?: string; message?: string; details?: unknown };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: ApiErrorBody = {};
  try {
    body = (await response.json()) as ApiErrorBody;
  } catch {
    // Non-JSON error bodies (nginx, proxies) still need a usable message.
  }
  return new ApiError(
    response.status,
    body.error?.code ?? "http_error",
    body.error?.message ?? response.statusText ?? "Request failed",
    body.error?.details,
  );
}

/**
 * Refresh is shared: if three queries 401 at once they must not each rotate the
 * refresh token, because rotation invalidates the previous one and the losers
 * would log the user out.
 */
let refreshInFlight: Promise<string | null> | null = null;

export async function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const response = await fetch(`${API_URL}/auth/refresh`, {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        if (!response.ok) return null;
        const data = (await response.json()) as { access_token: string };
        useAuthStore.getState().setAccessToken(data.access_token);
        return data.access_token;
      } catch {
        return null;
      } finally {
        // Cleared as soon as the request settles. Callers that arrived while it
        // was in flight already hold this promise, so they still share the one
        // result; anyone arriving afterwards starts a fresh refresh rather than
        // being handed a stale token.
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  /** Set for /auth/login, which must not try to refresh on failure. */
  skipAuth?: boolean;
  query?: Record<string, string | number | boolean | null | undefined>;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  const url = new URL(`${API_URL}${path}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, skipAuth, query, headers, ...rest } = options;

  const send = async (token: string | null): Promise<Response> => {
    const finalHeaders = new Headers(headers);
    if (body !== undefined && !(body instanceof FormData)) {
      finalHeaders.set("Content-Type", "application/json");
    }
    if (token && !skipAuth) finalHeaders.set("Authorization", `Bearer ${token}`);

    return fetch(buildUrl(path, query), {
      ...rest,
      credentials: "include",
      headers: finalHeaders,
      body:
        body === undefined
          ? undefined
          : body instanceof FormData
            ? body
            : JSON.stringify(body),
    });
  };

  const token = useAuthStore.getState().accessToken;
  let response = await send(token);

  // Exactly one retry after a refresh (spec 11.2).
  if (response.status === 401 && !skipAuth) {
    const refreshed = await refreshAccessToken();
    if (!refreshed) {
      useAuthStore.getState().clear();
      throw await toApiError(response);
    }
    response = await send(refreshed);
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;

  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export async function login(email: string, password: string) {
  // OAuth2 password flow expects form encoding, not JSON.
  const form = new URLSearchParams({ username: email, password });
  const response = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: form.toString(),
  });
  if (!response.ok) throw await toApiError(response);
  return (await response.json()) as {
    access_token: string;
    refresh_token: string;
    expires_in: number;
  };
}

export async function logout() {
  try {
    await apiFetch<void>("/auth/logout", { method: "POST", body: {} });
  } finally {
    useAuthStore.getState().clear();
  }
}

/**
 * SSE over fetch rather than EventSource: EventSource cannot send an
 * Authorization header, and the assistant endpoint is a POST.
 */
export async function streamMessages(
  path: string,
  body: unknown,
  handlers: {
    onToken?: (chunk: string) => void;
    onTool?: (tool: { name: string; arguments: unknown; error?: string | null }) => void;
    onError?: (message: string) => void;
    onDone?: () => void;
  },
  signal?: AbortSignal,
): Promise<void> {
  const token = useAuthStore.getState().accessToken;
  const response = await fetch(buildUrl(path), {
    method: "POST",
    credentials: "include",
    signal,
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });

  if (!response.ok || !response.body) {
    const error = await toApiError(response);
    handlers.onError?.(error.message);
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line.
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      let event = "message";
      const dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
      }
      const data = dataLines.join("\n");
      if (!data) continue;

      if (event === "token") handlers.onToken?.(data);
      else if (event === "tool") {
        try {
          handlers.onTool?.(JSON.parse(data));
        } catch {
          /* a malformed tool frame should not kill the stream */
        }
      } else if (event === "error") {
        try {
          handlers.onError?.((JSON.parse(data) as { message: string }).message);
        } catch {
          handlers.onError?.(data);
        }
      } else if (event === "done") handlers.onDone?.();
    }
  }
  handlers.onDone?.();
}
