# 🇪🇸 Spanish Immigration Status Notifier

Self-hosted web app that automatically checks the status of any Spanish
immigration / foreigner-affairs application (*expediente de extranjería*) on
the government portal and sends a **Telegram alert** the moment anything
changes. It works for any procedure the portal reports on — residence and
work permits, renewals, family reunification, and more — not one specific
programme.

Checking the portal by hand means filling in a form and solving a captcha,
every single time — and during the long months of *EN TRAMITE* you end up
doing it daily. This app does the waiting for you.

## What it does

- **Check now** — enter an expediente number, presentation date and birth
  year in a simple web form; the app fetches the current status from
  [infoext2.delegaciondelgobierno.gob.es](https://infoext2.delegaciondelgobierno.gob.es/infoext2/)
  in 1–3 minutes and shows every field (Estado de Resolución, N.I.E.,
  Tipo de Autorización, …).
- **Monitors** — register an expediente once; the app re-checks it in the
  background (hourly by default) and remembers the last known state.
- **Telegram alerts** — when the **N.I.E.**, **Estado de Resolución** or
  **Fecha de Resolución** changes, every subscriber gets a Telegram message
  within the hour. Perfect for the day *EN TRAMITE* finally becomes
  *RESUELTO – FAVORABLE*.
- **Family friendly** — one instance can monitor several expedientes (yours,
  your partner's, …), each with its own set of Telegram subscribers.

## How it works

```
 Web form / background scheduler
        │
        ▼
 ┌─────────────────┐     one check at a time,
 │  Check queue    │──── minimum 60 s between visits
 └─────────────────┘     (the portal's firewall bans bots)
        │
        ▼
 Headless Chromium (Playwright + stealth)
   → opens the portal, fills the form,
     solves the image captcha (OCR or paid API),
     submits, parses the result page
        │
        ▼
 SQLite: monitors, state, history
        │  state changed?
        ▼
 Telegram bot → alert to every subscriber
```

A few design points worth knowing:

- **The portal's firewall is aggressive.** Every check — from the web form or
  the scheduler — goes through a single queue with an enforced gap between
  visits, generous waits, and very few captcha retries. This is what keeps
  the tool working; don't tune it down.
- **Captcha**: the built-in **Tesseract OCR** solver is free and succeeds
  ~55–60% of the time per check. Background monitors simply retry on the next
  cycle, so alerts still arrive reliably; on-demand checks occasionally need a
  "try again". For near-100% reliability plug in
  [2captcha](https://2captcha.com) or [anti-captcha](https://anti-captcha.com)
  (≈ $1 per 1000 captchas) via two lines in `.env`.
- **Telegram subscriptions** use one-time `t.me` deep links (shown as link +
  QR in the UI). Telegram bots cannot contact anyone first or look up
  usernames/phone numbers — the deep link is the official pattern, and it
  doubles as proof that the subscriber owns that Telegram account.
  Unsubscribe by sending `/stop` to the bot, or with the personal
  cancellation code included in every alert.
- Everything stays on your machine: SQLite database, logs, no third-party
  services beyond Telegram (and the captcha API if you opt in).

## Installation

### Option 1 — one-click script (Linux/macOS)

```bash
git clone https://github.com/amanjuman/es-spanish-immigration-status.git
cd es-spanish-immigration-status
./setup.sh
```

The script checks Python 3.11+, installs Tesseract (apt/dnf/brew), creates a
virtualenv, downloads Chromium, asks for your Telegram bot token, and can
optionally install a systemd service so the app starts on boot. It is safe to
re-run at any time. Use `./setup.sh --no-prompt` for unattended installs.

Then open **http://127.0.0.1:8000**.

### Option 2 — Docker

```bash
git clone https://github.com/amanjuman/es-spanish-immigration-status.git
cd es-spanish-immigration-status
cp .env.example .env      # edit: at least TELEGRAM_TOKEN
docker compose up -d --build
```

### Option 3 — manual

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium     # plus system package: tesseract-ocr
cp .env.example .env                        # then edit
python -m app.web.main
```

There's also a CLI for one-off checks without the web UI:

```bash
venv/bin/python -m app.cli check E28XXXXXXXXXXXX 03/06/2026 1990
```

## Setting up Telegram alerts

1. In Telegram, message [@BotFather](https://t.me/BotFather) → `/newbot` →
   pick a name → copy the token into `TELEGRAM_TOKEN` in `.env` → restart
   the app.
2. Add a monitor in the web UI. An **activation link + QR** appears — open it
   in Telegram and press **Start**. That's it: this chat now receives the
   alerts, and the bot replies with a personal cancellation code.
3. Repeat for each person: the "Telegram link" button on a monitor generates
   a fresh one-time link (links expire after 24 h).

Bot commands: `/list` — your subscriptions · `/stop` / `/stop <n>` —
unsubscribe · `/stopall` — unsubscribe from everything. A subscription can
also be cancelled on the web page with its cancellation code.

Optionally set `TELEGRAM_CHAT_ID` in `.env` to a chat that should receive
**all** alerts regardless of subscriptions (handy for the instance owner —
get your id from [@userinfobot](https://t.me/userinfobot)).

## Configuration reference

All settings live in `.env` (see [.env.example](.env.example)):

| Variable | Default | What it does |
|---|---|---|
| `TELEGRAM_TOKEN` | *(empty)* | Bot token; without it there are no alerts, only the web UI. |
| `TELEGRAM_CHAT_ID` | *(empty)* | Optional chat that receives every alert. |
| `CAPTCHA_PROVIDER` | `ocr` | `ocr` (free), `2captcha`, or `anticaptcha`. |
| `CAPTCHA_API_KEY` | *(empty)* | API key for the paid provider. |
| `CAPTCHA_MAX_ATTEMPTS` | `5` | Captcha tries per check before giving up. |
| `MONITOR_INTERVAL_SECONDS` | `3600` | How often each monitor is re-checked. |
| `CHECK_SPACING_SECONDS` | `60` | Minimum gap between visits to the portal. **Don't lower it.** |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Where the web UI listens. |
| `DATA_DIR` | `./data` | SQLite DB, logs, debug screenshots. |
| `DEBUG_SCREENSHOTS` | `false` | Save step-by-step screenshots of each check. |

## FAQ

**A check failed with "could not solve the captcha" — is it broken?**
No. The free OCR solver fails ~40% of runs by design trade-off. Press "try
again", or configure a paid captcha provider, or just let the background
monitor retry on its next cycle.

**Why is it so slow / why only one check at a time?**
The portal sits behind an F5 firewall that blocks bot-like traffic and can
ban your IP for a while. Slow and single-file is the only sustainable way.

**Can I expose the web UI to the internet?**
It has no authentication and it accepts personal data — keep it on
`127.0.0.1` (default), or put a reverse proxy with auth / a VPN (Tailscale,
WireGuard) in front.

**Is this official?**
No. Unofficial tool, not affiliated with the Spanish government. The data
shown is whatever the portal returns — always verify important outcomes on
the portal itself. Use it for your own expediente and your family's (with
their consent); expediente numbers and birth years are personal data.

**The portal changed and checks stopped working.**
The HTML structure is parsed by this app; if the site changes, the checker
needs updating. Please open an issue (or a PR).

## Running a public instance

By default the app assumes a private (home/LAN) deployment. To host it
publicly for other people, set `PUBLIC_MODE=true` in `.env` — this switches
on a one-to-one privacy model and abuse protection:

- **Nobody can see anyone else's data.** There is no public list of monitors.
  Each monitor gets a secret **management link** (`/m/<token>`) shown once at
  creation and delivered to the creator's Telegram — it's the only way to
  view status, pause/resume, rename, add/remove subscribers, or delete.
  The operator's overview (`/api/monitors`) requires `ADMIN_TOKEN`, and the
  operator's `TELEGRAM_CHAT_ID` no longer receives other people's alerts.
- **Capacity is protected.** The queue physically fits ~60 portal visits per
  hour, so: `MAX_MONITORS` caps sign-ups (default 40), the re-check interval
  stretches automatically as the instance fills (`2 × monitors × spacing`),
  repeat on-demand checks of the same expediente are answered from a
  15-minute cache, and per-IP rate limits apply
  (`RATE_LIMIT_CHECKS_PER_HOUR`, `RATE_LIMIT_MONITORS_PER_DAY`).
- **Bots are kept out** with optional
  [Cloudflare Turnstile](https://www.cloudflare.com/products/turnstile/)
  (`TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY`).
- **Data hygiene**: monitors are purged 30 days after a final resolution
  (auto-paused after 2 days), unactivated monitors after 7 days, history
  after 90 days. There's a `/privacy` page describing all of it.

Recommended VPS setup: Docker Compose behind a reverse proxy with HTTPS
(Cloudflare proxied DNS works well and pairs with Turnstile), `SITE_URL` set
to the public URL, `TRUST_PROXY=true`, and a cron job backing up
`data/notifier.sqlite3`. A `/healthz` endpoint reports queue and monitor
stats for uptime monitoring.

Subscribers manage everything themselves: `/list`, `/status`, `/stop`,
`/stopall` in the Telegram bot, the cancellation code on the web page, or the
management link for the monitor owner. WhatsApp alerts are architecturally
prepared (per-subscription channel) but not yet wired to a provider.

## Development

```bash
venv/bin/python -m pytest        # unit tests (no network needed)
```

Project layout: `app/core/` — site automation (Playwright flow, captcha
solvers, parser); `app/web/` — FastAPI app, check queue, scheduler, SQLite,
Telegram bot, templates. The single-worker check queue with enforced spacing
is a hard requirement (the portal's firewall blocks rapid/parallel requests);
keep that in mind when changing the automation.

## License

[MIT](LICENSE)
