# BD Visa Slot Monitor

Watches the **"Country/Region you are applying visa from"** dropdown on the
Indian visa registration form and alerts you the instant Bangladesh reappears
in it (i.e. when the BD quota opens again).

Live dashboard: **https://bshults98.github.io/visa-monitor/** (or
`https://visa.forkdeals.com` once the DNS record below is added)

---

## How it works

The whole form is returned by **one POST** — no passport number, no name, no
Continue button, no security question, and no CAPTCHA are needed for a
read-only availability check:

```
GET  /visa/Registration                       -> session cookies
POST /visa/Registration  service_requested=1  -> full form HTML (~230 KB)
```

The monitor then parses **only** `<select name='appl.countryname'>` — the
dropdown whose label is exactly *"Country/Region you are applying visa from"*.

> **False-positive trap.** Two *other* selects on the same page contain the
> string `BANGLADESH`:
> * `appl.purpose` — visa purpose text, e.g. "…(FOR BANGLADESHI NATIONALS ONLY)"
> * `gate_nationality` — the nationality gate
>
> A naive `if "BANGLADESH" in html` would report AVAILABLE on **every single
> poll**. The matcher is deliberately scoped to the one dropdown that matters.

---

## Where it runs

| Layer | What |
|---|---|
| **Checker** | GitHub Actions on a public repo (unlimited free minutes). Azure egress reaches the target — this was probed and confirmed before committing to the host; Cloudflare egress is blocked by the site's WAF. |
| **State** | `data` branch — `samples.jsonl`, `transitions.jsonl`, `state.json`. Force-pushed as a single parentless commit each run so the branch never grows. |
| **Dashboard** | GitHub Pages, deployed from a rendered static `index.html`. |
| **Alerts** | Webhook (`WEBHOOK_URL` secret) |

### Schedule

All times below are **Dhaka time (Asia/Dhaka, UTC+6)**.

GitHub's shortest cron interval is **5 minutes**, and scheduled runs get
delayed or dropped when many short runs queue up. So the monitor uses
**few long jobs instead of many short ones**: one job every 2 hours
(`7 */2 * * *` UTC), each looping internally for 2 h 10 min.

The job picks its own poll interval:

* **60-90 s** while inside a reset window (00:00-00:30 and 08:00-09:30 Dhaka)
* **15 min** the rest of the time

Consecutive jobs overlap by 10 minutes, so a delayed start never leaves a
gap, and both reset windows are fully covered.

> The actual reset hour is **community folklore and unverified** — the site
> exposes no quota, reset time, or slot count. The dashboard's hour-of-day
> heatmap *learns* the real times from data, so check it after a week and
> adjust `FAST_WINDOWS` / the cron triggers to match what it shows.

---

## Dashboard is encrypted

The published HTML contains **no readable data**. The payload is
AES-256-GCM encrypted with a key derived from a passphrase via PBKDF2
(200k iterations). The browser decrypts it client-side with WebCrypto; the
plaintext never appears in the served bytes, so `curl`, view-source and
GitHub's file viewer all show ciphertext.

When a passphrase is set, `status.json` / `status.txt` are **not written at
all** — they would otherwise be served in cleartext next to the lock and
bypass it entirely.

**Passphrase:** see the `DASH_PASSPHRASE` repository secret, or ask the
operator. Change it any time:

```bash
gh secret set DASH_PASSPHRASE
```

---

## Webhook payloads

Every payload carries a `summary` field — a single sentence that reads
correctly on its own, so webhook.site / Slack / Telegram previews are useful
without parsing anything.

| Event | When |
|---|---|
| `BANGLADESH_AVAILABLE` | BD appeared in the dropdown — the one that matters |
| `BANGLADESH_UNAVAILABLE` | BD dropped back out |
| `MONITOR_HEARTBEAT` | Periodic liveness signal |
| `MONITOR_BLOCKED` | Consecutive failures (WAF / structure change) |
| `MONITOR_STARTED` | Process came up |
| `MONITOR_TEST` | Connectivity smoke test |

Example:

```json
{
  "event": "BANGLADESH_AVAILABLE",
  "summary": "✅ BANGLADESH is BACK in the appl.countryname dropdown after being offline for 6h 42m. Apply now -- option 149 on the list, 3 flip(s) today, 7d uptime 41.7%.",
  "country": "BANGLADESH",
  "state": "ONLINE",
  "option_count": 149,
  "was_offline_for": "6h 42m",
  "flips_today": 3,
  "action": "Apply now -- open the Regular/Paper Visa form",
  "url": "https://indianvisaonline.gov.in/visa/Registration"
}
```

---

## Repo layout

```
monitor.py                 entry point: check, notify, schedule, render
analytics.py               append-only sample/transition log, rollup, prune
notifier.py                webhook payload builders
dashboard.py               static dashboard + AES-GCM render
requirements.txt           requests, cryptography
.github/workflows/
  monitor.yml              scheduled checker + Pages deploy
  probe.yml                one-shot egress reachability test
```

## Running locally

```bash
pip install -r requirements.txt

export WEBHOOK_URL="https://webhook.site/<your-uuid>"   # optional
export DASH_PASSPHRASE="your-dashboard-passphrase"       # optional

python monitor.py --once            # single check, record, render, exit
python monitor.py                   # long-running loop (daemon)
python monitor.py --report          # plain-English trend summary
python monitor.py --render          # re-render dashboard from logs only
python monitor.py --test-webhook    # send a test event and exit
python monitor.py --rearm           # reset the one-shot notification
python monitor.py --burst-min 45 --force-fast   # poll fast for 45 min
```

Config via environment variables (all optional, defaults shown):

| Var | Default | Meaning |
|---|---|---|
| `WEBHOOK_URL` | *(empty = disabled)* | Where alerts are POSTed |
| `DASH_PASSPHRASE` | *(empty = unencrypted)* | Encrypts the published dashboard |
| `TARGET_COUNTRY` | `BANGLADESH` | Which country to watch |
| `SELECT_NAME` | `appl.countryname` | Which dropdown to parse |
| `FAST_WINDOWS` | `00:00-00:30,08:00-09:30` | Dhaka-time windows for fast polling |
| `FAST_MIN` / `FAST_MAX` | `60` / `90` | Fast poll interval (seconds) |
| `BASE_INTERVAL` | `900` | Baseline poll interval (seconds) |
| `RETENTION_DAYS` | `45` | Samples older than this are pruned |
| `HEARTBEAT_HOURS` | `6` | Heartbeat webhook cadence |
| `PAGES_CNAME` | *(empty)* | Custom domain written into `CNAME` |

## Analytics & logs

Everything needed to analyse *exactly* when BD came online and went offline:

* `data/samples.jsonl` — one line per poll: `ts`, `state`, `available`,
  `option_count`, `latency_ms`, `error`.
* `data/transitions.jsonl` — one line per state change, with the exact
  timestamp the previous state began and how long it held.

States are `ONLINE` / `OFFLINE` / `UNKNOWN`. **`UNKNOWN` is excluded from
availability maths** — treating a site outage as "BD closed" would corrupt
every uptime figure.

`python monitor.py --report` prints a plain-English summary; `--report --json`
emits the full rollup (daily trend, hour-of-day heatmap, streaks, transitions).

---

## Notes & limits

* The monitor is **detect + notify only**. It does not auto-submit the
  application form — that needs CAPTCHA solving and would mean auto-hoarding
  quota on a government service.
* Polling is deliberately gentle (15-min baseline, 60–90 s only inside reset
  windows) and backs off exponentially on errors.
* No PII is stored or transmitted — the passport number was never needed and
  is not in the repo.
