"""M4.11 (CB 4.3): G20 (F&O pre-open) and G16 (freeze slicing) re-run against the REAL shipped config.

The goldens keep their own bundles and are not changed. Here their worlds are rebuilt with the real
`sessions.json` phases (G20) and the real dated freeze limit (G16, from freeze_limits.json), and must still give
the golden's expected values, on the oracle and on the engine."""
from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path

import tests.support  # noqa: F401  — must precede project imports

from stockify import bundle as bundle_mod, engine, golden


def _variants(gid: str) -> list[tuple[str, dict]]:
    g = next(g for _, g in golden.goldens([gid]))
    out = []
    for v in g["variants"]:
        h = {k: copy.deepcopy(x) for k, x in g.items() if k != "variants"}
        for part in ("bundle", "run", "strategy"):
            if part in v:
                h[part] = golden._merge(h.get(part, {}), v[part])
        h["expect"] = v.get("expect", g.get("expect", {}))
        out.append((v["name"], h))
    return out


def _run(h: dict, impl: str, patch) -> list[str]:  # noqa: ANN001
    run_bundle = engine.run_bundle if impl == "engine" else __import__("stockify.oracle", fromlist=["x"]).run_bundle
    with tempfile.TemporaryDirectory(prefix=f"m4-real-{h['id']}-") as tmp:
        folder = Path(tmp)
        pack = golden.build_pack(h, folder)
        b = golden.build_bundle(h)
        patch(b)
        run_bundle(b, [pack], folder / "run", report=False)
        return golden.check(h, folder / "run")


IMPLS = ["oracle"] + (["engine"] if engine.available() else [])


class PreopenOnRealSessionsTests(unittest.TestCase):
    def test_g20_with_the_real_fo_phases(self):
        real = {d: bundle_mod.resolve_days(date.fromisoformat(d), date.fromisoformat(d), {})[0]["groups"]
                for d in ("2026-03-02",)}
        self.assertEqual([p["phase"] for p in real["2026-03-02"]["FO_FUT"]][:2], ["PREOPEN", "CONTINUOUS"])

        def use_real_sessions(b: dict) -> None:
            for day in b["days"]:
                for group in ("FO_FUT", "FO_OPT"):
                    day["groups"][group] = real[day["date"]][group]
        for name, h in _variants("G20"):
            for impl in IMPLS:
                with self.subTest(variant=name, impl=impl):
                    self.assertEqual(_run(h, impl, use_real_sessions), [])


class FreezeSlicingOnRealLimitsTests(unittest.TestCase):
    def test_g16_with_the_real_dated_limit(self):
        real = bundle_mod._freeze(None, "NIFTY", "2026-03-02")      # an index: decided by freeze_limits.json
        self.assertEqual(real, 1800)
        self.assertEqual(bundle_mod._freeze(None, "NIFTY", "2026-10-05"), 3510)   # the other variant's value

        def use_real_limit(b: dict) -> None:
            for inst in b["instruments"]:
                for spec in inst["specs"]:
                    spec["freeze_qty"] = bundle_mod._freeze(None, inst["underlying"], b["days"][0]["date"])
        variants = dict(_variants("G16"))
        for impl in IMPLS:
            with self.subTest(impl=impl):
                self.assertEqual(_run(variants["freeze_1800"], impl, use_real_limit), [])


if __name__ == "__main__":
    unittest.main()
