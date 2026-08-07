"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { SearchHit } from "@/lib/types";

const DEBOUNCE_MS = 250;
const MIN_CHARS = 2;

/**
 * Debounced company / ticker search.
 *
 * - Min 2 chars before hitting /api/search
 * - 250ms debounce
 * - Enter or click on a suggestion → navigate to /analyze/<ticker>
 * - Plain Enter with no selection → navigate to /analyze/<UPPERCASE_QUERY>
 *   (lets the analysis page resolve fuzzy input server-side)
 */
export function SearchBox() {
  const router = useRouter();
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const [open, setOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounced fetch on every keystroke.
  useEffect(() => {
    const q = query.trim();
    setErr(null);
    if (q.length < MIN_CHARS) {
      setHits([]);
      setLoading(false);
      return;
    }
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(async () => {
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setLoading(true);
      try {
        const data = await api<SearchHit[]>(
          `/api/search?q=${encodeURIComponent(q)}&limit=10`,
          { signal: ctrl.signal },
        );
        setHits(data);
        setActiveIdx(0);
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setErr(String(e));
        setHits([]);
      } finally {
        setLoading(false);
      }
    }, DEBOUNCE_MS);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [query]);

  function go(hit: SearchHit) {
    router.push(`/analyze/${encodeURIComponent(hit.ticker)}`);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, hits.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (hits[activeIdx]) {
        go(hits[activeIdx]);
      } else if (query.trim().length >= MIN_CHARS) {
        router.push(`/analyze/${encodeURIComponent(query.trim().toUpperCase())}`);
      }
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="relative w-full">
      <label className="stk-mono-tight block mb-2" style={{ color: "var(--color-fg-muted)" }}>
        Which company are you researching?
      </label>
      <input
        type="search"
        inputMode="search"
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="characters"
        spellCheck={false}
        placeholder="RELIANCE, TCS, INFY…"
        className="w-full rounded-lg border px-4 py-3 text-base outline-none focus:ring-2"
        style={{
          background: "var(--color-bg-soft)",
          borderColor: "rgba(0,255,65,0.25)",
          color: "var(--color-fg)",
          fontFamily: "var(--font-mono)",
          letterSpacing: "0.04em",
        }}
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={onKeyDown}
        aria-autocomplete="list"
        aria-expanded={open}
      />

      {open && (loading || hits.length > 0 || err) && (
        <ul
          role="listbox"
          className="absolute left-0 right-0 z-20 mt-1 max-h-72 overflow-auto rounded-lg border shadow-lg"
          style={{
            background: "var(--color-bg-soft)",
            borderColor: "rgba(0,255,65,0.25)",
          }}
        >
          {loading && (
            <li
              className="stk-mono-tight px-4 py-3"
              style={{ color: "var(--color-fg-muted)" }}
            >
              Searching…
            </li>
          )}
          {err && !loading && (
            <li
              className="stk-mono-tight px-4 py-3"
              style={{ color: "var(--color-red-soft)" }}
            >
              × {err}
            </li>
          )}
          {!loading && !err && hits.length === 0 && query.trim().length >= MIN_CHARS && (
            <li
              className="stk-mono-tight px-4 py-3"
              style={{ color: "var(--color-fg-muted)" }}
            >
              No NSE/BSE matches.
            </li>
          )}
          {!loading &&
            hits.map((h, i) => (
              <li
                key={`${h.exchange}:${h.ticker}`}
                role="option"
                aria-selected={i === activeIdx}
                className="flex cursor-pointer items-baseline justify-between gap-3 px-4 py-3 text-sm"
                style={{
                  background:
                    i === activeIdx ? "rgba(0,255,65,0.08)" : "transparent",
                  color: "var(--color-fg)",
                  borderTop: i === 0 ? "none" : "1px solid rgba(0,255,65,0.08)",
                }}
                onMouseDown={(e) => {
                  e.preventDefault();
                  go(h);
                }}
                onMouseEnter={() => setActiveIdx(i)}
              >
                <span className="flex-1 truncate">
                  <span
                    className="stk-mono"
                    style={{ color: "var(--color-green)" }}
                  >
                    {h.ticker}
                  </span>
                  <span className="ml-3" style={{ color: "var(--color-fg)" }}>
                    {h.name}
                  </span>
                </span>
                <span
                  className="stk-mono-tight shrink-0"
                  style={{ color: "var(--color-fg-muted)" }}
                >
                  {h.exchange}
                </span>
              </li>
            ))}
        </ul>
      )}
    </div>
  );
}
