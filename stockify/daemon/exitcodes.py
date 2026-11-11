"""Daemon exit codes the launcher (`scripts/run_stockify_daemon.ps1`) acts on."""

OK = 0                       # clean stop: do not restart
# The launcher's $ExitSleep, $ExitToken, $ExitAlreadyRunning, $ExitEngineStuck and $ExitLockFailed must hold these five, in this order (checked by blockwatch).
# <block name="launcher-codes" same-as="scripts/run_stockify_daemon.ps1" same-as-mode="sequence" same-as-pattern='^(?:\$Exit[A-Za-z]+|[A-Z_]+) += (?P<value>\d+)'>
SLEEP = 75                   # day done: hibernate, wake for the next trading day (D-32)
TOKEN_EXPIRED = 76           # Kite token expired: the launcher restarts after its minimum delay; the new process waits for the login (LCB 5.2)
ALREADY_RUNNING = 79         # another daemon holds this lab's lock: the launcher exits, it does not restart (GK 5.1)
ENGINE_STUCK = 80            # the engine thread did not stop at shutdown: journals left unfinalised, restart resumes them (GK 5.2)
LOCK_FAILED = 81             # the lab's lock file cannot be opened (permissions, disk): the launcher exits (GK 5.2)
# </block>
RECONNECT_EXHAUSTED = 78     # the ticker gave up reconnecting: restart with backoff
