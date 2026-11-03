# STOCKIFY web

Next.js 16 / React 19 frontend for the Windows-localhost STOCKIFY application.
Use the repository-root PowerShell 7 launcher for normal development:

```powershell
.\run.ps1
.\run.ps1 -WebOnly
```

Direct startup:

```powershell
$env:STOCKIFY_API_ORIGIN="http://127.0.0.1:8001"
cd web
npm ci
npm run dev -- --hostname 127.0.0.1 --port 3000
```

Open `http://127.0.0.1:3000`. `/api/*` is rewritten to the FastAPI origin;
REST, SSE, and live-tick WebSockets remain same-origin in the browser.

Verification:

```powershell
cd web
npm run lint
npm run build
```

Node.js 20.9 or newer is required.
