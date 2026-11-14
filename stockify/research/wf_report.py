"""M6.5 (WF 6.6): the walk-forward report.

    wf_report.render(job_id, sensitivity_run=None) -> Path    # research/<job_id>/report.md, the ONLY writer of it
    wf_report.detail(job_id) -> dict                          # what `GET /api/research/walkforward/{id}` serves
    wf_report.WfReportError(kind)                             # unknown | not_done | missing

Only a job whose status is `done` has a report (`walkforward.finish` is the only writer of `done`, and it writes
`walkforward.json` first). Everything here reads that file, never the engine's runs: the report and the page show
the same numbers and the same words (`plain`). Window lengths are calendar days and the report says so.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from stockify import paths, store
from stockify.commands import stockify_command
from stockify.research import fileio, pbo, plain, sensitivity, walkforward

_NOT_RUN = "not run yet: `" + stockify_command("sensitivity --run {run}") + "`"
SENSITIVITY_MARK = "Costs and fills changed one at a time"     # starts the cost table's section in report.md
_YEAR_OF_DAYS = 252             # trading days in the year the Sharpe is scaled to (stats.annualised)


min_oos_days = walkforward.min_oos_days      # looked up through this name, so a test can set the minimum here


class WfReportError(ValueError):
    """The job has no report. `kind`: unknown (no such walk-forward job, or an unsafe id), not_done (not finished),
    missing (finished, but walkforward.json is gone or unreadable)."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def report_path(job_id: str) -> Path:
    return paths.research_path(job_id) / "report.md"


def load(job_id: str) -> tuple[dict, dict]:
    """The job row and its `walkforward.json`, for a finished walk-forward job; WfReportError otherwise. A stored
    error text is never echoed: it can hold paths."""
    try:
        folder = paths.research_path(job_id)
    except ValueError:
        raise WfReportError("not a valid walk-forward job id", kind="unknown") from None
    job = store.get_research_job(job_id)
    if job is None or job["kind"] != "walkforward":
        raise WfReportError("no such walk-forward job", kind="unknown")
    if job["status"] != "done":
        raise WfReportError(f"the walk-forward job is not finished (it is {job['status']}), so it has no "
                            "report", kind="not_done")
    try:
        doc = json.loads((folder / "walkforward.json").read_text(encoding="utf-8"))
        if (doc["schema"] != walkforward.SCHEMA or doc["job_id"] != job_id or not isinstance(doc["windows"], list)
                or not isinstance(doc["summary"], dict)):
            raise ValueError("not this job's result")
        for key in ("train_days", "test_days", "step_days", "objective", "dates", "index", "name"):
            doc[key]
    except (OSError, ValueError, KeyError, TypeError):
        raise WfReportError("the walk-forward job is finished but its walkforward.json is missing or "
                            "unreadable", kind="missing") from None
    return job, doc


# ---- the plain lines, written once for the report and the page --------------------------------------------------

def _summary(doc: dict) -> walkforward.WalkforwardSummary:
    s = dict(doc["summary"])
    s["dsr"] = None                                       # only the sentence builders' inputs matter; dsr is read apart
    return walkforward.WalkforwardSummary(**s)


def _few_days(doc: dict) -> tuple[int, int] | None:
    """`(days, needed)` when the stitched out-of-sample days are fewer than the minimum (D-102), else None."""
    days, needed = int(doc["summary"]["oos_days"]), min_oos_days()
    return (days, needed) if days < needed else None


def _not_enough(doc: dict) -> str | None:
    few = _few_days(doc)
    return plain.NOT_ENOUGH_OOS.format(n=few[0], m=few[1]) if few else None


def _chosen_windows(doc: dict) -> int:
    return sum(1 for w in doc["windows"] if w.get("chosen_params") is not None)


def plain_lines(doc: dict, family_id: str) -> dict[str, str]:
    """One sentence each: `ratio`, `stability`, `dsr`, `pbo`, `holdout` (no trailing full stop)."""
    s = _summary(doc)
    raw = doc["summary"]
    n = int(raw["n_trials"])
    dsr = (raw.get("dsr") or {}).get("dsr")
    few = _few_days(doc)
    lines = {
        "ratio": _not_enough(doc) or plain.wf_ratio_sentence(s),
        "stability": plain.wf_stability_sentence(s, chosen_windows=_chosen_windows(doc)),
        "dsr": (plain.wf_dsr_sentence(n, dsr, oos_days=few[0], min_oos_days=few[1]) if few else
                plain.wf_dsr_sentence(n, dsr)) if n >= 1 else "no tries were recorded",
    }
    out = pbo.family_pbo(family_id)
    if out.result is not None:
        lines["pbo"] = plain.pbo_sentence(out.result.pbo, out.n_used, out.n_total)
    else:
        lines["pbo"] = f"not worked out: {out.note or 'there is no result'}"
    unlocks = len(store.holdout_unlock_rows(family_id))
    lines["holdout"] = ("the recent days you set aside were never looked at in this family" if unlocks == 0 else
                        f"the recent days you set aside were looked at {unlocks} time{'' if unlocks == 1 else 's'} "
                        "in this family")
    return lines


