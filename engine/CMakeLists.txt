cmake_minimum_required(VERSION 3.25)
project(stocksim LANGUAGES CXX)

# STOCKIFY is Windows-only (decision D-26): the engine is built with MSVC.
if(NOT MSVC)
  message(FATAL_ERROR "stocksim builds with MSVC only (D-26). Use .\\run.ps1 -BuildEngine.")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(STOCKSIM_WARNINGS_AS_ERRORS "Treat compiler warnings as errors" ON)

# Determinism (C-9): precise floating point, never /fp:fast.
set(STOCKSIM_CXX_FLAGS /W4 /permissive- /utf-8 /fp:precise /EHsc)
if(STOCKSIM_WARNINGS_AS_ERRORS)
  list(APPEND STOCKSIM_CXX_FLAGS /WX)
endif()

set(REPO_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/..")

# ── Python + pybind11 (from the repo venv) ───────────────────────────────────
set(PYBIND11_FINDPYTHON ON)
find_package(Python 3.11 REQUIRED COMPONENTS Interpreter Development.Module)
find_package(pybind11 CONFIG REQUIRED)

# ── Build info (I-8) ─────────────────────────────────────────────────────────
find_package(Git QUIET)
set(STOCKSIM_GIT_SHA "unknown")
set(STOCKSIM_GIT_DIRTY "true")
if(GIT_FOUND)
  execute_process(COMMAND "${GIT_EXECUTABLE}" rev-parse --short=12 HEAD
                  WORKING_DIRECTORY "${REPO_ROOT}" OUTPUT_VARIABLE _sha
                  OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET RESULT_VARIABLE _sha_rc)
  if(_sha_rc EQUAL 0)
    set(STOCKSIM_GIT_SHA "${_sha}")
  endif()
  execute_process(COMMAND "${GIT_EXECUTABLE}" status --porcelain -- engine
                  WORKING_DIRECTORY "${REPO_ROOT}" OUTPUT_VARIABLE _dirty
                  OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
  if(_dirty STREQUAL "")
    set(STOCKSIM_GIT_DIRTY "false")
  endif()
endif()

if(CMAKE_BUILD_TYPE)
  set(STOCKSIM_BUILD_TYPE "${CMAKE_BUILD_TYPE}")
else()
  set(STOCKSIM_BUILD_TYPE "multi-config")
endif()
set(STOCKSIM_COMPILER_ID "${CMAKE_CXX_COMPILER_ID}")
set(STOCKSIM_COMPILER_VERSION "${CMAKE_CXX_COMPILER_VERSION}")
set(STOCKSIM_CXX_STANDARD "${CMAKE_CXX_STANDARD}")
set(STOCKSIM_PYTHON_VERSION "${Python_VERSION_MAJOR}.${Python_VERSION_MINOR}")
list(JOIN STOCKSIM_CXX_FLAGS " " _flags_text)
string(SHA256 _flags_sha "${_flags_text}")
string(SUBSTRING "${_flags_sha}" 0 16 STOCKSIM_FLAGS_HASH)
string(SHA256 _id_sha
  "${STOCKSIM_GIT_SHA}|${STOCKSIM_GIT_DIRTY}|${STOCKSIM_COMPILER_ID}|${STOCKSIM_COMPILER_VERSION}|${STOCKSIM_BUILD_TYPE}|${STOCKSIM_CXX_STANDARD}|${STOCKSIM_FLAGS_HASH}|${STOCKSIM_PYTHON_VERSION}")
string(SUBSTRING "${_id_sha}" 0 12 STOCKSIM_BUILD_ID)

configure_file(src/build_info.hpp.in "${CMAKE_CURRENT_BINARY_DIR}/generated/stocksim/build_info_data.hpp" @ONLY)
message(STATUS "stocksim engine_build_id ${STOCKSIM_BUILD_ID} (git ${STOCKSIM_GIT_SHA}, dirty ${STOCKSIM_GIT_DIRTY})")

# ── Targets ──────────────────────────────────────────────────────────────────
add_library(stocksim_core STATIC
  src/build_info.cpp
  src/types.cpp
  src/decimal.cpp
  src/sha256.cpp
  src/format.cpp
  src/json_out.cpp
  src/json_bundle.cpp
  src/event.cpp
  src/pack_reader.cpp
  src/source.cpp
  src/session.cpp
  src/instruments.cpp
  src/indicators.cpp
  src/strategy.cpp
  src/engine.cpp
  src/engine_trading.cpp
  src/ledger.cpp
  src/margin_flat.cpp
  src/costs.cpp
  src/matcher.cpp
  src/metrics.cpp
  src/artifacts.cpp)
target_include_directories(stocksim_core
  PUBLIC  "${CMAKE_CURRENT_SOURCE_DIR}/include"
  PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/generated")
target_include_directories(stocksim_core SYSTEM PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/third_party")
target_compile_options(stocksim_core PRIVATE ${STOCKSIM_CXX_FLAGS})

# The C++ reference strategies register themselves at static-init time (STOCKSIM_STRATEGY). An OBJECT library
# puts their objects straight into each binary; members of a static library nobody references would be dropped.
add_library(stocksim_strategies OBJECT
  strategies/sma_cross_cpp.cpp
  strategies/orb_cpp.cpp
  strategies/noop_cpp.cpp)
target_link_libraries(stocksim_strategies PUBLIC stocksim_core)
target_compile_options(stocksim_strategies PRIVATE ${STOCKSIM_CXX_FLAGS})

add_executable(stocksim_run tools/stocksim_run.cpp)
target_link_libraries(stocksim_run PRIVATE stocksim_strategies stocksim_core)
target_compile_options(stocksim_run PRIVATE ${STOCKSIM_CXX_FLAGS})

pybind11_add_module(_engine bindings/module.cpp bindings/engine.cpp bindings/testing_packs.cpp)
target_link_libraries(_engine PRIVATE stocksim_strategies stocksim_core)
target_compile_options(_engine PRIVATE ${STOCKSIM_CXX_FLAGS})
add_custom_command(TARGET _engine POST_BUILD
  COMMAND "${CMAKE_COMMAND}" -E copy_if_different "$<TARGET_FILE:_engine>" "${REPO_ROOT}/stockify/"
  COMMENT "Copying _engine into stockify/")

add_executable(stocksim_tests
  tests/test_main.cpp
  tests/test_build_info.cpp
  tests/test_numbers.cpp
  tests/test_pcg32.cpp
  tests/test_output.cpp
  tests/test_bundle.cpp
  tests/test_packs.cpp
  tests/test_session.cpp)
target_link_libraries(stocksim_tests PRIVATE stocksim_strategies stocksim_core)
target_compile_options(stocksim_tests PRIVATE ${STOCKSIM_CXX_FLAGS})

enable_testing()
add_test(NAME stocksim_tests COMMAND stocksim_tests)
add_test(NAME stocksim_run_version COMMAND stocksim_run --version)
