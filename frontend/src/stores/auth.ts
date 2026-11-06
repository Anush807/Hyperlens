import { create } from "zustand";

import type { Role, UserOut } from "@/api/types";

export type { Role };

/** Generated from the API's UserOut (see src/api/types.ts). */
export type User = UserOut;

interface AuthState {
  /** Access token lives in memory only - never localStorage (spec 11.2). */
  accessToken: string | null;
  user: User | null;
  /** True until the initial silent refresh has resolved one way or the other. */
  bootstrapping: boolean;
  setSession: (token: string, user: User | null) => void;
  setUser: (user: User | null) => void;
  setAccessToken: (token: string | null) => void;
  setBootstrapping: (value: boolean) => void;
  clear: () => void;
}

export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  user: null,
  bootstrapping: true,
  setSession: (accessToken, user) => set({ accessToken, user }),
  setUser: (user) => set({ user }),
  setAccessToken: (accessToken) => set({ accessToken }),
  setBootstrapping: (bootstrapping) => set({ bootstrapping }),
  clear: () => set({ accessToken: null, user: null }),
}));

const ROLE_ORDER: Role[] = ["viewer", "operator", "admin"];

export function hasRole(user: User | null, minimum: Role): boolean {
  if (!user) return false;
  return ROLE_ORDER.indexOf(user.role) >= ROLE_ORDER.indexOf(minimum);
}
