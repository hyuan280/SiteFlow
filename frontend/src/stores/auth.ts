import { defineStore } from "pinia";

import { ApiError, apiRequest, setCsrfToken } from "@/api/client";

export interface UserView {
  username: string;
  auth_source: "env" | "bootstrap" | "db";
  can_change_password: boolean;
}

interface AuthResponse {
  user: UserView;
  csrf_token: string;
}

interface AuthState {
  user: UserView | null;
  csrfToken: string;
  initialized: boolean;
}

export const useAuthStore = defineStore("auth", {
  state: (): AuthState => ({
    user: null,
    csrfToken: "",
    initialized: false,
  }),
  getters: {
    isAuthenticated: (state) => state.user !== null,
  },
  actions: {
    apply(response: AuthResponse): void {
      this.user = response.user;
      this.csrfToken = response.csrf_token;
      setCsrfToken(response.csrf_token);
    },
    clear(): void {
      this.user = null;
      this.csrfToken = "";
      setCsrfToken("");
    },
    async login(username: string, password: string): Promise<void> {
      const response = await apiRequest<AuthResponse>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      this.apply(response);
      this.initialized = true;
    },
    async fetchMe(): Promise<void> {
      try {
        const response = await apiRequest<AuthResponse>("/auth/me");
        this.apply(response);
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          this.clear();
        } else {
          throw error;
        }
      } finally {
        this.initialized = true;
      }
    },
    async logout(): Promise<void> {
      try {
        await apiRequest("/auth/logout", { method: "POST" });
      } finally {
        this.clear();
      }
    },
    async changePassword(currentPassword: string, newPassword: string): Promise<void> {
      const response = await apiRequest<AuthResponse>("/auth/password", {
        method: "POST",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      this.apply(response);
    },
  },
});
