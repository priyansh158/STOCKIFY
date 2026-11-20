"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { SearchHit } from "@/lib/types";

const DEBOUNCE_MS = 250;
const MIN_CHARS = 2;

interface Props {
  /** Symbols already on the list (upper case): they are marked, not offered again. */
  watched: ReadonlySet<string>;
  /** Whether the list is at its cap. */
  full: boolean;
  /** Puts the stock on the list; resolves to a plain-words problem, or null when it worked. */
  onAdd: (symbol: string) => Promise<string | null>;
}

/** The add box: type a symbol or a company name, pick an NSE share from GET /api/search. Only NSE shares are offered
 *  (the watchlist holds nothing else): indices come typed INDEX and bonds are named SGB..., and both are left out. */
export default function AddStock({ watched, full, onAdd }: Props) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const q = query.trim();
    if (q.length < MIN_CHARS) return;
    const timer = setTimeout(async () => {
      abortRef.current?.abort();
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setLoading(true);
      try {
        const data = await api<SearchHit[]>(`/api/search?q=${encodeURIComponent(q)}&limit=15`, { signal: ctrl.signal });
        setHits(data.filter((h) => h.exchange === "NSE" && h.instrument_type === "EQ" && !/^SGB/i.test(h.ticker)).slice(0, 8));
        setActiveIdx(0);
        setProblem(null);
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setProblem("Search is not answering right now. Check that the STOCKIFY API is running.");
        setHits([]);
      } finally {
        setLoading(false);
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query]);

  async function pick(hit: SearchHit) {
    if (adding || full || watched.has(hit.ticker.toUpperCase())) return;
    setAdding(true);
    setOpen(false);
    const failed = await onAdd(hit.ticker);
    setAdding(false);
    if (failed) {
      setProblem(failed);
    } else {
      setProblem(null);
      setQuery("");
      setHits([]);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open || hits.length === 0) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, hits.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (hits[activeIdx]) void pick(hits[activeIdx]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  }

  const shown = query.trim().length >= MIN_CHARS ? hits : [];

  return (
    <div className="stk-stack-s">
      <div className="stk-relative">
        <label className="stk-fieldwrap">
          <span>Add a stock</span>
          <input
            type="search"
            autoComplete="off"
            autoCorrect="off"
            autoCapitalize="characters"
            spellCheck={false}
            placeholder="A symbol or company name, such as INFY"
            className="stk-field"
            data-fig
            disabled={full}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
              setProblem(null);
              if (e.target.value.trim().length < MIN_CHARS) {
                abortRef.current?.abort();
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
            aria-controls="watch-search-results"
          />
        </label>

        {open && (loading || shown.length > 0 || query.trim().length >= MIN_CHARS) && (
          <ul id="watch-search-results" role="listbox" className="stk-results">
            {loading && <li className="stk-muted">Searching…</li>}
            {!loading && shown.length === 0 && !problem && <li className="stk-muted">No NSE share matches.</li>}
            {!loading &&
              shown.map((h, i) => {
                const already = watched.has(h.ticker.toUpperCase());
                return (
                  <li
                    key={h.ticker}
                    role="option"
                    aria-selected={i === activeIdx}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      void pick(h);
                    }}
                    onMouseEnter={() => setActiveIdx(i)}
                  >
                    <span className="stk-break">
                      <span className="stk-mono">{h.ticker}</span> {h.name}
                    </span>
                    {already && <span className="stk-label">On the list</span>}
                  </li>
                );
              })}
          </ul>
        )}
      </div>
      {full && <p className="stk-fine">The list is full. Remove a stock to add another.</p>}
      {problem && (
        <p role="alert" className="stk-warn">
          {problem}
        </p>
      )}
    </div>
  );
}
