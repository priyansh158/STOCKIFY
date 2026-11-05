"""Daemon exit codes the launcher (`scripts/run_stockify_daemon.ps1`) acts on."""

OK = 0                       # clean stop: do not restart
SLEEP = 75                   # day done: hibernate, wake for the next trading day (D-32)
TOKEN_EXPIRED = 76           # Kite token stale or expired: wait for a fresh login, then restart
RECONNECT_EXHAUSTED = 78     # the ticker gave up reconnecting: restart with backoff
