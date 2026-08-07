# Geo monitor (always-on)

Standalone process — **does not require FastAPI**. Polls Google News (via `gnews`),
classifies disruptiveness with **Gemini**, stores alerts in SQLite, emails you on
HIGH/CRITICAL.

```bash
# From STOCKIFY root (with .env configured). On macOS use python3.
python3 -m geo_monitor --once -v   # single cycle
python3 -m geo_monitor             # loop forever (interval GEO_MONITOR_INTERVAL_SEC)
python3 -m geo_monitor --once --dry-run
```

## Required .env

```
GOOGLE_AI_API_KEY=...          # or GEMINI_API_KEY
GEMINI_MODEL=gemini-3.5-flash-lite  # high-volume classifier (preferred on this key)

ALERT_EMAIL_TO=you@example.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...              # Gmail App Password
SMTP_FROM=STOCKIFY Alerts <you@example.com>

GEO_MONITOR_INTERVAL_SEC=600   # default 10 min
GEO_MONITOR_WHEN=1h            # Google News freshness window
```

Optional: Kite login (via STOCKIFY UI `/portfolio`) so holdings are queried
alongside the macro catalogue. Without Kite auth the worker is macro-only.

## Keep-alive (macOS launchd)

Runs outside FastAPI so monitoring survives API restarts. Prefer the wrapper so
cwd / `PYTHONPATH` / unbuffered stdout are correct under launchd (Desktop paths
hit macOS TCC; keep the repo under `~/Developer/STOCKIFY`).

```bash
chmod +x ~/Developer/STOCKIFY/scripts/run_geo_monitor.sh
cp ~/Developer/STOCKIFY/scripts/com.stockify.geo-monitor.plist.example \
   ~/Library/LaunchAgents/com.stockify.geo-monitor.plist
launchctl unload ~/Library/LaunchAgents/com.stockify.geo-monitor.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.stockify.geo-monitor.plist
```

Logs (Python `logging` → stderr):

```bash
tail -f /tmp/stockify-geo-monitor.err.log
# /tmp/stockify-geo-monitor.out.log stays empty unless something prints to stdout
```

Check status:

```bash
launchctl print gui/$(id -u)/com.stockify.geo-monitor | head -40
```

Unload:

```bash
launchctl unload ~/Library/LaunchAgents/com.stockify.geo-monitor.plist
```

## Alerts in the app

When FastAPI is up: `GET /api/alerts`, ack via `POST /api/alerts/{id}/ack`, UI at
`/alerts`. SQLite path: `~/.cache/STOCKIFY/kite/geo_alerts.sqlite`.
