// Runs the pure helpers of web/src/lib/sweepForm.ts on fixed inputs and prints one JSON object. Called by
// tests/stockify/test_jobs_checks.py, which holds the hand-checked answers.
import { MAX_VALUES, MIN_TRIES, boolChoice, parseValues, planSweep, triesCount, triesWords, walkforwardRequest } from "../../web/src/lib/sweepForm.ts";

const p = (type: string, name = "x", label = "Opening range (minutes)") => ({ name, label, type, required: false, help: "" }) as never;
const limits = { max_trials: 1000, confirm_above: 50 };
const entry = (type: string, text: string, name = "x") => ({ param: p(type, name), ...parseValues(p(type, name), text) });
const wf = (over: object) => walkforwardRequest("sweep-1", { train: "120", test: "30", step: "30", objective: "sharpe", first: "", last: "", ...over });

const answers = {
  max: MAX_VALUES,
  minTries: MIN_TRIES,
  empty: parseValues(p("int"), "   "),
  list: parseValues(p("int"), "5, 10 ,15,"),
  range: parseValues(p("int"), "5 to 30 by 5"),
  rangeOne: parseValues(p("int"), "5 to 5 by 3"),
  decimalRange: parseValues(p("decimal"), "0.05 to 0.2 by 0.05"),
  decimalRangeExact: parseValues(p("decimal"), "0.1 to 0.5 by 0.1").values,
  decimalList: parseValues(p("decimal"), "0.05, 0.1"),
  times: parseValues(p("hhmm"), "14:30, 15:00"),
  symbols: parseValues(p("symbol"), "NSE:RELIANCE, NSE:TCS"),
  badInt: parseValues(p("int"), "5, abc"),
  badTime: parseValues(p("hhmm"), "25:99"),
  twice: parseValues(p("int"), "5, 5"),
  rangeOnText: parseValues(p("str"), "1 to 3 by 1"),
  rangeBackwards: parseValues(p("int"), "30 to 5 by 5"),
  rangeStepZero: parseValues(p("int"), "5 to 30 by 0"),
  rangeTooMany: parseValues(p("int"), "1 to 1000 by 1"),
  listTooMany: parseValues(p("int"), Array.from({ length: 101 }, (_, i) => String(i)).join(",")),
  rangeFraction: parseValues(p("int"), "5 to 10 by 2.5"),
  bool: [boolChoice("keep"), boolChoice("both"), boolChoice("true"), boolChoice("false")],
  tries: [triesCount([]), triesCount([[1, 2, 3]]), triesCount([[1, 2, 3], [], ["a", "b"]]), triesCount([[1], [2]])],
  words: [triesWords(1), triesWords(15), triesWords(12345)],
  planNoRun: planSweep([entry("int", "5, 10")], limits, false, false).reason,
  planNothing: planSweep([entry("int", "")], limits, true, false).reason,
  planSingle: planSweep([entry("int", "5")], limits, true, false).reason,
  planBad: planSweep([entry("int", "a")], limits, true, false),
  planOk: planSweep([entry("int", "5, 10, 15", "range_minutes"), entry("hhmm", "14:30, 15:00", "exit_hhmm")], limits, true, false),
  planBig: [planSweep([entry("int", "1 to 60 by 1")], limits, true, false), planSweep([entry("int", "1 to 60 by 1")], limits, true, true)]
    .map((x) => [x.tries, x.needsConfirm, x.reason]),
  planTooBig: planSweep([entry("int", "1 to 60 by 1")], { max_trials: 40, confirm_above: 50 }, true, true).reason,
  planLabelled: planSweep([{ param: p("int", "x", "Opening range (minutes)"), values: [], problem: "is not a whole number" }], limits, true, false).problems,
  wf: [
    wf({}),
    wf({ step: "10" }).problem,
    wf({ train: "0" }).problem,
    wf({ train: "abc" }).problem,
    wf({ train: "60", test: "20", step: "20", objective: "net_pnl", first: "2026-01-01", last: "2026-06-30" }).body,
    wf({ first: "1 Jan" }).problem,
    wf({ first: "2026-06-30", last: "2026-01-01" }).problem,
  ],
};
process.stdout.write(JSON.stringify(answers));
