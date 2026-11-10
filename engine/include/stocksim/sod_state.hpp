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

// The longest state text either runner reads (the oracle's sod_state.MAX_BYTES): checked before anything is parsed.
// The largest state the tests write is about 31 KB and a long paper session's is some tens of MB.
inline constexpr std::size_t kSodStateMaxBytes = 256U * 1024U * 1024U;

// A state a C++ strategy hands back, read by the same rules as a state text (at most 64 levels deep, integers
// inside 64 bits, no key twice, no NUL); RunError otherwise.
Json parse_strategy_state(std::string_view text);

// The state at the end of the engine's last processed day (or, before a run, the one it was loaded with).
// RunError when the state cannot be written (a CNC release left after DAY_END, a timer with no queued event, a
// strategy state that is not a JSON object).
std::string eod_state_json(const Engine& engine);

// Apply a state to an engine that has not processed any event, at most once. The state file is untrusted input: all of
// it is checked first (the oracle's checks, in its order, with its words) and then applied, so a refusal leaves the
// engine exactly as it was. RunError for invalid JSON, a text past the size limit, another schema, a missing or
// unknown key, a value out of its range, an id the bundle lacks, or a strategy whose load_state raises.
void load_sod_state(Engine& engine, std::string_view text);
void load_sod_state_json(Engine& engine, const Json& state);  // a parsed state (a bundle's `sod_state`)

}  // namespace stocksim