def _shown(doc: dict) -> tuple[dict, list[dict]]:
    """The summary and windows as the page may show them: `oos_days_needed` added, and with too few out-of-sample
    days every Sharpe, the ratio and the deflated Sharpe None (raw P&L, the curve and the window list stay)."""
    summary = dict(doc["summary"])
    summary["oos_days_needed"] = min_oos_days()
    windows = doc["windows"]
    if _few_days(doc):
        for key in ("is_sharpe", "oos_sharpe", "ratio", "dsr"):
            summary[key] = None
        windows = [{**w, **{part: ({**w[part], "sharpe": None} if w.get(part) else w.get(part))
                            for part in ("train", "test")}} for w in windows]
    return summary, windows


def detail(job_id: str) -> dict:
    """The page's view of a finished job: the file's content plus the plain lines (`plain`)."""
    job, doc = load(job_id)
    summary, windows = _shown(doc)
    return {"job_id": job_id, "name": doc["name"], "family_id": job["family_id"], "objective": doc["objective"],
            "train": doc["train_days"], "test": doc["test_days"], "step": doc["step_days"], "summary": summary,
            "windows": [{**w, "result": _result_word(w), "no_sharpe": _no_sharpe(w)} for w in windows],
            "stitched_dates": doc["dates"], "stitched_index": doc["index"],
            "plain": {**plain_lines(doc, job["family_id"]), **notes(doc, command=False)}}


def summary_text(job_id: str) -> str:
    """The console summary of a finished job: what the command prints before the folder."""
    job, doc = load(job_id)
    raw, lines = doc["summary"], plain_lines(doc, job["family_id"])
    tested = sum(1 for w in doc["windows"] if w["test"] is not None)
    return "\n".join([
        f"Walk-forward {doc['name']} ({job_id}): {tested} of {raw['windows']} windows tested, "
        f"{raw['oos_days']} out-of-sample days (window lengths are calendar days)",
        (f"Sharpe over a year: {lines['ratio']}" if _few_days(doc) else
         f"Sharpe over a year: in-sample {_sharpe(raw['is_sharpe'])}, out-of-sample {_sharpe(raw['oos_sharpe'])}"),
        f"Out-of-sample against in-sample: {lines['ratio']}",
        f"Deflated: {lines['dsr']} (N is {raw['n_trials']})",
        f"Stability: {lines['stability']}"])


# ---- markdown ---------------------------------------------------------------------------------------------------

def _cell(x) -> str:  # noqa: ANN001
    return str(x).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _row(cells: list) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


def _table(head: list[str], rows: list[list]) -> list[str]:
    return [_row(head), "|" + "|".join(" --- " for _ in head) + "|", *(_row(r) for r in rows)]


def _sharpe(x) -> str:  # noqa: ANN001
    return f"{x:.2f}" if isinstance(x, (int, float)) and math.isfinite(x) else "-"


def _money(paise) -> str:  # noqa: ANN001
    return plain.signed_rupees(int(paise)) if paise is not None else "-"


def _settings(params: dict | None, short: bool) -> str:
    if not params:
        return "-"
    parts = []
    for key, value in params.items():
        name = key.rsplit(".", 1)[-1] if short else key
        parts.append(f"{name} = {value if isinstance(value, str) else json.dumps(value)}")
    return ", ".join(parts)


NO_SHARPE = "too few days to work one out"


def _no_sharpe(w: dict) -> str:
    """The word for a window whose training or test run exists but has no usable Sharpe; empty when both are fine
    (or the run is absent: the result word says why)."""
    for run in (w.get("train"), w.get("test")):
        if run is not None and not (isinstance(run.get("sharpe"), (int, float)) and math.isfinite(run["sharpe"])):
            return NO_SHARPE
    return ""


def _result_word(w: dict) -> str:
    if w["test"] is not None:
        word = _no_sharpe(w)
        return "tested" + (f", no Sharpe: {word}" if word else "")
    if w["chosen_params"] is None:
        return "not tested, no training run gave a usable result"
    why = plain.plain_reason(w.get("error"), fallback=None)
    return "test run did not finish" + (f" ({why})" if why else "")


