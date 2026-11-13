"use client";

/**
 * The candlestick chart of a run, a walk-forward job or a paper session (CH 6.15): candles, volume, the buy and sell
 * markers (B 50, S 50, X 50), the strategy's own lines, and a card with the strategy's own note for the marker that was
 * clicked. TradingView Lightweight Charts 5.2.1; its attribution logo stays on (licence). Load it through
 * PriceChartLazy: the library needs the browser.
 *
 * Text: the note and every other string from the API reaches the page only as React text, never as markup.
 * Colours: read from the CSS tokens at mount and again when the theme changes (the library cannot read var(...)).
 * Keyboard: the chart is a canvas, so every trade is also a button in the "Trades on this chart" list; the card opens
 * from either, Escape closes it.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type SeriesMarker,
  type Time,
} from "lightweight-charts";
import {
  cardModel,
  chartViewState,
  edgeRequests,
  isDaily,
  istShift,
  markerRows,
  mergePages,
  rangeAround,
  settlementKindOf,
  shadeWindows,
  toCandle,
  toMarker,
  toPlotPoint,
  toVolume,
  type ChartBar,
  type ChartMarker,
  type ChartModel,
  type ChartOpts,
  type ChartPlot,
  type ChartWindow,
} from "@/lib/chart";
import { Loading, Panel, Skeleton } from "./ui";

export type PriceChartProps = {
  /** Fetches the chart: no range for the opening view, a range for a neighbouring page; `opts` carries the picker choices. */
  load: (range?: { from_ns: number; to_ns: number }, opts?: Pick<ChartOpts, "instrument" | "trade_date">) => Promise<ChartModel | null>;
  title: string;
  paperBadge?: boolean;
  pollMs?: number;
  windows?: ChartWindow[];
  focusMarkerId?: string;
  onMarkerClick?: (id: string) => void;
};

const PAGE_BARS = 2000;
const NEAR_BARS = 300;
const LIST_SHOWN = 50;
/** Line colours for the strategy's series, in a fixed order, each also told apart by its dash (never colour alone). */
const LINE_TOKENS = ["--color-info", "--color-warn", "--color-ink-2", "--color-accent", "--color-muted", "--color-edge", "--color-ink", "--color-focus"];
const LINE_DASH = [0, 2, 1, 3] as const; // solid, dashed, dotted, large dashed (the library's LineStyle values)
const DASH_CSS = ["solid", "dashed", "dotted", "dashed"] as const;
const NO_TOKENS = { up: "", down: "", warn: "" };
const PRICE = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** A CSS token as an rgba() string the library can parse (its parser does not know oklch): the canvas converts it. */
function readColour(name: string, alpha = 1): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  if (!ctx || !raw) return raw;
  ctx.fillStyle = raw;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

const markerKey = (m: ChartMarker) => `${m.kind}|${m.fill_id}|${m.t}|${m.qty}|${m.side}`;
function mergeMarkers(have: ChartMarker[], page: ChartMarker[]): ChartMarker[] {
  const seen = new Set(have.map(markerKey));
  const add = page.filter((m) => !seen.has(markerKey(m)));
  return add.length ? [...have, ...add].sort((a, b) => a.t - b.t) : have;
}

/** The visible range the library reports (chart time) as UTC seconds. */
function visibleSeconds(t: Time): number {
  if (typeof t === "number") return t - istShift;
  if (typeof t === "string") return Date.parse(`${t}T00:00:00Z`) / 1000 - istShift;
  return Date.UTC(t.year, t.month - 1, t.day) / 1000 - istShift;
}

function intervalWords(interval_s: number): string {
  if (isDaily(interval_s)) return "daily bars";
  return interval_s % 60 === 0 ? `${interval_s / 60}-minute bars` : `${interval_s}-second bars`;
}

