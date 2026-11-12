"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

interface Props {
  authenticated: boolean;
  /** When false (default true), shows ONE primary "Login to Kite" CTA without the logout option. */
  compact?: boolean;
  primary?: boolean;
}

export function PortfolioActions({
  authenticated,
  compact = true,
  primary = false,
}: Props) {
  const router = useRouter();
  const [pending, setPending] = useState<"login" | "logout" | null>(null);

  async function login() {
    setPending("login");
    try {
      const { url } = await api<{ url: string }>("/api/kite/login_url");
      // Same-tab redirect so the Kite callback lands back on our origin.
      window.location.assign(url);
    } catch (e) {
      alert(`Failed to start Kite login: ${String(e)}`);
      setPending(null);
    }
  }

  async function logout() {
    if (!confirm("Sign out of Kite?")) return;
    setPending("logout");
    try {
      await api<{ authenticated: boolean }>("/api/kite/logout", { method: "POST" });
      router.refresh();
    } catch (e) {
      alert(`Logout failed: ${String(e)}`);
    } finally {
      setPending(null);
    }
  }

  if (authenticated) {
    return (
      <button type="button" onClick={logout} disabled={pending === "logout"} className="stk-btn" data-tone="danger">
        {pending === "logout" ? "Signing out…" : "Sign out"}
      </button>
    );
  }

  if (compact) {
    return (
      <button type="button" onClick={login} disabled={pending === "login"} className="stk-btn" data-tone="primary">
        {pending === "login" ? "Redirecting…" : "Login to Kite"}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={login}
      disabled={pending === "login"}
      className="stk-btn"
      data-tone={primary ? "primary" : undefined}
    >
      {pending === "login" ? "Redirecting to Zerodha…" : "Login to Kite"}
    </button>
  );
}
