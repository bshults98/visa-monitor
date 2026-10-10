# Findings — BD visa dropdown monitor

Written 2026-10-10 so a new session can skip the expensive discovery phase.
Everything below was measured, not assumed. Where a claim is unverified it is
marked as such.

---

## 1. The target site

`https://indianvisaonline.gov.in/visa/Registration`

* **Not behind Cloudflare.** Origin is `164.100.129.11` (NDCSP-Delhi, NIXI —
  raw Indian government hosting).
* Behind **F5 BIG-IP / ASM** bot defence. Signatures: `BNES_JSESSIONID`,
  `BNES_IVFRT_Cookie`, `IVFRT_Cookie`.
* Aggressively fingerprints **both IP reputation and TLS client**.

### Egress matrix (measured)

| Source | Result |
|---|---|
| Home IP (Link3 Dhaka, `27.147.200.119`) | **200 OK** |
| Python `requests` / `httpx` / `urllib` (direct) | **200 OK** |
| PowerShell `.NET` `Invoke-WebRequest` | **200 OK** |
| `curl` (Windows schannel) | TLS handshake **fails** (JA3 fingerprint) |
| Decodo ISP proxies (all 10 ports) | **403** |
| `corsproxy.io` (Cloudflare egress) | **403** |
| `allorigins` (Cloudflare egress) | **522** |
| `r.jina.ai` | 200 on retry, 403 first try (inconsistent) |
| **GitHub Actions (Azure `172.183.135.156`)** | **200 OK** ✅ |

**Conclusion:** the WAF blocks *known proxy/datacenter ranges*, not datacenter
IPs as a class. GitHub Actions Azure egress works. Cloudflare egress does not.
This is the single most important fact for choosing a host.

---

## 2. The site can be read in ONE POST

No passport number, no name, no Continue button, no security question, no
CAPTCHA is needed for a **read-only availability check**:

```
GET  /visa/Registration                      -> session cookies (JSESSIONID, IVFRT_Cookie)
POST /visa/Registration  service_requested=1 -> full form HTML, ~230 KB
       (also send: Referer + Origin headers, requestedPage= empty)
```

Posting without a prior GET gets bounced to `index.html` (302). Posting without
Referer/Origin gets a WAF 403.

The whole form is server-rendered in that one response — 6 `<select>` elements
are all inline.

### FALSE-POSITIVE TRAP (important)

The target dropdown is **`<select name='appl.countryname'>`** — label is
literally *"Country/Region you are applying visa from"*. ~148 options.

But **two other selects on the same page contain the string `BANGLADESH`**:

* `appl.purpose` — visa purpose text, e.g. "…(FOR BANGLADESHI NATIONALS ONLY)"
* `gate_nationality` — the nationality gate (262 options)

A naive `if "BANGLADESH" in html` reports **AVAILABLE on every single poll**.
The matcher MUST be scoped to `appl.countryname` only.

Matching rule that works: normalize whitespace, compare option text to
`BANGLADESH` OR option value to `BGD` (ISO alpha-3; the dropdown uses codes
like `DZA`, `AGO`, `NPL`, `PAK`).

Option shape in the HTML: `<option value='NPL'>NEPAL` (single or double
quotes, 1–2 spaces after `option`, whitespace/newlines around the text).

### Current observed state

BD is **absent** from `appl.countryname` (148 options) on every check.
`gate_nationality` and `appl.purpose` do contain BANGLADESH — do not confuse
them with the target.

---

## 3. GitHub Actions scheduling (the hard part)

This is where most of the time went. Findings from live testing.

### 3.1 GitHub throttles high-frequency schedules

Measured on this account (`bshults98`):

| Schedule | Expected runs/day | Actually observed |
|---|---|---|
| `*/5 * * * *` (smg-backend) | 288 | **~3/day** |
| `7,22,37,52 * * * *` (visa-monitor) | 96 | **~12/day** |
| `0 14` / `0 2` (bhoot-fm-archive) | 2 | **2/day (honored)** |
| `7 */2 * * *` | 12 | **0 — never fired** |
| `7,37 * * * *` | 48 | **0 in 45 min** |

**Pattern: low frequency is honored; high frequency is dropped/delayed.**
Observed delays of up to 5 hours on cron targets (bhoot's `0 14` fired at
19:05, `0 2` fired at 08:05).

### 3.2 Step values in the HOUR field never fire

`7 */2 * * *` produced zero runs in 5+ hours. The patterns that DID fire all
use either `*` or literal hours in the hour field:

* ✅ `7,22,37,52 * * * *` (hour = `*`)
* ✅ `55 17 * * *` (literal hour)
* ✅ `0 14 * * *` (literal hour)
* ❌ `7 */2 * * *` (step in hour)

Minute-field steps (`*/5`) do fire — the problem is specifically step values
in the **hour** field.

### 3.3 New-repo registration lag

A brand-new repo's schedule took **~4h20m** to register (first workflow pushed
17:31, first scheduled run 21:51). `push` and `workflow_dispatch` work
instantly.

