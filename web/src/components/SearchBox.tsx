"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { SearchHit } from "@/lib/types";

const DEBOUNCE_MS = 250;
const MIN_CHARS = 2;

/**
 * Debounced instrument search.
 *
 * - Min 2 chars before hitting /api/search
 * - 250ms debounce
 * - Enter or click on a suggestion → show that instrument's details below
 *   (no navigation until the /tests page exists in milestone M3)
 */
export function SearchBox() {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<SearchHit | null>(null);
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
    if (q.length < MIN_CHARS) {
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
    setSelected(hit);
    setOpen(false);
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
      }
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  return (
    <div className="relative w-full">
      <label className="stk-mono-tight block mb-2" style={{ color: "var(--color-fg-muted)" }}>
        Look up an NSE / BSE instrument
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
          const next = e.target.value;
          setQuery(next);
          setOpen(true);
          setErr(null);
          if (next.trim().length < MIN_CHARS) {
            abortRef.current?.abort();
            if (debounceRef.current) clearTimeout(debounceRef.current);
            setHits([]);
            setLoading(false);
          }
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={onKeyDown}
        role="combobox"
        aria-autocomplete="list"
        aria-expanded={open}
        aria-controls="stock-search-results"
      />

      {open && (loading || hits.length > 0 || err) && (
        <ul
          id="stock-search-results"
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

      {selected && (
        <div
          className="mt-4 rounded-lg border p-4"
          style={{
            borderColor: "rgba(0,255,65,0.20)",
            background: "rgba(255,255,255,0.025)",
          }}
        >
          <div className="flex items-baseline justify-between gap-3">
            <span className="stk-mono" style={{ color: "var(--color-green)", fontSize: "1.05rem" }}>
              {selected.ticker}
            </span>
            <span className="stk-mono-tight" style={{ color: "var(--color-fg-muted)" }}>
              {selected.exchange} · {selected.instrument_type}
            </span>
          </div>
          <p className="mt-1" style={{ color: "var(--color-fg)" }}>{selected.name}</p>
          <p className="stk-mono-tight mt-2" style={{ color: "var(--color-fg-muted)" }}>
            {selected.instrument_token ? `token ${selected.instrument_token} · ` : ""}
            source {selected.source}
            {selected.sector ? ` · ${selected.sector}` : ""}
          </p>
        </div>
      )}
    </div>
  );
}
