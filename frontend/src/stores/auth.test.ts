import { describe, expect, it } from "vitest";

import { hasRole, useAuthStore, type User } from "@/stores/auth";

const user = (role: User["role"]): User => ({
  id: "1",
  email: "a@b.c",
  role,
  is_active: true,
  created_at: new Date().toISOString(),
});

describe("hasRole", () => {
  it("orders admin above operator above viewer", () => {
    expect(hasRole(user("admin"), "viewer")).toBe(true);
    expect(hasRole(user("admin"), "operator")).toBe(true);
    expect(hasRole(user("operator"), "operator")).toBe(true);
    expect(hasRole(user("operator"), "admin")).toBe(false);
    expect(hasRole(user("viewer"), "operator")).toBe(false);
  });

  it("treats a missing user as unauthorised", () => {
    expect(hasRole(null, "viewer")).toBe(false);
  });
});

describe("auth store", () => {
  it("clears both the token and the user on sign out", () => {
    useAuthStore.getState().setSession("token", user("admin"));
    useAuthStore.getState().clear();
    expect(useAuthStore.getState().accessToken).toBeNull();
    expect(useAuthStore.getState().user).toBeNull();
  });
});
