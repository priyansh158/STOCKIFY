// Run metrics (A4 §8), the twin of stockify.oracle.metrics: every reduction is a left-to-right loop in the
// stated expression order, and pow / sqrt are the same MSVC CRT functions CPython uses on Windows, so values
// agree with the oracle to the last bit or within the relative 1e-12 of A4 §9.3.
#pragma once

#include "stocksim/engine.hpp"
#include "stocksim/json_out.hpp"

namespace stocksim {

JValue compute_metrics(const Engine& engine);

}  // namespace stocksim
