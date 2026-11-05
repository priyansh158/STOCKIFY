"""stockify.daemon.subscriptions: tiers, re-centring with hysteresis, pins, the cap (U-13)."""
from __future__ import annotations

import unittest
from datetime import date

import tests.support as support  # noqa: F401  — must precede project imports
from tests.support import SandboxedDBTest

from stockify.daemon.clock import ManualClock
from stockify.daemon.subscriptions import FULL, QUOTE, SubscriptionManager
from stockify.data import instruments, refdata

DAY = date(2026, 11, 11)
WEEKLY, MONTHLY, NEXT = "2026-11-17", "2026-11-24", "2026-12-29"
STRIKES = list(range(24_000, 26_001, 50))          # rupees
HEADER = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
          "tick_size,lot_size,instrument_type,segment,exchange")


def dumps() -> dict[str, str]:
    nse = [HEADER, "256265,1,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE",
           "738561,2,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE"]
    nfo = [HEADER,
           f"5001,1,NIFTY26NOVFUT,NIFTY,0,{MONTHLY},0.0,0.1,65,FUT,NFO-FUT,NFO",
           f"5002,2,NIFTY26DECFUT,NIFTY,0,{NEXT},0.0,0.1,65,FUT,NFO-FUT,NFO"]
    token = 10_000
    for expiry in (WEEKLY, MONTHLY):
        for k in STRIKES:
            for kind in ("CE", "PE"):
                token += 1
                nfo.append(f"{token},{token},NIFTY{expiry}{k}{kind},NIFTY,0,{expiry},{k}.0,0.05,65,{kind},NFO-OPT,NFO")
    return {"NSE": "\n".join(nse) + "\n", "NFO": "\n".join(nfo) + "\n"}


def universe(**over) -> dict:
    u = {"indices_full": ["NSE:NIFTY 50"],
         "futures_full": [{"underlying": "NIFTY", "months": ["near", "next"]}],
         "chains": [{"underlying": "NIFTY", "expiry": "weekly:0", "n_full": 2, "n_quote": 4,
                     "recenter_threshold": 2, "hysteresis": 1}],
         "equities_full": [], "stock_futures_full": [],
         "equities_quote": ["NSE:RELIANCE"],
         "limits": {"max_per_connection": 3000, "warn_fraction": "0.8"}}
    u.update(over)
    return u


