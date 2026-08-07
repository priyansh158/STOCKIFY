"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

interface Props {
  authenticated: boolean;
  /** When false (default true), shows ONE primary "Login to Kite" CTA at full
   * width without the logout option. */
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

  const btnBase =
    "stk-mono-tight rounded-md border text-sm transition-colors disabled:opacity-60";

  if (authenticated) {
    return (
      <button
        type="button"
        onClick={logout}
        disabled={pending === "logout"}
        className={`${btnBase} px-3 py-1.5`}
        style={{
          borderColor: "rgba(255,68,68,0.4)",
          color: "var(--color-red-soft)",
          background: "rgba(255,68,68,0.06)",
        }}
      >
        {pending === "logout" ? "Signing out…" : "Sign out"}
      </button>
    );
  }

  if (compact) {
    return (
      <button
        type="button"
        onClick={login}
        disabled={pending === "login"}
        className={`${btnBase} px-3 py-1.5`}
        style={{
          borderColor: "var(--color-green)",
          color: "var(--color-green)",
          background: "rgba(0,255,65,0.10)",
        }}
      >
        {pending === "login" ? "Redirecting…" : "Login to Kite"}
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={login}
      disabled={pending === "login"}
      className={`${btnBase} w-full px-4 py-3 text-base`}
      style={{
        borderColor: "var(--color-green)",
        color: "var(--color-green)",
        background: primary ? "rgba(0,255,65,0.12)" : "rgba(0,255,65,0.06)",
      }}
    >
      {pending === "login" ? "Redirecting to Zerodha…" : "Login to Kite"}
    </button>
  );
}
