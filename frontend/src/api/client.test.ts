import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, login, refreshAccessToken } from "@/api/client";
import { useAuthStore } from "@/stores/auth";

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("apiFetch", () => {
  beforeEach(() => {
    useAuthStore.setState({ accessToken: "initial-token", user: null, bootstrapping: false });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    useAuthStore.getState().clear();
  });

  it("attaches the bearer token and parses JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch<{ ok: boolean }>("/vms")).resolves.toEqual({ ok: true });

    const [, init] = fetchMock.mock.calls[0]!;
    expect((init.headers as Headers).get("Authorization")).toBe("Bearer initial-token");
    expect(init.credentials).toBe("include");
  });

  it("serialises query parameters and drops empty ones", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/vms", { query: { q: "web", limit: 25, power_state: undefined, blank: "" } });

    const url = new URL(fetchMock.mock.calls[0]![0] as string);
    expect(url.searchParams.get("q")).toBe("web");
    expect(url.searchParams.get("limit")).toBe("25");
    expect(url.searchParams.has("power_state")).toBe(false);
    expect(url.searchParams.has("blank")).toBe(false);
  });

  it("refreshes once on 401 and replays the original request", async () => {
    const fetchMock = vi
      .fn()
      // 1: original request rejected
      .mockResolvedValueOnce(jsonResponse({ error: { message: "expired" } }, 401))
      // 2: refresh succeeds
      .mockResolvedValueOnce(jsonResponse({ access_token: "fresh-token" }))
      // 3: replay succeeds
      .mockResolvedValueOnce(jsonResponse({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch<{ ok: boolean }>("/vms")).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(useAuthStore.getState().accessToken).toBe("fresh-token");

    const [, replayInit] = fetchMock.mock.calls[2]!;
    expect((replayInit.headers as Headers).get("Authorization")).toBe("Bearer fresh-token");
  });

  it("clears the session and throws when the refresh also fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ error: { message: "expired" } }, 401))
      .mockResolvedValueOnce(jsonResponse({ error: { message: "nope" } }, 401));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/vms")).rejects.toBeInstanceOf(ApiError);
    expect(useAuthStore.getState().accessToken).toBeNull();
    // Exactly one refresh attempt: retrying would rotate tokens in a loop.
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry a 500", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ error: { message: "boom" } }, 500));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/vms")).rejects.toMatchObject({ status: 500 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("surfaces the API error envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ error: { code: "forbidden", message: "Requires admin role or higher" } }, 403),
      ),
    );

    await expect(apiFetch("/sources", { method: "POST", body: {} })).rejects.toMatchObject({
      status: 403,
      code: "forbidden",
      message: "Requires admin role or higher",
    });
  });

  it("returns undefined for 204 responses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 204 })));
    await expect(apiFetch("/auth/logout", { method: "POST" })).resolves.toBeUndefined();
  });
});

describe("refreshAccessToken", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    useAuthStore.getState().clear();
  });

  it("shares one in-flight refresh between concurrent callers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ access_token: "shared" }));
    vi.stubGlobal("fetch", fetchMock);

    const results = await Promise.all([
      refreshAccessToken(),
      refreshAccessToken(),
      refreshAccessToken(),
    ]);

    expect(results).toEqual(["shared", "shared", "shared"]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("returns null without throwing when the cookie is gone", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));
    await expect(refreshAccessToken()).resolves.toBeNull();
  });
});

describe("login", () => {
  afterEach(() => vi.restoreAllMocks());

  it("posts form-encoded credentials for the OAuth2 password flow", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ access_token: "a", refresh_token: "r", expires_in: 900 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await login("admin@hvops.local", "secret");

    const [, init] = fetchMock.mock.calls[0]!;
    expect(init.headers["Content-Type"]).toBe("application/x-www-form-urlencoded");
    expect(init.body).toContain("username=admin%40hvops.local");
    expect(init.body).toContain("password=secret");
  });
});
