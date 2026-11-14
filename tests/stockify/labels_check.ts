// Runs web/src/lib/labels.ts on every label the lab can attach to a run and prints one JSON object. Node >= 22.18 runs this
// directly (type stripping); called by tests/stockify/test_web_labels.py. labels.ts imports "./names" without an extension
// (the bundler's way), so a resolve hook adds ".ts" here.
import { registerHooks } from "node:module";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";

registerHooks({
  resolve(specifier, context, nextResolve) {
    if (specifier.startsWith("./") && !/\.\w+$/.test(specifier)) return nextResolve(specifier + ".ts", context);
    return nextResolve(specifier, context);
  },
});
const L = await import(pathToFileURL(resolve(import.meta.dirname, "..", "..", "web", "src", "lib", "labels.ts")).href);
const labels: string[] = JSON.parse(process.argv[2]);
const out: Record<string, { text: string; warn: boolean; meaning: string; known: boolean }> = {};
for (const l of labels) out[l] = { ...L.labelText(l), known: L.labelKnown(l), meaning: L.labelMeaning(l) };
process.stdout.write(JSON.stringify({ labels: out, step: L.marginStep("B1_span_nse_elm"), weird: L.marginStep("B9_new_step") }));
