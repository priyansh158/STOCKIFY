// Runs web/src/lib/params.ts on tests/fixtures/param_cases.json and prints one JSON array of the problems it
// reports (null when none), in case order. Node ≥ 22.18 runs this file directly (type stripping); called by
// tests/stockify/test_param_checks.py.
import { readFileSync } from "node:fs";
import { validateParams } from "../../web/src/lib/params.ts";

type Case = { type: string; value: unknown; problem: string | null };

const fixture = JSON.parse(readFileSync(process.argv[2], "utf-8")) as { cases: Case[] };
const answers = fixture.cases.map((c) => {
  const problems = validateParams(
    [{ name: "x", label: "", type: c.type as never, default: null, required: false, help: "" }],
    { x: c.value },
  );
  return problems.length ? problems[0].split(": ").slice(1).join(": ") : null;
});
process.stdout.write(JSON.stringify(answers));
