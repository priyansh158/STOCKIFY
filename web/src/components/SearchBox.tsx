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
    <div className="stk-stack-s">
      <div className="stk-relative">
        <label className="stk-fieldwrap">
          <span>Look up an NSE / BSE instrument</span>
          <input
            type="search"
            inputMode="search"
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="characters"
            spellCheck={false}
            placeholder="RELIANCE, TCS, INFY…"
            className="stk-field"
            data-fig
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
        </label>

        {open && (loading || hits.length > 0 || err) && (
          <ul id="stock-search-results" role="listbox" className="stk-results">
            {loading && <li className="stk-muted">Searching…</li>}
            {err && !loading && (
              <li role="alert" className="stk-down">
                {err}
              </li>
            )}
            {!loading && !err && hits.length === 0 && query.trim().length >= MIN_CHARS && (
              <li className="stk-muted">No NSE/BSE matches.</li>
            )}
            {!loading &&
              hits.map((h, i) => (
                <li
                  key={`${h.exchange}:${h.ticker}`}
                  role="option"
                  aria-selected={i === activeIdx}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    go(h);
                  }}
                  onMouseEnter={() => setActiveIdx(i)}
                >
                  <span className="stk-break">
                    <span className="stk-mono stk-up">{h.ticker}</span> {h.name}
                  </span>
                  <span className="stk-label">{h.exchange}</span>
                </li>
              ))}
          </ul>
        )}
      </div>

      {selected && (
        <section className="stk-panel" aria-label="Selected instrument">
          <div className="stk-row-between">
            <span className="stk-mono stk-up">{selected.ticker}</span>
            <span className="stk-label">
              {selected.exchange} {selected.instrument_type}
            </span>
          </div>
          <p>{selected.name}</p>
          <p className="stk-fine stk-mono">
            {selected.instrument_token ? `token ${selected.instrument_token}, ` : ""}
            source {selected.source}
            {selected.sector ? `, ${selected.sector}` : ""}
          </p>
        </section>
      )}
    </div>
  );
}
