"""Plain display text for names the code keeps as identifiers (strategy refs, order tags, run names).

One rule for every place the UI shows such a name: `humanize` splits on `_ - .` and camelCase, capitalises each word
and keeps the known acronyms upright. Strategies that declare a `DISPLAY_NAME` are shown by that; everything else
(a user's own script, an unknown tag) goes through `humanize`, so a person never sees snake_case. The raw
identifier is never changed: only what is shown.
"""
from __future__ import annotations

import re

# Words kept as written (upper case, or the one spelling given), whatever case the identifier has them in.
ACRONYMS: dict[str, str] = {
    "orb": "ORB", "sma": "SMA", "ema": "EMA", "rsi": "RSI", "macd": "MACD", "eod": "EOD", "nse": "NSE", "bse": "BSE",
    "nifty": "NIFTY", "banknifty": "BANKNIFTY", "finnifty": "FINNIFTY", "mis": "MIS", "cnc": "CNC", "nrml": "NRML",
    "sl": "SL", "slm": "SL-M", "iv": "IV", "atm": "ATM", "otm": "OTM", "itm": "ITM", "span": "SPAN", "elm": "ELM",
    "stt": "STT", "gst": "GST", "sebi": "SEBI", "vix": "VIX", "etf": "ETF", "nav": "NAV", "pnl": "P&L",
    "ltp": "LTP", "ohlc": "OHLC", "fo": "F&O", "cpp": "C++", "api": "API", "sqoff": "square-off", "rms": "RMS",
    "ist": "IST", "utc": "UTC", "dsr": "DSR", "pbo": "PBO", "cagr": "CAGR", "ce": "CE", "pe": "PE",
}
# Small words that stay lower case inside a name ("Short Straddle with Stop"), but never as its first word.
_SMALL = frozenset({"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"})

_SPLIT = re.compile(r"[_\-.\s]+")
# camelCase boundaries: "smaCross" -> sma Cross, "HTTPServer" -> HTTP Server, "run2Day" -> run2 Day
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _words(text: str) -> list[str]:
    out: list[str] = []
    for chunk in _SPLIT.split(text.strip()):
        out.extend(w for w in _CAMEL.split(chunk) if w)
    return out


def humanize(text: object) -> str:
    """"opening_range_breakout" -> "Opening Range Breakout"; "orb_cpp" -> "ORB C++"; "SmaCross" -> "SMA Cross".
    Never empty: text with no usable word comes back as it was (stripped), and a non-text value as "Unnamed"."""
    if not isinstance(text, str) or not text.strip():
        return "Unnamed"
    words = _words(text)
    if not words:
        return text.strip()
    out = []
    for i, w in enumerate(words):
        low = w.lower()
        if low in ACRONYMS:
            out.append(ACRONYMS[low])
        elif i > 0 and low in _SMALL:
            out.append(low)
        else:
            out.append(w[:1].upper() + w[1:].lower() if not w.isdigit() else w)
    return " ".join(out)


def strategy_ref_words(kind: str, ref: str) -> str:
    """A fallback name for a strategy with no `DISPLAY_NAME`: the class name of 'module:Class', or the C++ name."""
    if not isinstance(ref, str) or not ref.strip():
        return "Unnamed"
    if kind == "python" and ":" in ref:
        cls = ref.split(":", 1)[1]
        mod = ref.split(":", 1)[0].rsplit(".", 1)[-1].lstrip("_")
        return humanize(cls or mod)
    return humanize(ref)


# What the strategies' own order tags and the system's say, in plain words. A tag not here is humanized.
TAG_WORDS: dict[str, str] = {
    "entry": "Entry", "exit": "Exit", "stop": "Stop", "wing": "Protective wing", "flip": "Reversal",
    "time_exit": "Exit at the set time", "orb_long": "Long entry", "orb_short": "Short entry", "orb_exit": "Exit",
    "orb_stop": "Stop", "AUTO_SQOFF": "Automatic square-off", "RMS_SQOFF": "Forced square-off by the risk system",
}


def tag_label(tag: object) -> str:
    """An order tag in plain words ("orb_long" -> "Long entry"); "" stays ""."""
    if not isinstance(tag, str) or not tag:
        return ""
    return TAG_WORDS.get(tag) or humanize(tag)


def sentence(text: object) -> str:
    """The same words as `humanize` in sentence case ("range_high" -> "Range high", "orb_cpp" -> "ORB C++"): for labels
    that sit in running text or a legend."""
    keep = set(ACRONYMS.values())
    words = humanize(text).split(" ")
    out = [w if w in keep else w.lower() for w in words]
    if out[0] not in keep:
        out[0] = out[0][:1].upper() + out[0][1:]
    return " ".join(out)


