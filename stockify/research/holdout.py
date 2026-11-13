"""The locked holdout (M6.6, PM 6.1, D-88): a run whose data reaches `holdout_from` is refused unless it carries a
written reason. It fails closed: when the holdout is set and a run's span cannot be worked out, the run is refused.

    holdout.enforce(cfg)         # None, or the reason when the run is over the holdout and unlocked; else raises
    holdout.enforce_bundle(cfg, bundle)    # the same check on the days a resolved bundle really holds

`research.json` `holdout_from` is `null` until the owner sets it; while it is `null` nothing here reads a pack.
Paper sessions are exempt (they trade forward, live days, and cannot look at the past by choice).
"""
from __future__ import annotations

from datetime import date

from stockify import runconfig

MIN_REASON_CHARS = 3        # letters or digits
UNLOCK_HOW = ('add "unlock_holdout": "<why you are looking at the holdout>" to the run config, or pass '
              '--unlock-holdout "<why>" to `python -m stockify run`')


class HoldoutLocked(runconfig.RunConfigError):
    """A run that reaches the locked holdout without a real reason. A RunConfigError: the CLI exits 2 and the API
    answers 422 with the message."""


def holdout_from() -> date | None:
    """`research.json` `holdout_from` as a date; None while no holdout is set. An unreadable research.json raises
    HoldoutLocked (fail closed)."""
    from stockify import research
    from stockify.config import loader
    try:
        text = research.settings()["holdout_from"]
        return None if text is None else date.fromisoformat(text)
    except (loader.ConfigError, KeyError, TypeError, ValueError, OSError) as e:
        raise HoldoutLocked("research.json could not be read, so the holdout cannot be checked: "
                            "fix the file first") from e


def _iso(text) -> date:  # noqa: ANN001
    if not isinstance(text, str):
        raise ValueError(f"{text!r} is not a date")
    d = date.fromisoformat(text)
    if d.isoformat() != text:
        raise ValueError(f"{text!r} is not YYYY-MM-DD")
    return d


def _pack_bounds(pack) -> tuple[date, date | None]:  # noqa: ANN001
    """(from, to) of one pack entry of a run config; `to` None when the spec has none. ValueError, KeyError,
    TypeError or OSError when the dates are not known."""
    if not isinstance(pack, dict):
        raise ValueError("a pack entry is not an object")
    if "sha256" in pack:
        from stockify.data import packs
        spec = packs.manifest(pack["sha256"])["spec"]
    else:
        spec = pack["spec"]
    frm = _iso(spec["from"])
    to = _iso(spec["to"]) if spec.get("to") is not None else None
    return frm, to


def run_span(cfg: dict) -> tuple[date, date | None] | None:
    """The earliest `from` and the latest `to` over a run config's packs; `to` is None when any pack has none
    (open-ended). For a `{"sha256"}` pack the dates are its manifest's. None when no dates are known (no packs, a
    pack without a `from`, an unreadable manifest, a date that is not YYYY-MM-DD)."""
    try:
        listed = cfg["data"]["packs"]
        if not isinstance(listed, list) or not listed:
            return None
        bounds = [_pack_bounds(p) for p in listed]
    except (KeyError, TypeError, ValueError, OSError):
        return None
    first = min(b[0] for b in bounds)
    last = None if any(b[1] is None for b in bounds) else max(b[1] for b in bounds)
    return first, last


def _reason(cfg: dict) -> str | None:
    raw = cfg.get("unlock_holdout") if isinstance(cfg, dict) else None
    # Letters or digits only count (str.isalnum), so zero-width and other invisible characters are not a reason.
    if isinstance(raw, str) and sum(c.isalnum() for c in raw) >= MIN_REASON_CHARS:
        return raw.strip()
    return None


def _locked(hold: date, last: date | None, bad_reason) -> HoldoutLocked:  # noqa: ANN001
    reach = "has no end date (it runs to the newest data)" if last is None else f"ends on {last.isoformat()}"
    why = (f"the unlock reason {bad_reason!r} is not enough: write at least {MIN_REASON_CHARS} characters of a "
           "real reason. " if bad_reason is not None else "")
    return HoldoutLocked(f"This run {reach}, and the holdout starts on {hold.isoformat()} (research.json "
                         f"holdout_from). {why}To look at the holdout anyway, {UNLOCK_HOW}. Every unlock is logged "
                         "and counted on the family's report.")


def enforce(cfg: dict) -> str | None:
    """None when no holdout is set or the run ends before it; the reason (stripped) when the run reaches the
    holdout and `cfg["unlock_holdout"]` is a string of at least 3 non-space characters. HoldoutLocked otherwise,
    and when the holdout is set but the run's span is unknown."""
    hold = holdout_from()
    if hold is None:
        return None
    span = run_span(cfg)
    if span is None:
        raise HoldoutLocked(
            f"The holdout starts on {hold.isoformat()} (research.json holdout_from), and this run's dates cannot "
            "be worked out (every pack needs a from date, and a pack named by its hash needs a readable "
            f"manifest), so it cannot be shown to stay clear of it. To run it anyway, {UNLOCK_HOW}.")
    last = span[1]
    if last is not None and last < hold:
        return None
    reason = _reason(cfg)
    if reason is None:
        raise _locked(hold, last, cfg.get("unlock_holdout"))
    return reason


def enforce_bundle(cfg: dict, bundle: dict) -> str | None:
    """The days a resolved bundle holds, against the holdout: catches a span the config did not show. None when no
    holdout is set or the last day is before it; the reason when a day is on or after `holdout_from` and the config
    has a real one (the caller logs it); HoldoutLocked otherwise, and when the bundle's days cannot be read."""
    hold = holdout_from()
    if hold is None:
        return None
    try:
        last = max(_iso(d["date"]) for d in bundle["days"])
    except (KeyError, TypeError, ValueError) as e:
        raise HoldoutLocked(f"The holdout starts on {hold.isoformat()}, and the days of this run's data cannot be "
                            f"read. To run it anyway, {UNLOCK_HOW}.") from e
    if last < hold:
        return None
    reason = _reason(cfg)
    if reason is None:
        raise _locked(hold, last, cfg.get("unlock_holdout"))
    return reason
