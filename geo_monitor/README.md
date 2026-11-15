# Geo monitor (Windows localhost)

The geo monitor is an independent process. It polls Google News, classifies
disruptive headlines with Gemini, stores alerts in SQLite, and emails
HIGH/CRITICAL alerts even when FastAPI is not running.

## Run

After `.\run.ps1` has created `.venv`:

```powershell
.\scripts\run_geo_monitor.ps1 -Once -VerboseOutput
.\scripts\run_geo_monitor.ps1 -Once -DryRun
.\scripts\run_geo_monitor.ps1
```

The main launcher can also supervise it:

```powershell
.\run.ps1 -GeoMonitor
.\run.ps1 -MonitorOnly
```

## Environment

Configure the repository-root `.env`:

```text
GOOGLE_AI_API_KEY=...
GEMINI_MODEL=gemini-3.5-flash-lite
ALERT_EMAIL_TO=you@example.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=STOCKIFY Alerts <you@example.com>
GEO_MONITOR_INTERVAL_SEC=600
GEO_MONITOR_WHEN=1h
```

The market daemon's own email alerts (login needed, session errors, parity failures,
the daily summary; `stockify/config/notify.json`) go through the same SMTP settings.
`python -m stockify notify test` sends one test message with them.

Kite authentication is optional. When available, current holdings extend the
monitor queries; otherwise it runs macro-only.

## Always-on Task Scheduler lifecycle

PowerShell 7 registers a per-user task that starts at logon, ignores duplicate
starts, and retries after crashes:

```powershell
.\scripts\manage_geo_monitor.ps1 Install
.\scripts\manage_geo_monitor.ps1 Start
.\scripts\manage_geo_monitor.ps1 Status
.\scripts\manage_geo_monitor.ps1 Stop
.\scripts\manage_geo_monitor.ps1 Uninstall
```

Logs: `%LOCALAPPDATA%\STOCKIFY\logs\geo-monitor.log`

Alerts database:
`%LOCALAPPDATA%\STOCKIFY\kite\geo_alerts.sqlite`

The monitor is its own background process. It starts with `.\run.ps1 -GeoMonitor`
or the scheduled task above, and the web pages cannot start it (D-146).

When FastAPI is running, use `GET /api/alerts`,
`POST /api/alerts/{id}/ack`, or the `/alerts` page.