# ── run settings in plain words (the compare page's "what was set up differently") ─────────────────────────────

# Config keys outside a strategy's own params, by their dotted path in a run config (and the manifest's field names).
CONFIG_LABELS: dict[str, str] = {
    "strategy.ref": "Strategy", "strategy.kind": "Strategy type",
    "fill_model": "How orders fill", "fill_model.kind": "How orders fill",
    "fill_model.limit_touch_policy": "Limit orders fill when the price", "fill_model.participation": "Share of volume taken",
    "fill_model.depth_take": "Share of quoted depth taken", "fill_model.slippage": "Slippage",
    "fill_model.slippage.kind": "Slippage kind", "fill_model.slippage.extra_ticks": "Extra ticks of slippage",
    "fill_model.slippage.k": "Random ticks, up to", "fill_model.slippage.x": "Slippage (basis points)",
    "fill_model.half_spread_ticks": "Half the spread (ticks)",
    "margin": "Margin", "margin.model": "How margin is worked out", "margin.live_fresh_s": "Newest live margin answer (seconds)",
    "price_basis": "Prices", "capital_paise": "Starting money", "seed": "Random seed", "latency_ms": "Order delay (ms)",
    "mark": "Prices used to value holdings", "family_id": "Research family",
    "costs.broker": "Broker", "costs.allow_unverified": "Allow charge rates not yet verified",
    "costs.scale": "Costs multiplied by", "costs.auto_square_off_fee": "Auto square-off fee",
    "costs.dp_charges": "Demat (DP) charges",
    "policies.preopen": "Pre-open orders", "policies.cas": "Closing auction orders", "policies.freeze": "Orders over the freeze limit",
    "policies.max_slices": "Most slices for a big order", "policies.rms_square_off": "Risk-system square-off",
    "config_sha256": "All settings (fingerprint)", "bundle_sha256": "Data bundle (fingerprint)",
    "packs_sha256": "Event packs (fingerprint)", "engine_build_id": "Engine build",
    "engine_build": "Engine build", "bundle_id": "Data bundle", "duration_ms": "Time taken (ms)", "python": "Python version",
    "os": "Operating system", "packs": "Event packs",
}

# What a setting's stored word means, by field. Anything else lower-case and code-like goes through `humanize`.
VALUE_WORDS: dict[str, dict[str, str]] = {
    "fill_model.kind": {"bar": "From bars", "eod_close": "At the day's close (end-of-day files)",
                        "snapshot_quote": "From quotes (ticks)", "snapshot_ltp": "From the last price (ticks, approximate)"},
    "fill_model.limit_touch_policy": {"through": "trades through the limit", "touch": "touches the limit"},
    "fill_model.slippage.kind": {"none": "None", "fixed_ticks": "A fixed number of ticks", "random_ticks": "A random number of ticks",
                                 "bps": "Basis points"},
    "margin.model": {"flat": "Flat percentage (approximate)", "spanlite": "SPAN-like estimate",
                     "span_exact": "NSE SPAN files only", "staircase": "Best available, SPAN files first",
                     "live_with_fallback": "Live from Kite, with a fallback"},
    "price_basis": {"raw": "Actual (as traded)", "adjusted": "Adjusted for splits and bonuses"},
    "mark": {"ltp": "Last traded price"},
    "policies.preopen": {"queue": "Queued for the open"}, "policies.freeze": {"slice": "Split into slices"},
    "policies.cas": {"none": "Not used"},
}
_CODE_WORD = re.compile(r"[a-z][a-z0-9_]*")


def field_label(field: str, param_labels: dict[str, str] | None = None) -> str:
    """A settings field in plain words. `strategy.params.<name>` uses the strategy's own label for the parameter
    (`param_labels`, name to label, from its params schema); a config key uses CONFIG_LABELS; any other is `sentence`."""
    if field.startswith("strategy.params."):
        name = field[len("strategy.params."):]
        return (param_labels or {}).get(name) or sentence(name)
    return CONFIG_LABELS.get(field) or sentence(field.rsplit(".", 1)[-1])


def _scalar_text(field: str, v: object) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, str):
        words = VALUE_WORDS.get(field, {})
        if v in words:
            return words[v]
        if v in ("true", "false"):
            return "Yes" if v == "true" else "No"
        if _CODE_WORD.fullmatch(v) and not v.isdigit():
            return humanize(v)
        return v
    return str(v)


