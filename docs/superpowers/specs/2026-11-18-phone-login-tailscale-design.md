# Phone login through Tailscale

Date: 2026-11-18. Approach chosen by the owner: option 1 (a private network between phone and laptop).

## Intent

The owner wants to do the daily Kite login (16:30–19:30 PST) from their phone, with only the laptop on, so the
recorder can start its session without the owner at the laptop. Also useful: reading the app from the phone.

## How it works

- Tailscale (vetted: 1.104.6, published 2026-11-11, SHA-256 8445636f…ba90ab matches pkgs.tailscale.com, Authenticode
  signed by Tailscale Inc. via DigiCert) runs on the laptop and the phone, signed in to the owner's own account.
- On the laptop, `tailscale serve` publishes the web app (port 3000) at a private https address,
  `https://<laptop>.<tailnet>.ts.net`, reachable only from the owner's own Tailscale devices. Nothing listens on the
  public internet; the API and web keep binding 127.0.0.1.
- The Kite app's redirect URL changes (owner, in the Kite developer console) to
  `https://<laptop>.<tailnet>.ts.net/api/kite/callback`. The laptop logs in through the same address (Tailscale runs
  on the laptop too), so there is one login path.
- The phone opens that address, taps the Kite login on the Portfolio page, logs in to Zerodha, and lands back on the
  app's "logged in" page. The recorder picks up the token as today.

## Code changes

- One opt-in setting, `STOCKIFY_REMOTE_HOST` in `.env` (the owner sets it; empty by default = today's behaviour).
- The API's three guards (server/local_only.py) accept exactly that one host name, in addition to loopback, for
  `Host`, `X-Forwarded-Host` and the web `Origin` (`https://<that host>`); nothing else is relaxed, and the docstring's
  "there is no setting to relax" line is rewritten to name this one setting and why.
- The Kite login URL and the callback's return address use the remote address when the setting is on.
- Next.js: the dev server is told to allow that host (Next 16's dev-origin allowance), and its /api rewrite keeps
  working through the proxy.
- `run.ps1` gains nothing new; a short README section says how to set it up once.
- Tests for each guard with the setting off (unchanged) and on (the one host accepted, any other refused).

## What the owner does once

1. Install Tailscale on the laptop (the vetted installer, needs an admin click) and on the phone (App Store /
   Play Store), sign both in to the same account.
2. Run `tailscale serve --bg 3000` on the laptop once (it persists across restarts).
3. Put the laptop's address in `.env` as `STOCKIFY_REMOTE_HOST`, and set the same `/api/kite/callback` URL in the
   Kite developer console.

## Non-goals and limits

- No public exposure, no automatic login, no stored password.
- Only the owner's own Tailscale devices can reach the app; whoever holds the phone (unlocked) can use the app.
- The laptop must be on and awake at login time (the recorder's wake task already wakes it at 16:30 PST).