def _windows_table(doc: dict) -> list[str]:
    ws = doc["windows"]
    keys = {k for w in ws for k in (w["chosen_params"] or {})}
    short = len({k.rsplit(".", 1)[-1] for k in keys}) == len(keys)        # the last part of a dotted name, if unique
    rows = []
    few = plain.NOT_ENOUGH_OOS_SHORT if _few_days(doc) else None
    for w in ws:
        train, test = w["train"], w["test"]
        rows.append([w["index"] + 1, f"{w['train_from']} to {w['train_to']}", f"{w['test_from']} to {w['test_to']}",
                     _settings(w["chosen_params"], short), (few or _sharpe(train["sharpe"])) if train else "-",
                     (few or _sharpe(test["sharpe"])) if test else "-", _money(test["net_pnl_paise"]) if test else "-",
                     _result_word(w)])
    return _table(["Window", "Training days", "Test days", "Settings chosen", "In-sample Sharpe",
                   "Out-of-sample Sharpe", "Out-of-sample net P&L", "Result"], rows)


def _sensitivity_lines(run_id: str | None) -> list[str]:
    """The cost-sensitivity section. A run with no readable sensitivity.json reads as "not run yet" (a hand-edited
    file must not break the report)."""
    command = _NOT_RUN.format(run=run_id if run_id is not None else "<id>")
    if run_id is None:
        return [command]
    try:
        d = json.loads((paths.run_path(run_id) / "sensitivity.json").read_text(encoding="utf-8"))
        if d["run_id"] != run_id or d["verdict"] not in sensitivity.VERDICTS:
            raise ValueError("not this run's result")
        rows = [sensitivity.SensitivityRow(**{k: r[k] for k in ("key", "axis", "label", "run_id", "status",
                                                                  "net_pnl_paise", "sharpe", "delta_net_paise",
                                                                  "error")}) for r in d["rows"]]
        result = sensitivity.SensitivityResult(run_id, d["job_id"], rows, d["baseline_net_paise"], bool(d["fragile"]),
                                               d["verdict"], list(d["reasons"]))
        sentence = plain.fragile_sentence(result, fragile_cost_scale=d["fragile_cost_scale"],
                                          fragile_extra_ticks=d["fragile_extra_ticks"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        return [command]
    words = {"baseline": "baseline", "done": "ran", "failed": "did not finish", "not_applicable": "not applicable"}
    body = [[r.label, words.get(r.status, "-"), _money(r.net_pnl_paise),
             _money(r.delta_net_paise) if r.status != "baseline" else "-", _sharpe(r.sharpe)] for r in rows]
    return [f"{SENSITIVITY_MARK} on run `{run_id}`.", "",
            *_table(["Variant", "Status", "Net P&L", "Change", "Sharpe"], body), "", sentence]


def _paragraph_for_days(s: walkforward.WalkforwardSummary) -> str | None:
    if s.oos_days <= 0:
        return None
    if s.oos_days < _YEAR_OF_DAYS:
        return (f"Only {s.oos_days} out-of-sample days, less than a year of trading: a Sharpe over so few days is "
                "rough, and the range it could really sit in is wide.")
    return None


def notes(doc: dict, *, command: bool = True) -> dict[str, str]:
    """The report's fixed explanatory lines written once for the report and the page:
    `objective` (how settings were chosen), `setup` (the window lengths, in calendar days), `flatter`,
    `ratio_caveat`, `coverage`, `n_note`, `curve` (the chart's caption), `y_label`, `gaps`, the three lead-ins `ratio_lead`, `stability_lead` and `pbo_lead`, `sensitivity` (where the cost check is) and, only when it
    applies, `few_days`. A line is a clause or whole sentences; the page adds a full stop only where one is missing.
    `command=False` (the page, D-145) words `sensitivity` without a command line; report.md keeps the command."""
    s = _summary(doc)
    raw = doc["summary"]
    tested = sum(1 for w in doc["windows"] if w["test"] is not None)
    objective = plain.OBJECTIVE_LABELS.get(doc["objective"], doc["objective"])
    out = {
        "objective": objective,
        "setup": (f"Window lengths are calendar days: {doc['train_days']} days of training, then "
                  f"{doc['test_days']} days of testing, moving on {doc['step_days']} days each time"
                  + (f", from {doc['span'][0]} to {doc['span'][1]}" if doc.get("span") else "")
                  + f". Settings were chosen by {objective}."),
        "flatter": ("The in-sample figure is the best of the grid on the training days, so choosing flatters it. "
                    "The out-of-sample figure is on days the choice never saw."),
        "ratio_caveat": ("The in-sample figure is the average of each window's training Sharpe; the out-of-sample "
                         "figure is the Sharpe of all the tested days joined together, so the ratio is a rough "
                         "guide, not like for like."),
        "coverage": (f"The out-of-sample figure covers {raw['oos_days']} out-of-sample days in {tested} of "
                     f"{raw['windows']} windows."),
        "n_note": (f"N = windows x grid = {raw['n_trials']}: every training run of every window is a try, "
                   "whichever one was picked."),
        "curve": "Out-of-sample equity, each tested window drawn on its own (growth of 1)",
        "y_label": "growth of 1 (dashed line = starting capital)",
        "gaps": "Each tested window is its own line; the days between windows are left empty",
        "ratio_lead": "Out-of-sample against in-sample",
        "stability_lead": "Stability",
        "pbo_lead": "Overfitting",
        "sensitivity": (
            ("The cost check isn't stored with a walk-forward job. Run `"
             + stockify_command(f"research report --walkforward {doc['job_id']} --sensitivity-run RUN_ID")
             + "` to add it to the report file.") if command else
            "The cost check isn't stored with a walk-forward job. Open the page of one of its tests and use "
            "\"Run sensitivity check\" to see how costs change a result."),
    }
    caution = _paragraph_for_days(s)
    if caution:
        out["few_days"] = caution
    return out


def build(job: dict, doc: dict, sensitivity_run: str | None) -> str:
    raw = doc["summary"]
    lines = plain_lines(doc, job["family_id"])
    tested = sum(1 for w in doc["windows"] if w["test"] is not None)
    nt = notes(doc)
    out = [f"# Walk-forward report: {_cell(doc['name'])}", "",
           f"Job `{job['job_id']}`. {nt['setup']}", "",
           "## In-sample against out-of-sample", "",
           *_table(["", "In-sample", "Out-of-sample"],
                   [["Sharpe over a year", *([_not_enough(doc)] * 2 if _few_days(doc) else
                                             [_sharpe(raw["is_sharpe"]), _sharpe(raw["oos_sharpe"])])],
                    ["Windows", raw["is_windows"], tested],
                    ["Days", "-", raw["oos_days"]]]), "",
           nt["flatter"], "",
           f"{nt['ratio_lead']}: {lines['ratio']}.",
           nt["ratio_caveat"],
           nt["coverage"]]
    if "few_days" in nt:
        out += ["", nt["few_days"]]
    out += ["", "## Deflated Sharpe", "",
            f"Out of sample: {lines['dsr']}.",
            nt["n_note"], "",
            "## Settings per window", "",
            *([f"Sharpe figures per window: {_not_enough(doc)}.", ""] if _few_days(doc) else []),
            *_windows_table(doc), "", f"{nt['stability_lead']}: {lines['stability']}.", ""]
    out += ["## Out-of-sample equity", ""]
    if (paths.research_path(job["job_id"]) / "walkforward.png").is_file() and doc["dates"]:
        out += [f"![{nt['curve']}](walkforward.png)"]
    else:
        out += ["no out-of-sample days, so no chart."]
    out += ["", "## Cost sensitivity", "", *_sensitivity_lines(sensitivity_run), "",
            "## Holdout", "", f"{lines['holdout'][:1].upper()}{lines['holdout'][1:]}.", "",
            "## Chance of overfitting", "", f"{nt['pbo_lead']}: {lines['pbo']}.", ""]
    return "\n".join(out)


def has_sensitivity_table(job_id: str) -> bool:
    """True when the job's report.md on disk carries a cost-sensitivity table (an unreadable file reads as no)."""
    try:
        return SENSITIVITY_MARK in report_path(job_id).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False


def render(job_id: str, *, sensitivity_run: str | None = None) -> Path:
    """Write research/<job_id>/report.md from the finished job's walkforward.json and return its path. The only
    writer of that file: `walkforward.finish` leaves none, and the `research report` command re-renders through
    here. `sensitivity_run` names a run whose sensitivity.json goes in (an unsafe id is refused here)."""
    if sensitivity_run is not None:
        try:
            paths.run_path(sensitivity_run)
        except ValueError:
            raise WfReportError("not a valid run id for the sensitivity table", kind="unknown") from None
    job, doc = load(job_id)
    path = report_path(job_id)
    fileio.atomic_write_text(path, build(job, doc, sensitivity_run))
    return path