### 3.4 `concurrency` group cancels queued runs

With `concurrency: group: X, cancel-in-progress: false`, GitHub keeps **one
running + one pending**. A third queued run cancels the older pending one.
So a long job silently deletes everything queued behind it.

This is fine if the long job IS the coverage (it polls internally), but it
destroys a "many short runs" design.

### 3.5 The design that survives all of the above

**Few triggers + long jobs.**

```
cron: "1 0,2,4,6,8,10,12,14,16,18,20,22 * * *"   # 12/day, explicit hours
job:  python monitor.py --burst-min 330           # loops 5h30m internally
timeout-minutes: 360
```

Key property: **job duration (330 min) > trigger interval (120 min)**, so one
job spans 2.75 trigger intervals and absorbs up to 2 consecutive dropped
triggers. Simulated 50% drop rate over 3 days → **99% coverage**, all Dhaka
reset windows still covered.

The job must pick its own poll interval internally (cron cannot do 60–90 s —
GitHub's floor is 5 min).

### 3.6 Other gotchas

* GitHub delays/queues scheduled runs at the **top of every hour**. Put cron
  minutes at odd offsets (:01, :37, etc.).
* `GITHUB_TOKEN` pushes do **not** trigger other workflows (recursion guard).
* Public repo = **unlimited free Actions minutes**. Private = 2,000/month,
  which a 24/7 monitor burns in days.
* Max job execution time on GitHub-hosted runners = **6 hours**.
* A workflow change on a repo re-triggers schedule registration.

---

## 4. What is built and working

Repo: `https://github.com/bshults98/visa-monitor` (public)

| File | Purpose |
|---|---|
| `monitor.py` | entry point: check, notify, schedule, render. `--once`, `--burst-min`, `--force-fast`, `--render`, `--report`, `--test-webhook`, `--rearm` |
| `analytics.py` | append-only `samples.jsonl` + `transitions.jsonl`, rollup (uptime, trend, hour-of-day heatmap, streaks), retention prune |
| `notifier.py` | webhook payloads with a human-readable `summary` line |
| `dashboard.py` | static HTML dashboard, **AES-256-GCM encrypted** behind a passphrase |
| `.github/workflows/monitor.yml` | hosted checker + Pages deploy |
| `README.md` | usage + architecture |

### Verified behaviours

* **Detection is correct** — transition fired when BD was simulated present,
  `armed`/re-arm logic works, no double-notifications across process restarts.
* **Span math is correct** — `span_seconds` computes the *previous* state's
  duration. (Bug fixed: it originally computed before the append and returned 0.)
* **UNKNOWN state excluded from uptime** — a site outage is not counted as
  "BD closed", so percentages stay honest.
* **Dashboard encryption works** — tested in a real browser: lock screen shows,
  wrong passphrase rejected, correct one decrypts, session persists on reload,
  fresh context re-locks. `curl` of the page shows ciphertext only. `status.json`
  is withheld entirely when a passphrase is set (it would bypass the lock).
* **Times render in Asia/Dhaka** regardless of viewer timezone — verified by
  loading with the browser forced to `America/New_York`.
* `ruff` + `mypy` clean.

### Deliberate scope limit

Detect + notify only. No auto-submit. Auto-submitting a government form needs
CAPTCHA solving and would mean hoarding quota slots. The webhook is designed
to get a human to the form the instant BD appears.

---

## 5. What is NOT solved

### 5.1 Schedule reliability (the reason for stopping)

GitHub's throttling/delay means the monitor does not poll on the intended
cadence. The "few triggers + long jobs" design (§3.5) is the fix but has **not
been proven in production yet** — it was pushed at 15:23 UTC and the first
trigger is 16:01 UTC. A new session should check whether those runs fire.

### 5.2 Dashboard refresh cadence

`site/` is rebuilt and deployed only at **job end**, so the live dashboard lags
by up to a job's duration (~2h with the current design). Webhooks are instant;
the dashboard is not. To fix: either render+deploy periodically inside the
loop, or switch Pages to a git-branch source and push `site/` on each data
commit.

### 5.3 Reset hours are unverified folklore

The site exposes **no** quota, no reset time, no slot count, no server-clock
display. The `00:00–00:30` and `08:00–09:30` Dhaka windows come from a
third-party claim (Gemini) that could not be confirmed. **The hour-of-day
heatmap on the dashboard is meant to discover the real pattern from data** —
after a week of runs, adjust `FAST_WINDOWS` and the cron to match what it
shows.

---

## 6. Approaches worth trying in a new session

Ranked by expected reliability.

### A. Cloudflare Worker with a cron trigger
Cron triggers are designed for this and are reliable — no throttling like
GitHub's. Two sub-options:

* **A1. Worker as dispatcher only** — the Worker's cron POSTs to the GitHub
  API to `workflow_dispatch` `visa-monitor`. Keeps the Python monitor intact.
  Needs a PAT with `actions:write` stored in Worker secrets, plus `wrangler
  login`.
* **A2. Worker does the whole check** — port the checker to JS and run it in
  the Worker. Needs to verify Worker egress can reach the site (**untested**;
  Cloudflare's public proxies got 403, but Worker egress may differ).

### B. Termux on an Android phone
The home IP is known-good and a phone can stay on 24/7 charging. Free, no
egress risk, no GitHub throttling. Only downside: the phone must stay powered.
The monitor is pure `requests` + stdlib, so it runs on Android unmodified.
This was scoped earlier and is ready to build.

### C. External cron service dispatching GitHub Actions
`cron-job.org`, UptimeRobot, etc. can POST to the GitHub dispatch endpoint on
a real schedule. Reliable clock, no GitHub throttling. Downside: the PAT lives
in a third-party account.

### D. Oracle Cloud Always Free VM
Full VM, cron works properly, runs the Python monitor as-is. Needs signup +
card. Datacenter egress to the site is unproven (would need a probe first).

### E. Contabo VPS (the forkdeals.com box)
`207.244.245.9`. It is alive (Cloudflare Tunnel serves `forkdeals.com`) but
**SSH is unreachable from the user's current network** — all ports timeout,
tracert dies at Lumen transit. `webroot` says "leave it", but it is reachable
from a different network (phone hotspot / VNC console in the Contabo panel).
If SSH can be regained, this is the most reliable option: pm2 + cron + SQLite.

### F. Do nothing — accept the current design
If the 12/day long-job design (§3.5) turns out to fire reliably once
registered, it is already deployed and needs no further work. **Check this
first** before building anything new.

---

## 7. Gotchas a new session must not re-discover

1. **Never grep the raw page for "BANGLADESH"** — see §2. Scope to
   `appl.countryname`.
2. **Do not use `curl`/schannel** for this site — TLS fingerprint is blocked.
   Python `requests` works.
3. **Do not put step values in the cron hour field** (`*/2`) — never fires.
4. **Do not use many short scheduled runs** — GitHub throttles them. Use few
   triggers + long jobs.
5. **`concurrency` with `cancel-in-progress: false`** keeps only 1 pending —
   long jobs delete queued short ones.
6. **`timezone.utc` is an instance, not a class** — write `datetime.now(timezone.utc)`,
   not `datetime.now(timezone.utc())`.
7. **Windows Python needs `tzdata` for `zoneinfo`** — this project uses fixed
   UTC offsets instead (`DHAKA = timezone(timedelta(hours=6))`). Bangladesh
   and India have no DST.
8. **Windows consoles are cp1252** — emoji in webhook summaries crash `print`.
   Call `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.
9. **GitHub Actions secrets use libsodium SealedBox (Curve25519, 32-byte key)**,
   not RSA-OAEP. Use `nacl.public.SealedBox` with the base64-decoded key.
10. **A passphrase on the dashboard must also gate `status.json`** — otherwise
    the JSON is served publicly and bypasses the lock.
11. **If you render a dashboard with synthetic test data, delete the output
    immediately.** A test render was mistaken for the live dashboard, which
    caused a confusing detour.

---

## 8. Credentials / secrets (as of 2026-10-10)

* **Dashboard passphrase:** `ember-onyx-thicket-beacon-83`
* **Webhook URL:** `https://webhook.site/b4709261-68c9-4931-91ac-80e3afdccf0f`
  (a test endpoint — anyone with the UUID can read it; swap for Discord/Telegram
  before trusting it)
* **GitHub secret `WEBHOOK_URL`** — set in `bshults98/visa-monitor`
* **GitHub secret `DASH_PASSPHRASE`** — set in `bshults98/visa-monitor`
* **GitHub secret `VISA_MONITOR_TOKEN`** — a fine-grained PAT set in
  `bshults98/smg-backend` (used only by the dispatcher step). Revoke at
  https://github.com/settings/tokens if no longer needed.
* A broad OAuth token (`gho_…`, `repo`+`gist`+`workflow` scope) lives in the
  Windows Credential Manager as `git:https://github.com`. It was used
  transiently for setup only and is **not** stored in any repo.

**smg-backend `check.yml` has an added step** ("Trigger visa-monitor") at the
end of the `check` job. It is `continue-on-error: true`, so it cannot break
the shop workflow. Remove that one step to fully decouple the two projects.

---

## 9. Immediate next step for a new session

```bash
cd "E:\Test OCODE\Indian VISA SLOT GRABBER"
# did the 12/day long-job schedule register and fire?
gh api repos/bshults98/visa-monitor/actions/runs --jq '.workflow_runs[:5]'
```

If scheduled runs appear, everything is done — just tune `FAST_WINDOWS` from
the dashboard's heatmap once a week of data exists.

If not, go to §6 option **B (Termux)** — it has zero egress risk and zero
scheduling risk, and the Python monitor already runs unchanged on Android.
