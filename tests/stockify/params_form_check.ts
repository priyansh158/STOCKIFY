// Runs the form helpers of web/src/lib/params.ts (percentages, choice lists) and prints one JSON object of the
// answers; called by tests/stockify/test_param_checks.py. Node >= 22.18 runs this file directly (type stripping).
import { coerceParamText, fractionToPercentText, paramText, percentTextToFraction, validateParams } from "../../web/src/lib/params.ts";

const base = { name: "x", label: "", default: null, required: false, help: "", choices: [], percent: false };
const invest = { ...base, name: "invest", type: "decimal" as const, default: "0.95", percent: true };
const bars = {
  ...base, name: "interval_s", type: "int" as const, default: 86400,
  choices: [{ value: 60, label: "1 minute" }, { value: 86400, label: "1 day" }],
};

const toPercent = ["0.95", "1", "0", "0.075", "0.5", "1.00", "0.001", "abc", ""].map((t) => fractionToPercentText(t));
const toFraction = ["95", "9.5", "100", "0", "0.1", "95 %", "7.5%", "1000", "abc", ""].map((t) => percentTextToFraction(t));
process.stdout.write(JSON.stringify({
  toPercent,
  toFraction,
  startText: paramText(invest),
  stored: coerceParamText(invest, "95"),
  storedInt: coerceParamText(bars, "300"),
  okChoice: validateParams([bars], { interval_s: 60 }),
  okChoiceText: validateParams([bars], { interval_s: "86400" }),
  badChoice: validateParams([bars], { interval_s: 7 }),
}));
