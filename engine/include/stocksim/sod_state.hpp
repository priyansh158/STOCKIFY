// A paper session's start-of-day state (A1 §6.4, A4 §1.10, D-66), the twin of stockify.oracle.sod_state.
//
//   const std::string text = eod_state_json(engine);   // after a day's run: canonical JSON, byte for byte the oracle's
//   load_sod_state(next_day_engine, text);             // before its first event (the constructor does it for a
//                                                      // bundle's `sod_state`; set_strategy hands the strategy its part)
//
// Everything one day hands to the next: the ledger (positions as exact cost and units), pending and earlier final
// orders, DP charges taken, timers and built bars still to fire, id counters, both PCG32 streams, subscriptions,
// the pause state, FSPs seen, market state, the full bar history (never trimmed, C-2) and the strategy's own state.
// Integers only outside `strategy`. Unknown keys are refused at every level; so is a missing one.
#pragma once

#include <string>
#include <string_view>

#include "stocksim/json_bundle.hpp"

namespace stocksim {

class Engine;

// The state at the end of the engine's last processed day (or, before a run, the one it was loaded with).
// RunError when the state cannot be written (a CNC release left after DAY_END, a timer with no queued event, a
// strategy state that is not a JSON object).
std::string eod_state_json(const Engine& engine);

// Apply a state to an engine that has not processed any event, at most once. RunError otherwise, for invalid JSON,
// another schema, a missing or unknown key, or a strategy whose load_state raises.
void load_sod_state(Engine& engine, std::string_view text);
void load_sod_state_json(Engine& engine, const Json& state);  // a parsed state (a bundle's `sod_state`)

}  // namespace stocksim