def param_value_text(param: dict | None, value: object) -> str:
    """One value of a strategy setting as the form shows it, given the setting's entry in the strategy's params schema:
    a setting with `choices` shows the chosen one's label (300 as "5 minutes"), a `percent` setting shows its fraction as
    a percentage ("0.95" as "95%"), None is "none", and anything else is plain text (true/false as "Yes"/"No" is left to
    the caller's own words: a bool is "true"/"false" here, as on the sweep's axes)."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(param, dict):
        for c in param.get("choices") or []:
            if isinstance(c, dict) and (c.get("value") == value or str(c.get("value")) == str(value)) and c.get("label"):
                return str(c["label"])
        if param.get("percent"):
            from decimal import Decimal, InvalidOperation
            try:
                pct = Decimal(str(value)) * 100
            except InvalidOperation:
                return str(value)
            if pct.is_finite():
                text = format(pct.normalize(), "f")
                return f"{text}%"
    return str(value)


def setting_text(field: str, raw: object) -> str | None:
    """A stored setting's value in plain words: a JSON object becomes "Label: value" pairs (a lone `kind` is just its
    words), a code-like word is humanized, a number or a symbol stays as it is. None stays None."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw[:1] in ("{", "["):
        import json
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw
    if isinstance(raw, dict):
        if list(raw) == ["kind"]:
            return _scalar_text(f"{field}.kind", raw["kind"])
        parts = []
        for k, v in raw.items():
            sub = f"{field}.{k}"
            if k == "kind":
                parts.insert(0, _scalar_text(sub, v))
                continue
            text = setting_text(sub, v) if isinstance(v, (dict, list)) else _scalar_text(sub, v)
            parts.append(f"{CONFIG_LABELS.get(sub) or sentence(k)}: {text}")
        return "; ".join(parts)
    if isinstance(raw, list):
        return ", ".join(str(_scalar_text(field, x)) for x in raw)
    return _scalar_text(field, raw)


# ── trial families ──────────────────────────────────────────────────────────────────────────────────────────

_DATASET_WORDS = {"bars": "bars", "bhav_eod": "end-of-day files", "bhav": "end-of-day files", "ticks": "recorded ticks",
                  "live": "live prices", "packs": "saved data"}
_INTERVAL_WORDS = {"day": "daily", "minute": "minute"}


def family_title(family_id: str, description: str | None = None, job_name: str | None = None) -> str:
    """What a trial family is called on a page: its description when it has one; a sweep's or walk-forward's own name;
    else the id `<strategy>/<dataset>-<interval>-<universe>/<objective>` read as words ("SMA Cross on RELIANCE, daily
    bars, judged by Sharpe ratio"). Never the id's own punctuation."""
    if isinstance(description, str) and description.strip():
        return description.strip()
    if not isinstance(family_id, str) or not family_id.strip():
        return "Unnamed"
    parts = family_id.split("/")
    if len(parts) == 2 and parts[0] in ("sweep", "walkforward"):
        kind = "Parameter sweep" if parts[0] == "sweep" else "Walk-forward"
        return f"{kind}: {job_name.strip()}" if isinstance(job_name, str) and job_name.strip() else kind
    if len(parts) != 3:
        return humanize(family_id)
    strategy, middle, objective = parts
    from stockify.research import plain
    pieces = middle.split("-")
    data = _DATASET_WORDS.get(pieces[0], humanize(pieces[0]).lower())
    interval = _INTERVAL_WORDS.get(pieces[1], pieces[1]) if len(pieces) > 2 else None
    universe = pieces[-1] if len(pieces) > 1 else ""
    more = re.search(r"_plus(\d+)$", universe)
    if more:
        universe = universe[:more.start()]
    universe = universe.replace("_", " ").strip()
    what = f"{interval} {data}" if interval else data
    where = f"{universe}{f' and {more.group(1)} more' if more else ''}"
    judged = plain.OBJECTIVE_LABELS.get(objective, humanize(objective).lower())
    return f"{humanize(strategy)}{f' on {where}' if where else ''}, {what}, judged by {judged}"


# ── names the person chose (runs, sessions, sweeps) ──────────────────────────────────────────────────────────

def name_label(name: object) -> str:
    """A name the person chose, as words: underscores become spaces ("_-_" a spaced hyphen), known acronyms are upright
    ("orb_reliance_minute" -> "ORB reliance minute") and the first letter is capital; hyphens, dots and the case the
    person typed are kept. "" for no name. The stored name (and every id made from it) is never changed."""
    if not isinstance(name, str) or not name.strip():
        return ""
    text = " ".join(re.sub(r"_+", " ", name.replace("_-_", " - ")).split())
    words = [ACRONYMS.get(w.lower(), w) if w.isalpha() else w for w in text.split(" ")]
    out = " ".join(words)
    return out if out[0] in ACRONYMS.values() or not out[0].isalpha() else out[0].upper() + out[1:]
