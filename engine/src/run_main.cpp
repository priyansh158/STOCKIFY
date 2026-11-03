// stocksim_run: command-line runner for backtests and replays (M3).
// M0 only implements --version.
#include <cstdio>
#include <cstring>

#include "stocksim/build_info.hpp"

int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "--version") == 0) {
        std::printf("%s\n", stocksim::build_info_text().c_str());
        return 0;
    }
    std::fprintf(stderr, "usage: stocksim_run --version\n"
                         "(running bundles arrives in milestone M3)\n");
    return 2;
}
