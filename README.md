# Flight Price Watcher

Checks a flight route on a schedule and pushes a notification to your
iPhone (via [ntfy](https://ntfy.sh)) when the price drops under a
threshold you set.

## A note on data sources (read this first)

This project's data source has changed twice:

1. **Originally Amadeus.** Amadeus shut down self-service API access on
   July 17, 2026 — no free developer tier exists anymore.
2. **Then Duffel.** Duffel is a real, self-serve API, but real (live)
   prices require activating your account, which involves identity
   verification (Duffel asks for things like a Social Security Number,
   since they're a regulated payments/booking platform). Duffel's
   *test-mode* keys skip that verification but only return **fake,
   synthetic sandbox prices** — not real fares. This is a reasonable
   thing to want to avoid for a personal, search-only project.
3. **Now: SerpApi (Google Flights) is the primary source.** It returns
   real, live prices, requires no identity verification, and bills
   simple pay-per-search. Duffel is now optional — the script only uses
   it if you've completed Duffel's live-mode activation yourself and
   provide a genuine `duffel_live_` key. If you provide a `duffel_test_`
   key, the script detects it and skips Duffel automatically (rather
   than silently reporting fake prices as real, which is what caused
   the price-mismatch issue in earlier versions of this project).

## How it works

- A Python script queries [SerpApi's Google Flights engine](https://serpapi.com/google-flights-api)
  for real, live pricing on your route.
- (Optional) If you've set up a genuine live Duffel key, it's tried
  first as an alternate real-data source.
- It compares the cheapest fare found to your `MAX_PRICE` (converting
  currency if needed, via the free [Frankfurter](https://frankfurter.dev)
  exchange-rate API).
- If it's under budget *and* lower than the last price you were notified
  about, it sends a push notification via ntfy, with a Kayak link to the
  route so you can book it.
- GitHub Actions runs this on a schedule (default: every 8 hours) for free.

## One-time setup

### 1. Get a SerpApi key (required)
1. Sign up at https://serpapi.com/ — 100 free searches/month included,
   no identity verification needed.
2. Grab your API key from the dashboard.

**Cost note:** the free tier is 100 searches/month. This project's
default schedule (every 8 hours, ~90 checks/month) is designed to stay
just under that. If you want more frequent checks, either upgrade your
SerpApi plan or accept the per-search overage cost — check
https://serpapi.com/pricing for current rates before changing the
schedule.

### 2. (Optional) Get a Duffel API key
Only worth doing if you want a second, independent real-data source, or
if Duffel happens to have better coverage for a specific
route/carrier. **Skip this entirely if you don't want to go through
Duffel's identity verification** — the script works fine on SerpApi alone.

If you do want it:
1. Sign up at https://duffel.com/
2. To get *real* prices, you'll need to activate your account (their
   identity/KYC verification process — this is what asks for an SSN or
   equivalent). A `duffel_test_` token alone is not useful here since
   this script automatically skips test-mode keys.
3. Once live mode is activated, create a **live** access token (starts
   with `duffel_live_`) and use that as `DUFFEL_API_KEY`.

### 3. Set up ntfy on your iPhone
1. Install the **ntfy** app from the App Store (free).
2. In the app, subscribe to a topic name of your choosing — make it long
   and hard-to-guess since anyone who knows your topic name can post to
   it (e.g. `flightwatch-<random string>-xnaqla`, not just `flights`).
3. That topic name is what goes in `NTFY_TOPIC` below.

### 4. Push this project to a GitHub repo
```
cd flight-watcher
git init
git add .
git commit -m "Initial commit"
# create a repo on github.com, then:
git remote add origin https://github.com/<you>/flight-watcher.git
git push -u origin main
```

### 5. Add secrets and variables in GitHub
Go to your repo → **Settings → Secrets and variables → Actions**.

**Secrets** (Repository secrets tab):
| Name | Value |
|---|---|
| `SERPAPI_API_KEY` | your SerpApi key (required) |
| `DUFFEL_API_KEY` | your Duffel **live** access token (optional — omit entirely if you're not using Duffel) |
| `NTFY_TOPIC` | your ntfy topic name |

**Variables** (Repository variables tab):
| Name | Example |
|---|---|
| `ORIGIN` | `XNA` |
| `DESTINATION` | `LAX` |
| `DEPART_DATE` | `2026-11-20` |
| `RETURN_DATE` | `2026-11-27` (omit/leave blank for one-way) |
| `MAX_PRICE` | `250` |
| `CURRENCY` | `USD` (the currency you want alerts/threshold in) |

Airport codes are 3-letter IATA codes (e.g. XNA = Northwest Arkansas
Regional, LAX = Los Angeles).

### 6. Test it
Go to the **Actions** tab in your repo → "Flight Price Watcher" →
**Run workflow** to trigger it manually and confirm you get a
notification (temporarily set `MAX_PRICE` very high, like `99999`, to
force a test notification, then set it back to your real budget).

## Price history database

Every run logs the price it found — not just the ones that trigger a
notification — to a SQLite database, `flight_watcher.db`, which lives in
the repo and is committed back after each run. The database itself is
also used to figure out whether a new check is a new low worth notifying
about (no separate state file needed).

**Schema** (table `price_checks`):

| Column | Meaning |
|---|---|
| `checked_at` | UTC timestamp of the check |
| `origin`, `destination`, `depart_date`, `return_date` | the route/dates searched |
| `source` | `Duffel` or `Google Flights (via SerpApi)` |
| `native_price`, `native_currency` | the raw price as returned by the source |
| `converted_price`, `target_currency` | that price converted into your `CURRENCY` |
| `max_price` | the threshold that was active at the time |
| `notified` | 1 if this check triggered a push notification, 0 otherwise |

**Querying it yourself:** download `flight_watcher.db` from the repo (or
pull the repo locally) and open it with any SQLite tool — e.g. the
[DB Browser for SQLite](https://sqlitebrowser.org/) GUI, or the command
line:
```
sqlite3 flight_watcher.db "SELECT checked_at, source, converted_price, target_currency FROM price_checks ORDER BY checked_at DESC LIMIT 20;"
```
This is handy for plotting a price-history chart later, or just eyeballing
how much a route fluctuates over time.

**Note:** since the database is a binary file, `git diff` won't show
readable changes to it — that's expected, just query the database
itself to inspect the data rather than reading the git history.

## Watching multiple routes

Duplicate the workflow file (e.g. `watch-route2.yml`) with different
variable names (`ORIGIN_2`, `DESTINATION_2`, etc.) and matching env vars
in the script call, or turn the route list into a JSON file and loop
over it in the script — happy to build that out if you want to track
more than one route. Keep the SerpApi free-tier math in mind (100
searches/month split across however many routes you're watching).

## Notes & limitations

- This does not book anything — it just notifies you and gives you a
  Kayak link to check and book manually.
- Round-trip pricing via SerpApi requires a two-step search (Google
  Flights' own flow), which the script handles for you automatically.
- The booking link in each notification points to Kayak, since Google's
  natural-language flight search links can sometimes serve a
  restricted/verification page instead of results when opened from an
  in-app browser. If the Kayak link ever asks you to verify you're
  human, opening it in Safari instead of staying in the ntfy app usually
  clears it.
- If SerpApi's pricing or coverage doesn't fit your needs, other
  legitimate options as of 2026 include a full travel-API aggregator
  (e.g. Ignav, built for exactly this post-Amadeus migration gap).
