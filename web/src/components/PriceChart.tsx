"use client";

/**
 * The candlestick chart of a run, a walk-forward job or a paper session (CH 6.15): candles, volume, the buy and sell
 * markers (B 50, S 50, X 50), the strategy's own lines, and a card with the strategy's own note for the marker that was
 * clicked. TradingView Lightweight Charts 5.3.0; its attribution logo stays on (licence). Load it through
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
  axisTimes,
  cardModel,
  chartSummary,
  chartViewState,
  edgeRequests,
  intervalWords,
  isDaily,
  istShift,
  markerRows,
  mergePages,
  rangeAround,
  contractOf,
  DEFAULT_INTERVAL_S,
  shadePoints,
  initialView,
  type FirstView,
  toCandle,
  toMarker,
  toPlotPoint,
  tipModel,
  toVolume,
  type ChartBar,
  type ChartMarker,
  type ChartModel,
  type ChartOpts,
  type ChartPlot,
  type ChartWindow,
  prunedWords,
  nextDelay,
} from "@/lib/chart";
import { Loading, Panel, Skeleton } from "./ui";

const toneClass = (t: "up" | "down" | "flat"): string => (t === "up" ? "stk-up" : t === "down" ? "stk-down" : "");

export type PriceChartProps = {
  /** Fetches the chart: no range for the opening view, a range for a neighbouring page; `opts` carries the picker choices. */
  load: (range?: { from_ns: number; to_ns: number }, opts?: Pick<ChartOpts, "instrument" | "trade_date" | "signal">) => Promise<ChartModel | null>;
  title: string;
  paperBadge?: boolean;
  pollMs?: number;
  windows?: ChartWindow[];
  focusMarkerId?: string;
  onMarkerClick?: (id: string) => void;
  /** The title's heading level: 3 under a page section's h2 (the default), 2 when the chart is a section of its own. */
  headingLevel?: 2 | 3;
  /** Hide the "Trades on this chart" list when the page already has its own table of the same trades. */
  hideTradesList?: boolean;
};

const PAGE_BARS = 2000;
const NEAR_BARS = 300;
const LIST_SHOWN = 50;
/** The validated three colours for the strategy's lines (dataviz validator, see the C15 report); a 4th line and beyond reuse them with a new dash. */
const LINE_TOKENS = ["--color-accent", "--color-info", "--color-warn"] as const;
const MAX_LINES = 8; // the engine records at most 8 series per run
const LINE_DASH = [0, 2, 1, 3] as const; // the library's LineStyle: solid, dashed, dotted, large dashed
const DASH_CSS = ["solid", "dashed", "dotted", "dashed"] as const;
const lineToken = (i: number) => LINE_TOKENS[i % LINE_TOKENS.length];
const lineDash = (i: number) => Math.floor(i / LINE_TOKENS.length) % LINE_DASH.length;
const PRICE = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

let scratch: CanvasRenderingContext2D | null | undefined;

/** A CSS token as an rgba() string the library can parse (its parser does not know oklch): the canvas converts it. */
function readColour(name: string, alpha = 1): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  scratch ??= document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const ctx = scratch;
  if (!ctx || !raw) return raw;
  ctx.fillStyle = raw;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

const markerKey = (m: ChartMarker) => m.id;
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