export function PriceChart({ load, title, paperBadge, pollMs, windows, focusMarkerId, onMarkerClick }: PriceChartProps) {
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const shadeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const linesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const cardRef = useRef<HTMLElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const fitRef = useRef(false);

  const [model, setModel] = useState<ChartModel | null | "error">(null);
  const [problem, setProblem] = useState("");
  const [bars, setBars] = useState<ChartBar[]>([]);
  const [markers, setMarkers] = useState<ChartMarker[]>([]);
  const [instrument, setInstrument] = useState<number | undefined>(undefined);
  const [tradeDate, setTradeDate] = useState<string | undefined>(undefined);
  const [reloadKey, setReloadKey] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [revealTick, setRevealTick] = useState(0);
  const [showVolume, setShowVolume] = useState(true);
  const [showAll, setShowAll] = useState(false);
  const [themeTick, setThemeTick] = useState(0);

  // The latest values for handlers the library holds; set after render, read only from events and timers.
  const live = useRef({ load, bars, model, instrument, tradeDate, pollMs, onMarkerClick });
  useEffect(() => {
    live.current = { load, bars, model, instrument, tradeDate, pollMs, onMarkerClick };
  });
  const exhausted = useRef({ before: false, after: false });
  const paging = useRef(false);

  const meta = model !== null && model !== "error" ? model : null;
  const daily = meta ? isDaily(meta.interval_s) : false;
  const state = chartViewState(model);
  const symbol = meta?.instruments.find((i) => i.id === meta.instrument_id)?.symbol ?? "";
  const contract = settlementKindOf(symbol);

  // --- the opening load, and a new one when the instrument or the day changes (or Try again) ---------------
  useEffect(() => {
    let cancelled = false;
    live.current.load(undefined, { instrument, trade_date: tradeDate }).then(
      (res) => {
        if (cancelled) return;
        if (res === null) {
          setProblem("This chart is not available: the run or session was not found.");
          setModel("error");
          return;
        }
        exhausted.current = { before: false, after: false };
        fitRef.current = true;
        setBars(res.bars);
        setMarkers(res.markers);
        setSelected(null);
        setModel(res);
      },
      (e: unknown) => {
        if (cancelled) return;
        setProblem(e instanceof Error ? e.message : "The chart could not be loaded.");
        setModel("error");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [instrument, tradeDate, reloadKey]);

  // --- polling, paused while the tab is hidden --------------------------------------------------------------
  useEffect(() => {
    if (!pollMs) return;
    let busy = false;
    const tick = () => {
      if (document.hidden || busy) return;
      busy = true;
      const { instrument: i, tradeDate: d } = live.current;
      live.current
        .load(undefined, { instrument: i, trade_date: d })
        .then((res) => {
          if (!res || live.current.instrument !== i || live.current.tradeDate !== d) return;
          setBars((have) => mergePages(res.bars, have)); // the fresh bar wins: the last one is still forming
          setMarkers((have) => mergeMarkers(have, res.markers));
          setModel(res);
        })
        .catch(() => undefined) // keep what is shown; the next tick tries again
        .finally(() => {
          busy = false;
        });
    };
    const id = window.setInterval(tick, pollMs);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [pollMs]);

  // --- create the chart once; remove it on unmount ----------------------------------------------------------
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const chart = createChart(box, {
      autoSize: true,
      layout: { attributionLogo: true, background: { type: ColorType.Solid, color: "transparent" }, fontFamily: getComputedStyle(box).fontFamily, fontSize: 12 },
      localization: { locale: "en-IN", priceFormatter: (p: number) => PRICE.format(p) },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 4 },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.18 } },
    });
    const shade = chart.addSeries(HistogramSeries, {
      priceScaleId: "stk-shade",
      lastValueVisible: false,
      priceLineVisible: false,
      autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }),
    });
    shade.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 }, visible: false });
    const volume = chart.addSeries(HistogramSeries, {
      priceScaleId: "stk-volume",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
    const candles = chart.addSeries(CandlestickSeries, { borderVisible: false });
    chartRef.current = chart;
    candlesRef.current = candles;
    volumeRef.current = volume;
    shadeRef.current = shade;
    markersRef.current = createSeriesMarkers(candles, []);
    const lines = linesRef.current;

    chart.subscribeClick((p) => {
      const id = p.hoveredInfo?.objectId ?? p.hoveredObjectId;
      if (typeof id === "string") {
        openerRef.current = null;
        setSelected(id);
        live.current.onMarkerClick?.(id);
      }
    });
    chart.timeScale().subscribeVisibleTimeRangeChange((range) => {
      const { bars: held, model: m, pollMs: poll, load: fetchPage, instrument: i, tradeDate: d } = live.current;
      if (!range || paging.current || m === null || m === "error") return;
      const reqs = edgeRequests(
        { from: visibleSeconds(range.from), to: visibleSeconds(range.to) },
        held,
        m.interval_s,
        PAGE_BARS,
      ).filter((r) => !exhausted.current[r.side] && !(poll && r.side === "after"));
      if (reqs.length === 0) return;
      paging.current = true;
      Promise.all(
        reqs.map((r) =>
          fetchPage({ from_ns: r.from_ns, to_ns: r.to_ns }, { instrument: i, trade_date: d }).then((res) => ({ r, res })),
        ),
      )
        .then((got) => {
          if (live.current.instrument !== i || live.current.tradeDate !== d) return;
          let merged = live.current.bars;
          for (const { r, res } of got) {
            if (!res) continue;
            const next = mergePages(merged, res.bars);
            if (next.length === merged.length) exhausted.current[r.side] = true;
            merged = next;
            setMarkers((have) => mergeMarkers(have, res.markers));
          }
          setBars(merged);
        })
        .catch(() => undefined)
        .finally(() => {
          paging.current = false;
        });
    });

    const theme = () => setThemeTick((n) => n + 1);
    const watch = new MutationObserver(theme);
    watch.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme", "style"] });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", theme);
    return () => {
      watch.disconnect();
      media.removeEventListener("change", theme);
      lines.clear();
      chart.remove();
      chartRef.current = candlesRef.current = volumeRef.current = shadeRef.current = null;
      markersRef.current = null;
    };
  }, []);

  // --- colours from the tokens: at mount, and whenever the theme changes -----------------------------------------
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const ink = readColour("--color-ink-2");
    const rule = readColour("--color-rule");
    chart.applyOptions({
      layout: { textColor: ink },
      grid: { vertLines: { color: rule }, horzLines: { color: rule } },
      crosshair: { vertLine: { color: readColour("--color-edge") }, horzLine: { color: readColour("--color-edge") } },
      rightPriceScale: { borderColor: rule },
      timeScale: { borderColor: rule },
    });
    const up = readColour("--color-up");
    const down = readColour("--color-down");
    candlesRef.current?.applyOptions({ upColor: up, downColor: down, wickUpColor: up, wickDownColor: down });
    volumeRef.current?.applyOptions({ color: rule });
  }, [themeTick]);

  // --- the data on the chart ----------------------------------------------------------------------------------
  useEffect(() => {
    const chart = chartRef.current;
    const candles = candlesRef.current;
    const volume = volumeRef.current;
    const shade = shadeRef.current;
    if (!chart || !candles || !volume || !shade || !meta) return;

    candles.setData(bars.map((b) => toCandle(b, daily)) as unknown as CandlestickData[]);
    volume.setData((showVolume ? bars.map((b) => toVolume(b, daily)) : []) as unknown as HistogramData[]);
    volume.applyOptions({ color: readColour("--color-rule") });

    const tint = [readColour("--color-info", 0.1), readColour("--color-info", 0.04)];
    shade.setData(
      (windows ? shadeWindows(bars, windows, daily).map((p) => ({ time: p.time, value: 1, color: tint[p.index % 2] })) : []) as unknown as HistogramData[],
    );

    const tokens = { up: readColour("--color-up"), down: readColour("--color-down"), warn: readColour("--color-warn") };
    const points = markers.map((m, i) => toMarker(m, daily, tokens, i));
    points.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
    markersRef.current?.setMarkers(points as unknown as SeriesMarker<Time>[]);

    // The strategy's own lines: prices on the price scale, levels in a pane of their own under the candles.
    const shown = meta.plots
      .filter((p) => p.instrument_id === null || p.instrument_id === 0 || p.instrument_id === meta.instrument_id)
      .slice(0, LINE_TOKENS.length);
    const lines = linesRef.current;
    const keep = new Set(shown.map((p) => p.name));
    for (const [name, s] of lines) {
      if (!keep.has(name)) {
        chart.removeSeries(s);
        lines.delete(name);
      }
    }
    shown.forEach((p, i) => {
      let s = lines.get(p.name);
      if (!s) {
        s = chart.addSeries(LineSeries, { lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false }, p.kind === "level" ? 1 : 0);
        lines.set(p.name, s);
      }
      s.applyOptions({ color: readColour(LINE_TOKENS[i]), lineWidth: 2, lineStyle: LINE_DASH[i % LINE_DASH.length] });
      const byTime = new Map<ChartPlot["points"][number]["t"], ChartPlot["points"][number]>();
      for (const q of p.points) byTime.set(q.t, q);
      const data = [...byTime.values()].sort((a, b) => a.t - b.t).map((q) => toPlotPoint(q, daily));
      s.setData(data as unknown as LineData[]);
    });

    if (fitRef.current && bars.length > 0) {
      fitRef.current = false;
      if (bars.length > NEAR_BARS && markers.length > 0) {
        const at = bars.findIndex((b) => b.t >= markers[0].bar_t);
        const i = at < 0 ? bars.length - 1 : at;
        chart.timeScale().setVisibleLogicalRange({ from: i - NEAR_BARS / 2, to: i + NEAR_BARS / 2 });
      } else {
        chart.timeScale().fitContent();
      }
    }
  }, [bars, markers, meta, daily, showVolume, windows, themeTick]);

  // --- scroll to a marker when it is picked from the list or from outside ---------------------------------------
  const rows = useMemo(() => markerRows(markers, daily, NO_TOKENS), [markers, daily]);
  useEffect(() => {
    if (revealTick === 0 || selected === null) return;
    const chart = chartRef.current;
    const idx = rows.findIndex((r) => r.id === selected);
    if (!chart || idx < 0) return;
    const m = markers[idx];
    const at = bars.findIndex((b) => b.t === m.bar_t);
    if (at >= 0) {
      chart.timeScale().setVisibleLogicalRange({ from: at - 60, to: at + 60 });
      return;
    }
    // Not loaded yet: fetch the bars around it, then this effect runs again.
    const interval = meta?.interval_s ?? 60;
    live.current
      .load(rangeAround(m.bar_t, interval, NEAR_BARS), { instrument: live.current.instrument, trade_date: live.current.tradeDate })
      .then((res) => {
        if (res) setBars((have) => mergePages(have, res.bars));
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- re-run only when a pick or the loaded bars change
  }, [revealTick, bars]);

  const pick = useCallback((id: string, fromList: boolean) => {
    openerRef.current = fromList ? (document.activeElement as HTMLElement | null) : null;
    setSelected(id);
    setRevealTick((n) => n + 1);
    live.current.onMarkerClick?.(id);
  }, []);

  // A marker named from outside (a row in the page's trades table) opens its card; adjusted during render, once per id.
  const [seenFocus, setSeenFocus] = useState<string | undefined>(undefined);
  if (focusMarkerId !== seenFocus && (focusMarkerId === undefined || rows.some((r) => r.id === focusMarkerId))) {
    setSeenFocus(focusMarkerId);
    if (focusMarkerId !== undefined) {
      setSelected(focusMarkerId);
      setRevealTick((n) => n + 1);
    }
  }

  const close = useCallback(() => {
    setSelected(null);
    const el = openerRef.current;
    openerRef.current = null;
    if (el && el.isConnected) el.focus();
  }, []);

  useEffect(() => {
    if (selected === null) return;
    cardRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [selected, close]);

  const selIndex = selected === null ? -1 : rows.findIndex((r) => r.id === selected);
  const card = selIndex >= 0 ? cardModel(markers[selIndex], contract) : null;
  const listed = showAll ? rows : rows.slice(0, LIST_SHOWN);
  const drawable = state === "empty" || state === "full";

  return (
    <section className="stk-pricechart" aria-label={`${title}: price chart`}>
      <header className="stk-pricechart-head">
        <h3>{title}</h3>
        {paperBadge && <span className="stk-chip" data-tone="warn">Paper trading: no real orders</span>}
        {meta && (
          <div className="stk-pricechart-controls">
            {meta.instruments.length > 1 && (
              <label className="stk-pricechart-pick">
                <span>Instrument</span>
                <select className="stk-field" data-fit
                  value={meta.instrument_id ?? ""}
                  onChange={(e) => {
                    setModel(null);
                    setInstrument(Number(e.target.value));
                  }}
                >
                  {meta.instruments.map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.symbol}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {meta.days && meta.days.length > 0 && (
              <label className="stk-pricechart-pick">
                <span>Day</span>
                <select className="stk-field" data-fit value={meta.trade_date ?? ""} onChange={(e) => {
                  setModel(null);
                  setTradeDate(e.target.value);
                }}>
                  {meta.days.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label className="stk-pricechart-check">
              <input type="checkbox" className="stk-check" checked={showVolume} onChange={(e) => setShowVolume(e.target.checked)} />
              <span>Volume</span>
            </label>
          </div>
        )}
      </header>
      {meta && (
        <p className="stk-label">
          {intervalWords(meta.interval_s)}
          {meta.basis ? `, ${meta.basis} prices` : ""}
          {meta.live ? ", live" : ""}
          {meta.plain ? `. ${meta.plain}` : ""}
          {windows && windows.length > 0 ? `. Shaded bands are the test windows: ${windows.map((w) => w.label).join(", ")}` : ""}
        </p>
      )}
      {meta?.notice && (
        <p className="stk-pricechart-notice" role="note">
          {meta.notice}
        </p>
      )}

      {state === "loading" && (
        <Loading label="Loading the chart">
          <Skeleton size="l" />
        </Loading>
      )}
      {state === "error" && (
        <Panel tone="red">
          <div className="stk-pricechart-error" role="alert">
            <p>{problem || "The chart could not be loaded. Check that the API is running, then try again."}</p>
            <button type="button" className="stk-btn" onClick={() => {
                setModel(null);
                setReloadKey((n) => n + 1);
              }}>
              Try again
            </button>
          </div>
        </Panel>
      )}
      {state === "pruned" && (
        <p className="stk-pricechart-notice" role="note">
          {meta?.data_state === "no_pack"
            ? "There is no price recording for this day. The trades are listed below."
            : "The price data for this run is no longer on disk. The trades are listed below."}
        </p>
      )}
      {state === "empty" && <p className="stk-pricechart-empty">This run made no trades.</p>}

      <div hidden={!drawable} className="stk-pricechart-stage">
        <div ref={boxRef} className="stk-pricechart-canvas" />
      </div>

      {meta?.plots && drawable && meta.plots.length > 0 && (
        <ul className="stk-pricechart-legend" aria-label="The strategy's lines">
          {meta.plots.slice(0, LINE_TOKENS.length).map((p, i) => (
            <li key={p.name}>
              <span
                className="stk-pricechart-swatch"
                style={{ borderTopColor: `var(${LINE_TOKENS[i]})`, borderTopStyle: DASH_CSS[i % DASH_CSS.length] }}
                aria-hidden
              />
              {p.name}
              {p.kind === "level" ? " (own scale, lower pane)" : ""}
            </li>
          ))}
        </ul>
      )}

      {card && (
        <section ref={cardRef} tabIndex={-1} className="stk-pricechart-card" aria-label="Why the strategy traded">
          <div className="stk-pricechart-card-head">
            <h4>{card.title}</h4>
            <button type="button" className="stk-btn" data-tone="quiet" onClick={close}>
              Close
            </button>
          </div>
          <p className="stk-pricechart-reason">{card.reason}</p>
          <dl className="stk-pricechart-lines">
            {card.lines.map((l) => (
              <div key={l.label}>
                <dt>{l.label}</dt>
                <dd className="num">{l.code ? <code>{l.value}</code> : l.value}</dd>
              </div>
            ))}
            {card.pnl && (
              <div>
                <dt>{card.pnl.label}</dt>
                <dd className={`num ${card.pnl.tone === "up" ? "stk-up" : card.pnl.tone === "down" ? "stk-down" : ""}`}>{card.pnl.value}</dd>
              </div>
            )}
          </dl>
        </section>
      )}

      {(meta?.omitted_markers ?? 0) > 0 && (
        <p className="stk-label">
          {meta?.omitted_markers} more trade{meta?.omitted_markers === 1 ? " is" : "s are"} outside the bars shown; pan
          toward {meta?.omitted_markers === 1 ? "it" : "them"} to load.
        </p>
      )}

      {rows.length > 0 && (
        <div className="stk-pricechart-trades">
          <h4>Trades on this chart</h4>
          <ul>
            {listed.map((r) => (
              <li key={r.id}>
                <button type="button" aria-pressed={r.id === selected} onClick={() => pick(r.id, true)}>
                  <span className="stk-label">{r.when}</span>
                  <span className="num">{r.title}</span>
                </button>
              </li>
            ))}
          </ul>
          {rows.length > LIST_SHOWN && !showAll && (
            <button type="button" className="stk-btn" data-tone="quiet" onClick={() => setShowAll(true)}>
              Show all {rows.length} trades
            </button>
          )}
        </div>
      )}
    </section>
  );
}
