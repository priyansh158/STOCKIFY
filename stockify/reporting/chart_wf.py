"""The chart of a finished walk-forward job (CH 6.12): each window's out-of-sample test run drawn one after the other.

A window's chart is `chart.run_chart` of its `test_run_id`, cut to the window's own dates (calendar days, IST, both
ends inclusive, as the job stores them). The windows never overlap (M6 mismatch row 10), so the bars simply follow
one another; a window that would start before the previous one ended is clipped to start where it ended. Every
marker is a fill or settlement of an out-of-sample run and keeps that run's own FIFO trade profit (a test run starts
flat); the training runs are never read. A window whose test run failed, was skipped or has no folder is listed with
its status and no bars, never hidden.

Reads only the job's `walkforward.json` (through `wf_report`, so the words match the report page) and the test runs'
folders and packs. A window the summary leaves out (no stitched test) is listed as excluded, with the page's reason,
and is not drawn; a tested window whose run never traded the chosen instrument is `no_trades`. Nothing is fetched or
written. Times are UTC epoch ns here (`from_ns`, `to_ns` of each window, half-open `[from_ns, to_ns)`).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from stockify import paths, timeutil
from stockify.reporting import chart

_KNOWN = ("done", "failed", "skipped", "pending")
_NOT_TESTED = "not tested, "
_NOT_MIXED = "not charted: different price basis or interval"


def _session_open_s(wins: list[dict]) -> int | None:
    """Seconds after IST midnight at which the cash session opens on the first window's first day (dated config), for
    the page's gap shading; None when the config has no session for it."""
    from stockify.data import calendar
    try:
        return calendar.continuous_open_s("CM", date.fromisoformat(wins[0]["from_d"]))
    except (IndexError, ValueError, calendar.CalendarError):
        return None


class WfChartError(ValueError):
    """The job has no chart. `kind`: unknown (no such walk-forward job, or an unsafe id), not_done (not finished),
    missing (finished, but its stored windows are missing or unreadable), instrument (no window's run has it).
    Messages are fixed words: no id, path or stored text."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def _windows(job_id: str) -> list[dict]:
    """The job's windows, checked for shape, as {index, from_d, to_d, from_ns, to_ns, run_id, status, result, no_sharpe,
    excluded}. They come from the job's `walkforward.json` through the same functions the report page uses, so the
    chart and the summary say the same: a window with no stitched test (`test` is None) is `excluded` with the page's
    reason and is not drawn; `result` and `no_sharpe` are the page's words."""
    from stockify.research import wf_report
    try:
        _, doc = wf_report.load(job_id)
    except wf_report.WfReportError as e:
        if e.kind == "unknown":
            raise WfChartError("no such walk-forward job", kind="unknown") from None
        if e.kind == "not_done":
            raise WfChartError("the walk-forward job is not finished, so it has no chart", kind="not_done") from None
        raise WfChartError("the walk-forward job has no readable windows", kind="missing") from None
    unreadable = WfChartError("the walk-forward job has no readable windows", kind="missing")
    try:
        _, shown = wf_report._shown(doc)
        if not shown:
            raise unreadable
        out = []
        for w in shown:
            first, last = date.fromisoformat(w["test_from"]), date.fromisoformat(w["test_to"])
            index, run_id, status = w["index"], w.get("test_run_id"), w["test_status"]
            if (isinstance(index, bool) or not isinstance(index, int) or last < first
                    or not (run_id is None or isinstance(run_id, str)) or not isinstance(status, str)):
                raise unreadable
            result = wf_report._result_word(w)
            excluded = None
            if w.get("test") is None:
                excluded = result[len(_NOT_TESTED):] if result.startswith(_NOT_TESTED) else result
            out.append({"index": index, "from_d": first.isoformat(), "to_d": last.isoformat(),
                        "from_ns": timeutil.ist_ns(first, "00:00"),
                        "to_ns": timeutil.ist_ns(last + timedelta(days=1), "00:00"), "run_id": run_id,
                        "status": status if status in _KNOWN else "unknown", "result": result,
                        "no_sharpe": wf_report._no_sharpe(w), "excluded": excluded})
    except WfChartError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise unreadable from None
    out.sort(key=lambda w: (w["from_ns"], w["index"]))
    return out


def _has_folder(run_id: str | None) -> bool:
    if not run_id:
        return False
    try:
        return paths.run_path(run_id).is_dir()
    except ValueError:
        return False