export function PriceChart({ load, title, paperBadge, pollMs, windows, focusMarkerId, onMarkerClick, headingLevel = 3, hideTradesList = false }: PriceChartProps) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const boxRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const shadeRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const axisRef = useRef<ISeriesApi<"Line"> | null>(null);
  const axisMarkersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const byTimeRef = useRef<Map<string, ChartMarker[]>>(new Map());
  const gapRef = useRef<Map<string, string>>(new Map()); // a slot shaded for a window with nothing drawn: its name, for the hover text
  const alive = useRef(true);
  const linesRef = useRef<Map<string, ISeriesApi<"Line">>>(new Map());
  const cardRef = useRef<HTMLElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const fitRef = useRef(false);
  const viewRef = useRef<FirstView | null>(null); // the first view of the data, re-applied when the box gets its width late
  const moved = useRef(false); // the user panned or zoomed since the last fit

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
  const [busy, setBusy] = useState(false);
  const [pollFailed, setPollFailed] = useState(false);
  const [lastMeta, setLastMeta] = useState<ChartModel | null>(null);
  const [tip, setTip] = useState<{ title: string; reason: string; more: number; x: number; y: number } | null>(null);

  const meta = model !== null && model !== "error" ? model : null;
  const daily = meta ? isDaily(meta.interval_s ?? DEFAULT_INTERVAL_S) : false;
  const ctrl = meta ?? lastMeta; // the pickers stay mounted while a reload is under way
  const state = busy ? "loading" : chartViewState(model);
  const picked = meta?.instruments.find((i) => i.id === meta.instrument_id);
  const symbol = picked?.symbol ?? "";
  const contract = contractOf(picked?.kind);

  // The latest values for handlers the library holds; set after render, read only from events and timers.
  const live = useRef({ load, bars, model, instrument, tradeDate, pollMs, onMarkerClick, contract: undefined as "option" | "future" | undefined });
  useEffect(() => {
    live.current = { load, bars, model, instrument, tradeDate, pollMs, onMarkerClick, contract };
  });
  const exhausted = useRef({ before: false, after: false });
  const paging = useRef(false);

  // --- the opening load, and a new one when the instrument or the day changes (or Try again) ---------------
  useEffect(() => {
    let cancelled = false;
    const ctl = new AbortController();
    live.current.load(undefined, { instrument, trade_date: tradeDate, signal: ctl.signal }).then(
      (res) => {
        if (cancelled) return;
        setBusy(false);
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
        setLastMeta(res);
      },
      (e: unknown) => {
        if (cancelled) return;
        setBusy(false);
        setProblem(e instanceof Error ? e.message : "The chart could not be loaded.");
        setModel("error");
      },
    );
    return () => {
      cancelled = true;
      ctl.abort();
    };
  }, [instrument, tradeDate, reloadKey]);

  // --- polling: one request at a time, a longer wait after each failure, paused while the tab is hidden -----
  useEffect(() => {
    if (!pollMs) return;
    let stopped = false;
    let inflight = false;
    let failures = 0;
    let timer: number | undefined;
    let ctl: AbortController | undefined;
    const schedule = () => {
      if (!stopped) timer = window.setTimeout(tick, nextDelay(pollMs, failures));
    };
    const tick = () => {
      if (stopped || inflight) return;
      if (document.hidden) return schedule();
      inflight = true;
      const { instrument: i, tradeDate: d } = live.current;
      ctl = new AbortController();
      live.current
        .load(undefined, { instrument: i, trade_date: d, signal: ctl.signal })
        .then((res) => {
          if (stopped || !alive.current || !res || live.current.instrument !== i || live.current.tradeDate !== d) return;
          failures = 0;
          setPollFailed(false);
          setBars((have) => mergePages(res.bars, have)); // the fresh bar wins: the last one is still forming
          setMarkers((have) => mergeMarkers(have, res.markers));
          setModel(res);
          setLastMeta(res);
        })
        .catch(() => {
          if (stopped) return;
          failures += 1; // keep what is shown; say so once, and try again later
          setPollFailed(true);
        })
        .finally(() => {
          inflight = false;
          schedule();
        });
    };
    const onVisible = () => {
      if (!document.hidden && !inflight) {
        window.clearTimeout(timer);
        tick();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    schedule();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      ctl?.abort();
      setPollFailed(false);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [pollMs, instrument, tradeDate]);

  // --- create the chart once; remove it on unmount ----------------------------------------------------------
  useEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const chart = createChart(box, {
      autoSize: true,
      layout: { attributionLogo: true, background: { type: ColorType.Solid, color: "transparent" }, fontFamily: getComputedStyle(box).fontFamily, fontSize: 12 },
      localization: { locale: "en-IN", priceFormatter: (p: number) => PRICE.format(p) },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 4 },
      // entireTextOnly drops a tick label that would be cut off; the 12% top margin keeps the highest bar clear of the top label.
      rightPriceScale: { scaleMargins: { top: 0.12, bottom: 0.18 }, entireTextOnly: true },
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
    // The last-price label and line are for a live chart only (see the effect below); on a finished run they sit on top of the scale's own labels.
    const candles = chart.addSeries(CandlestickSeries, { borderVisible: false, lastValueVisible: false, priceLineVisible: false });
    // With no price data the markers still need a time axis: a flat, invisible line whose only job is to hold the times.
    const axis = chart.addSeries(LineSeries, {
      priceScaleId: "stk-axis",
      color: "transparent",
      lineVisible: false,
      pointMarkersVisible: false,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    axis.priceScale().applyOptions({ visible: false });
    axisRef.current = axis;
    axisMarkersRef.current = createSeriesMarkers(axis, []);
    chartRef.current = chart;
    candlesRef.current = candles;
    volumeRef.current = volume;
    shadeRef.current = shade;
    markersRef.current = createSeriesMarkers(candles, []);
    const lines = linesRef.current;
    alive.current = true;

    chart.subscribeCrosshairMove((p) => {
      const key = p.time === undefined ? "" : String(p.time);
      const ms = byTimeRef.current.get(key);
      const gap = gapRef.current.get(key);
      if (!ms && gap && p.point) {
        setTip({ title: gap, reason: "Nothing is drawn here: no price bars or trades for this window.", more: 0, x: p.point.x, y: p.point.y });
        return;
      }
      if (!ms || !p.point) {
        setTip((t) => (t === null ? t : null));
        return;
      }
      const m = tipModel(ms, live.current.contract);
      if (m) setTip({ ...m, x: p.point.x, y: p.point.y });
    });

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
        m.interval_s ?? DEFAULT_INTERVAL_S,
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
          if (!alive.current || live.current.instrument !== i || live.current.tradeDate !== d) return;
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

    // The box can get its width after the first fit (it is hidden until data arrives) or change later: refit,
    // but only while the user has not panned or zoomed. Created after the library's own observer, so it runs after it.
    const refit = new ResizeObserver(() => {
      const v = viewRef.current;
      if (!v || moved.current) return;
      if (v.kind === "fit") chart.timeScale().fitContent();
      else chart.timeScale().setVisibleLogicalRange({ from: v.from, to: v.to });
    });
    refit.observe(box);
    const touched = () => {
      moved.current = true;
    };
    box.addEventListener("wheel", touched, { passive: true });
    box.addEventListener("pointerdown", touched);

    const theme = () => setThemeTick((n) => n + 1);
    const watch = new MutationObserver(theme);
    watch.observe(document.documentElement, { attributes: true, attributeFilter: ["class", "data-theme", "style"] });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", theme);
    return () => {
      refit.disconnect();
      box.removeEventListener("wheel", touched);
      box.removeEventListener("pointerdown", touched);
      watch.disconnect();
      media.removeEventListener("change", theme);
      alive.current = false;
      lines.clear();
      chart.remove();
      chartRef.current = candlesRef.current = volumeRef.current = shadeRef.current = null;
      markersRef.current = axisMarkersRef.current = axisRef.current = null;
    };
  }, []);

  // --- the last-price label: only while the chart is live (polling) ---------------------------------------------
  useEffect(() => {
    const on = !!pollMs;
    candlesRef.current?.applyOptions({ lastValueVisible: on, priceLineVisible: on });
  }, [pollMs]);

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
    const gapTint = readColour("--color-warn", 0.18); // a window with nothing to draw (excluded, no trades, failed)
    const shaded = windows ? shadePoints(bars, windows, daily) : [];
    gapRef.current = new Map(shaded.filter((p) => p.gap).map((p) => [String(p.time), windows?.[p.index]?.label ?? ""]));
    shade.setData(shaded.map((p) => ({ time: p.time, value: 1, color: p.gap ? gapTint : tint[p.index % 2] })) as unknown as HistogramData[]);

    const tokens = { up: readColour("--color-up"), down: readColour("--color-down"), warn: readColour("--color-warn") };
    const points = markers.map((m) => toMarker(m, daily, tokens));
    const byTime = new Map<string, ChartMarker[]>();
    points.forEach((pt, i) => byTime.set(String(pt.time), [...(byTime.get(String(pt.time)) ?? []), markers[i]]));
    byTimeRef.current = byTime;
    points.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
    const drawn = points as unknown as SeriesMarker<Time>[];
    const axis = axisRef.current;
    if (bars.length === 0 && axis) {
      // No price data (a pruned run, a day with no recording): the markers go on an empty, flat time axis.
      axis.setData(axisTimes(markers, meta.interval_s ?? DEFAULT_INTERVAL_S, daily).map((time) => ({ time, value: 0 })) as unknown as LineData[]);
      axisMarkersRef.current?.setMarkers(drawn);
      markersRef.current?.setMarkers([]);
    } else {
      axis?.setData([]);
      axisMarkersRef.current?.setMarkers([]);
      markersRef.current?.setMarkers(drawn);
    }

    // The strategy's own lines: prices on the price scale, levels in a pane of their own under the candles.
    const shown = meta.plots
      .filter((p) => p.instrument_id === null || p.instrument_id === 0 || p.instrument_id === meta.instrument_id)
      .slice(0, MAX_LINES);
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
      s.applyOptions({ color: readColour(lineToken(i)), lineWidth: 2, lineStyle: LINE_DASH[lineDash(i)] });
      const byTime = new Map<ChartPlot["points"][number]["t"], ChartPlot["points"][number]>();
      for (const q of p.points) byTime.set(q.t, q);
      const data = [...byTime.values()].sort((a, b) => a.t - b.t).map((q) => toPlotPoint(q, daily));
      s.setData(data as unknown as LineData[]);
    });

    if (fitRef.current && (bars.length > 0 || markers.length > 0)) {
      fitRef.current = false;
      moved.current = false;
      const at = bars.findIndex((b) => b.t >= (markers[0]?.bar_t ?? Infinity));
      const view = initialView(bars.length, at, markers.length, NEAR_BARS);
      viewRef.current = view;
      if (view.kind === "fit") chart.timeScale().fitContent();
      else chart.timeScale().setVisibleLogicalRange({ from: view.from, to: view.to });
    }
  }, [bars, markers, meta, daily, showVolume, windows, themeTick]);

  // --- scroll to a marker when it is picked from the list or from outside ---------------------------------------
  const rows = useMemo(() => markerRows(markers, daily), [markers, daily]);
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
  // No price data means no canvas at all: one notice says so (below), and the trades stay in the lists. Never an empty axis.
  const drawable = state === "empty" || state === "full";
  const summary = meta ? chartSummary({ ...meta, bars, markers }, symbol) : "Price chart";

  return (
    <section className="stk-pricechart" aria-label={`${title}: price chart`}>
      <header className="stk-pricechart-head">
        <Heading>{title}</Heading>
        {paperBadge && <span className="stk-chip" data-tone="warn">Paper trading: no real orders</span>}
        {meta?.live && (
          <span className="stk-chip" data-tone={pollFailed ? "warn" : "up"}>
            {pollFailed ? "Live, paused" : "Live"}
          </span>
        )}
        {ctrl && (
          <div className="stk-pricechart-controls">
            {ctrl.instruments.length > 1 && state !== "pruned" && (
              <label className="stk-pricechart-pick">
                <span>Instrument</span>
                <select className="stk-field" data-fit
                  value={instrument ?? ctrl.instrument_id ?? ""}
                  onChange={(e) => {
                    setBusy(true);
                    setInstrument(Number(e.target.value));
                  }}
                >
                  {ctrl.instruments.map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.symbol}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {ctrl.days && ctrl.days.length > 0 && (
              <label className="stk-pricechart-pick">
                <span>Day</span>
                <select className="stk-field" data-fit value={tradeDate ?? ctrl.trade_date ?? ""} onChange={(e) => {
                  setBusy(true);
                  setTradeDate(e.target.value);
                }}>
                  {ctrl.days.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {state !== "pruned" && (
              <label className="stk-pricechart-check">
                <input type="checkbox" className="stk-check" checked={showVolume} onChange={(e) => setShowVolume(e.target.checked)} />
                <span>Volume</span>
              </label>
            )}
          </div>
        )}
      </header>
      {meta && state !== "pruned" && (
        <p className="stk-label">
          {intervalWords(meta.interval_s ?? DEFAULT_INTERVAL_S)} bars
          {meta.basis ? `, ${meta.basis} prices` : ""}
          {meta.plain && !paperBadge ? `. ${meta.plain}` : ""}
          {windows && windows.length > 0 ? ". Shaded bands are the test windows, named below the chart" : ""}
        </p>
      )}
      {pollFailed && (
        <p className="stk-pricechart-notice" role="status">
          Live updates paused. Retrying…
        </p>
      )}
      {(meta?.notice || meta?.pnl_complete === false) && (
        <p className="stk-pricechart-notice" role="note">
          {meta.notice || "Some profit and loss figures are incomplete for this chart."}
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
                setBusy(true);
                setReloadKey((n) => n + 1);
              }}>
              Try again
            </button>
          </div>
        </Panel>
      )}
      {state === "pruned" && (
        <p className="stk-pricechart-notice" role="note">
          {prunedWords(meta?.data_state ?? "pruned", Boolean(paperBadge), Boolean(meta?.live))}
        </p>
      )}
      {state === "empty" && <p className="stk-pricechart-empty">{paperBadge ? "This session made no trades." : "This run made no trades."}</p>}

      <div hidden={!drawable} className="stk-pricechart-stage">
        <div ref={boxRef} className="stk-pricechart-canvas" role="img" aria-label={summary} />
        {tip && drawable && (
          <div className="stk-pricechart-tip" aria-hidden style={{ left: tip.x, top: tip.y }}>
            <strong>{tip.title}</strong>
            <span>{tip.reason}</span>
            {tip.more > 0 && <span className="stk-label">and {tip.more} more on this bar</span>}
          </div>
        )}
      </div>

      {meta?.plots && drawable && meta.plots.length > 0 && (
        <ul className="stk-pricechart-legend" aria-label="The strategy's lines">
          {meta.plots.slice(0, MAX_LINES).map((p, i) => (
            <li key={p.name}>
              <span
                className="stk-pricechart-swatch"
                style={{ borderTopColor: `var(${lineToken(i)})`, borderTopStyle: DASH_CSS[lineDash(i)] }}
                aria-hidden
              />
              {p.label || p.name}
              {p.kind === "level" ? " (drawn in a pane below)" : ""}
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
                <dd className="num" title={l.hint}>{l.code ? <code>{l.value}</code> : l.value}</dd>
              </div>
            ))}
            {card.pnl && (
              <div>
                <dt>{card.pnl.label}</dt>
                <dd className={`num ${toneClass(card.pnl.tone)}`}>{card.pnl.value}</dd>
              </div>
            )}
            {card.chunks.map((c) => (
              <div key={c.label}>
                <dt>{c.label}</dt>
                <dd className={`num ${toneClass(c.tone)}`}>{c.value}</dd>
              </div>
            ))}
          </dl>
          {markers[selIndex].kind === "settlement" && contract === "option" && (
            <p className="stk-label">Value at expiry is what the option was worth when it expired. The settlement price is the underlying&apos;s.</p>
          )}
        </section>
      )}

      {(meta?.omitted_markers ?? 0) > 0 && (
        <p className="stk-label">
          {meta?.omitted_markers} more trade{meta?.omitted_markers === 1 ? " is" : "s are"} outside the bars shown; pan
          toward {meta?.omitted_markers === 1 ? "it" : "them"} to load.
        </p>
      )}

      {rows.length > 0 && !hideTradesList && (
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
