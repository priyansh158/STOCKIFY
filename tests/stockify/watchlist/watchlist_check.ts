// Runs web/src/lib/watchlistMath.ts on hand cases and prints one JSON object. Node >= 22.18 runs this file directly
// (type stripping); called by tests/stockify/watchlist/test_web_math.py.
import { aboveLow, belowHigh, ltpPaise } from "../../../web/src/lib/watchlistMath.ts";

const out = {
  below: [belowHigh(90_00, 100_00), belowHigh(100_00, 100_00), belowHigh(110_00, 100_00), belowHigh(null, 100_00),
    belowHigh(90_00, null), belowHigh(90_00, 0), belowHigh(undefined, undefined)],
  above: [aboveLow(150_00, 100_00), aboveLow(100_00, 100_00), aboveLow(90_00, 100_00), aboveLow(null, 100_00),
    aboveLow(90_00, null), aboveLow(90_00, 0)],
  ltp: [ltpPaise(1234.5), ltpPaise(0.07), ltpPaise(1499.999), ltpPaise(19.99)],
};
console.log(JSON.stringify(out));
