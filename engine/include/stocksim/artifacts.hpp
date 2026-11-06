// The run folder's deterministic artifacts (A1 §6.3, A4 §9), the twin of stockify.oracle.artifacts.write.
// manifest.json (wall clock, host) is written by the Python caller: it is outside the hashed set.
#pragma once

#include <filesystem>
#include <map>
#include <string>

#include "stocksim/engine.hpp"

namespace stocksim {

// Writes orders.csv … events_log.txt and determinism.json into `folder`; returns file name → SHA-256.
std::map<std::string, std::string> write_artifacts(const Engine& engine, const std::filesystem::path& folder);

}  // namespace stocksim