def walkforward_chart(job_id: str, instrument_id: int | None = None, from_ns: int | None = None,
                      to_ns: int | None = None) -> dict:
    """The stitched out-of-sample chart of a finished walk-forward job, shaped like `run_chart` (so the same DTO
    builder serves both) plus `windows` and, on each marker, the `window` index it came from. `instrument_id` None is
    the first traded instrument of the first window that has a chart, then the same one for every window. At most
    `chart.MAX_BARS` bars per call: when `truncated`, ask again from `next_from_ns`. Raises WfChartError (see there)
    and ValueError for a bad argument."""
    if instrument_id is not None:
        chart._int_arg("instrument_id", instrument_id)
    for name, v in (("from_ns", from_ns), ("to_ns", to_ns)):
        if v is not None:
            chart._int_arg(name, v)
    wins = _windows(job_id)

    bars: list[dict] = []
    markers: list[dict] = []
    plots: dict[tuple, dict] = {}
    instruments: dict[int, dict] = {}
    states: list[str] = []
    notices: list[str] = []
    basis = interval = None
    omitted = skipped = idle = 0
    pnl_complete = True
    truncated, next_from = False, None
    chosen = instrument_id
    queried = found = 0
    uncharted = no_trades = mixed = 0
    prev_hi = None
    listed = []
    for w in wins:
        listed.append({k: w[k] for k in ("index", "from_ns", "to_ns", "from_d", "to_d", "run_id", "status", "result",
                                         "no_sharpe", "excluded")})
        run_id = w["run_id"]
        if w["excluded"] is not None:
            listed[-1]["run_id"] = None
            run_id = None
        elif w["status"] == "done" and not _has_folder(run_id):
            listed[-1]["run_id"], listed[-1]["status"] = None, "missing"
            run_id = None
        elif w["status"] != "done":
            listed[-1]["run_id"] = None
            run_id = None
        if run_id is None:
            uncharted += 1
        lo = w["from_ns"] if prev_hi is None else max(w["from_ns"], prev_hi)
        prev_hi = w["to_ns"] if prev_hi is None else max(prev_hi, w["to_ns"])
        if run_id is None or truncated:
            continue
        elo = lo if from_ns is None else max(lo, from_ns)
        ehi = w["to_ns"] if to_ns is None else min(w["to_ns"], to_ns)
        if elo >= ehi:
            continue
        queried += 1
        try:
            out = chart.run_chart(run_id, chosen, elo, ehi)
        except LookupError:
            listed[-1]["status"] = "no_trades"                   # this run has not traded that instrument
            no_trades += 1
            continue
        if out["data_state"] == "ok" and basis is not None and interval is not None and (
                out["basis"], out["interval_s"]) != (basis, interval):
            listed[-1]["run_id"], listed[-1]["excluded"] = None, _NOT_MIXED      # never drawn on the same axis
            mixed += 1
            continue
        found += 1
        chosen = out["instrument_id"]
        states.append(out["data_state"])
        for i in out["instruments"]:
            instruments.setdefault(i["id"], i)
        if out["data_state"] == "ok":
            basis = basis or out["basis"]
            interval = interval or out["interval_s"]
        else:
            basis = basis or out["basis"]
        if out["notice"] and out["notice"] not in notices:
            notices.append(out["notice"])
        pnl_complete = pnl_complete and out["pnl_complete"]
        skipped += out["skipped_settlements"]
        idle += out["idle_settlements"]
        omitted += out["omitted_markers"]

        here = out["bars"]
        room = chart.MAX_BARS - len(bars)
        cut = None
        if len(here) > room:
            cut, here = here[room]["start_ns"], here[:room]
            truncated, next_from = True, cut
        elif out["truncated"]:
            truncated, next_from = True, out["next_from_ns"]
        bars.extend(here)
        for m in out["markers"]:
            if cut is not None and m["t_ns"] >= cut:
                omitted += 1
            else:
                markers.append({**m, "window": w["index"]})
        for p in out["plots"]:
            pts = [q for q in p["points"] if cut is None or q[0] < cut]
            if pts:
                key = (p["name"], p["kind"], p["instrument_id"])
                plots.setdefault(key, {**p, "points": []})["points"].extend(pts)

    if instrument_id is not None and queried and not found:
        raise WfChartError("no such instrument in this walk-forward", kind="instrument")
    if uncharted:
        notices.append(f"{uncharted} of {len(wins)} windows have no chart: their test run failed, was skipped, is "
                       "gone or is left out of the summary.")
    if mixed:
        notices.append(f"{mixed} window{'s were' if mixed > 1 else ' was'} not charted: "
                       f"{'their' if mixed > 1 else 'its'} price basis or bar interval differs from the first charted "
                       "window's.")
    if no_trades:
        notices.append(f"{no_trades} window{'s' if no_trades > 1 else ''} did not trade this instrument, so "
                       f"{'they have' if no_trades > 1 else 'it has'} no markers here.")
    if "ok" in states:
        state = "ok"
    else:
        state = states[0] if states else "no_pack"
    return {"instruments": sorted(instruments.values(), key=lambda i: i["id"]), "instrument_id": chosen,
            "basis": basis, "interval_s": interval, "bars": bars, "markers": markers, "omitted_markers": omitted,
            "plots": list(plots.values()), "truncated": truncated, "next_from_ns": next_from, "data_state": state,
            "notice": " ".join(notices) or None, "pnl_complete": pnl_complete, "skipped_settlements": skipped,
            "idle_settlements": idle, "windows": listed, "session_open_s": _session_open_s(wins)}