class SubscriptionTest(SandboxedDBTest):
    db_modules = (refdata,)

    def setUp(self):
        super().setUp()
        instruments.sync(DAY, dumps(), underlyings={"NIFTY": "NSE:NIFTY 50"})
        self.logged: list[tuple] = []
        self.clock = ManualClock(1_000)
        self.index = instruments.find("NSE", "NIFTY 50")

    def manager(self, **over) -> SubscriptionManager:
        return SubscriptionManager(DAY, universe(**over), self.clock, log_rows=self.logged.extend)

    def strikes(self, m: SubscriptionManager, mode: str, expiry: str = WEEKLY) -> list[int]:
        out = set()
        for sub in m.current.values():
            info = instruments.get(sub.instrument_id)
            if info["kind"] in ("CE", "PE") and info["expiry"] == expiry and sub.mode == mode:
                out.add(info["strike_paise"] // 100)
        return sorted(out)


class BuildTests(SubscriptionTest):
    def test_tiers_from_the_universe(self):
        m = self.manager()
        m.build({self.index: 2_501_000})                     # 25,010 -> ATM 25,000
        self.assertEqual(self.strikes(m, FULL), [24_900, 24_950, 25_000, 25_050, 25_100])
        self.assertEqual(self.strikes(m, QUOTE), [24_800, 24_850, 25_150, 25_200])
        by_reason = {s.reason: s.mode for s in m.current.values() if not s.reason.startswith("chain")}
        self.assertEqual(by_reason, {"index:NSE:NIFTY 50": FULL, "future:NIFTY:near": FULL,
                                     "future:NIFTY:next": FULL, "equity:NSE:RELIANCE": QUOTE})
        self.assertEqual(m.counts(), {FULL: 3 + 10, QUOTE: 1 + 8})

    def test_weekly_chain_centres_on_the_index_and_monthly_on_its_future(self):
        m = self.manager(chains=[
            {"underlying": "NIFTY", "expiry": "weekly:0", "n_full": 0, "n_quote": 0},
            {"underlying": "NIFTY", "expiry": "monthly:0", "n_full": 0, "n_quote": 0}])
        refs = {c.key: c.ref_id for c in m.chains}
        self.assertEqual(refs["NIFTY:weekly:0"], self.index)
        self.assertEqual(refs["NIFTY:monthly:0"], instruments.find("NFO", "NIFTY26NOVFUT"))
        self.assertEqual(m.reference_ids(), set(refs.values()))

    def test_n_full_zero_is_a_quote_only_chain(self):
        m = self.manager(chains=[{"underlying": "NIFTY", "expiry": "weekly:0", "n_full": 0, "n_quote": 2}])
        m.build({self.index: 2_501_000})
        self.assertEqual(self.strikes(m, FULL), [])
        self.assertEqual(self.strikes(m, QUOTE), [24_900, 24_950, 25_000, 25_050, 25_100])

    def test_no_price_means_no_chain_yet(self):
        m = self.manager()
        m.build({})
        self.assertEqual(self.strikes(m, FULL), [])
        self.assertTrue(any("no reference price" in w for w in m.warnings))

    def test_missing_instruments_warn_instead_of_failing(self):
        m = self.manager(equities_quote=["NSE:NOSUCH"], futures_full=[{"underlying": "NIFTY", "months": ["far"]}])
        self.assertTrue(any("NOSUCH" in w for w in m.warnings))
        self.assertTrue(any("far future" in w for w in m.warnings))

    def test_every_subscription_is_logged(self):
        m = self.manager()
        m.build({self.index: 2_501_000})
        self.assertEqual(len(self.logged), len(m.current))
        self.assertTrue(all(r[1] == "subscribe" and r[0] == 1_000 for r in self.logged))


class RecentreTests(SubscriptionTest):
    def setUp(self):
        super().setUp()
        self.m = self.manager()
        self.m.build({self.index: 2_501_000})
        self.logged.clear()

    def test_a_one_strike_move_changes_nothing(self):
        self.assertTrue(self.m.update_prices({self.index: 2_504_000}).empty())   # ATM 25,050: 1 strike

    def test_a_two_strike_move_recentres_with_hysteresis(self):
        diff = self.m.update_prices({self.index: 2_510_000})                      # ATM 25,100
        self.assertEqual(self.strikes(self.m, FULL), [25_000, 25_050, 25_100, 25_150, 25_200])
        # 24,850 is n_quote + hysteresis = 5 strikes away: kept. 24,800 is 6 away: removed.
        self.assertEqual(self.strikes(self.m, QUOTE), [24_850, 24_900, 24_950, 25_250, 25_300])
        removed = {instruments.get(s.instrument_id)["strike_paise"] // 100 for s in diff.unsubscribe}
        self.assertEqual(removed, {24_800})
        added = {instruments.get(s.instrument_id)["strike_paise"] // 100 for s in diff.subscribe}
        self.assertEqual(added, {25_250, 25_300})
        actions = {r[1] for r in self.logged}
        self.assertEqual(actions, {"subscribe", "unsubscribe", "mode"})


class PinAndCapTests(SubscriptionTest):
    def far_call(self) -> int:
        return instruments.derivative("NIFTY", "CE", WEEKLY, 2_400_000)

    def test_pins_survive_recentring_until_unpinned(self):
        m = self.manager()
        m.build({self.index: 2_501_000})
        far = self.far_call()
        m.pin([far], owner="session-1")
        self.assertEqual(m.current[far].mode, FULL)
        m.update_prices({self.index: 2_560_000})
        self.assertIn(far, m.current)
        diff = m.unpin("session-1")
        self.assertIn(far, {s.instrument_id for s in diff.unsubscribe})

    def test_cap_trims_the_farthest_quote_strikes_first(self):
        m = self.manager(limits={"max_per_connection": 18, "warn_fraction": "0.8"})
        far = self.far_call()
        m.pins["session-1"] = {far}
        m.build({self.index: 2_501_000})
        self.assertLessEqual(len(m.current), 18)
        self.assertIn(far, m.current)                                   # pins are never trimmed
        # FULL = the five ATM strikes plus the pinned 24,000 call; only QUOTE strikes are trimmed.
        self.assertEqual(self.strikes(m, FULL), [24_000, 24_900, 24_950, 25_000, 25_050, 25_100])
        quote = self.strikes(m, QUOTE)
        self.assertNotIn(24_800, quote)
        self.assertTrue(any("cap 18" in w for w in m.warnings))

    def test_unsubscribe_all_at_session_end(self):
        m = self.manager()
        m.build({self.index: 2_501_000})
        n = len(m.current)
        diff = m.unsubscribe_all()
        self.assertEqual(len(diff.unsubscribe), n)
        self.assertEqual(m.current, {})
        self.assertEqual(self.logged[-1][5], "session_end")


if __name__ == "__main__":
    unittest.main()
