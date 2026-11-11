"""Daemon exit codes the launcher (`scripts/run_stockify_daemon.ps1`) acts on."""

OK = 0                       # clean stop: do not restart
# The launcher's $ExitSleep, $ExitToken and $ExitAlreadyRunning must hold these three, in this order (checked by blockwatch).
# <block name="launcher-codes" same-as="scripts/run_stockify_daemon.ps1" same-as-mode="sequence" same-as-pattern='^(?:\$Exit[A-Za-z]+|[A-Z_]+) += (?P<value>\d+)'>
SLEEP = 75                   # day done: hibernate, wake for the next trading day (D-32)
TOKEN_EXPIRED = 76           # Kite token stale or expired: wait for a fresh login, then restart
ALREADY_RUNNING = 79         # another daemon holds this lab's lock: the launcher exits, it does not restart (GK 5.1)
# </block>
RECONNECT_EXHAUSTED = 78     # the ticker gave up reconnecting: restart with backoff
