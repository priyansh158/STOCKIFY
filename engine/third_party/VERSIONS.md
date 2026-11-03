# Vendored single-header libraries

Pinned copies; update deliberately and record the new pin and hash here.
No SQLite and no network library belong in the engine (D-04, I-11).

| File | Library | Pin | License | SHA-256 |
|---|---|---|---|---|
| `doctest.h` | doctest | tag `v2.4.12` | MIT | `94029a7d32da24a56249658147dbd2b33ff0b9ed665295cbbaf19aafff5b0ced` |
| `nlohmann/json.hpp` | nlohmann/json (single include), reading bundles only | tag `v3.12.0` | MIT | `aaf127c04cb31c406e5b04a63f1ae89369fccde6d8fa7cdda1ed4f32dfc5de63` |
| `picosha2.h` | PicoSHA2 | commit `161cb3fc4170fa7a3eca9e582cebd27cc4d1fe29` | MIT | `b13c180161ffac8d0adc81e033e493c409457c4d1258ab9781ac80579ba3bdd8` |

Fetched 2026-11-02 from `raw.githubusercontent.com` (doctest/doctest, nlohmann/json, okdshin/PicoSHA2).
