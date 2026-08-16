# Flight Price Watcher

Checks a flight route on a schedule and pushes a notification to your
iPhone (via [ntfy](https://ntfy.sh)) when the price drops under a
threshold you set.

## A note on Amadeus

This was originally built on the Amadeus Self-Service API. **Amadeus
shut down self-service access on July 17, 2026** — the free developer
tier no longer exists, only an enterprise/commercial tier does. This
version has been migrated to **[Duffel](https://duffel.com)** instead,
which currently has self-serve signup and a pay-as-you-go model that's
effectively free at this project's scale (see Costs, below).

## How it works

- A Python script creates a flight search ("offer request") via the
  [Duffel Flights API](https://duffel.com/docs/guides/getting-started-with-flights)
  — a real, self-serve, ToS-compliant API with direct airline
  connections, not scraping.
- It compares the cheapest fare found to your `MAX_PRICE` (converting
  currency if needed, via the free [Frankfurter](https://frankfurter.dev)
  exchange-rate API).
- If it's under budget *and* lower than the last price you were notified
  about, it sends a push notification via ntfy.
- GitHub Actions runs this on a schedule (default: every 2 hours) for free.

## One-time setup

### 1. Get a Duffel API key
1. Sign up at https://duffel.com/ (self-serve, no sales call needed).
2. In your Duffel dashboard, create an **access token**. Start with a
   **test mode** token (starts with `duffel_test_`) — test mode returns
   realistic-looking offers without touching real bookings, which is
   all this script needs. Duffel's docs cover the difference between
   test and live mode if you ever want to go further.
3. Note your account's **settlement currency** (set in your dashboard) —
   this is the currency Duffel will return prices in, regardless of
   what you set as `CURRENCY` below. The script converts automatically.

### 2. (Optional) Get a SerpApi key for the Google Flights fallback
If Duffel comes back empty for your route (common for small/regional
carriers it doesn't have a direct connection to), the script can fall
back to Google Flights results via [SerpApi](https://serpapi.com/).
1. Sign up at https://serpapi.com/ — 100 free searches/month included.
2. Grab your API key from the dashboard.
3. This is optional — if you skip it, the script just reports "no
   offers found" on routes Duffel doesn't cover, instead of falling back.

**Cost note:** the free tier is 100 searches/month. A route checked
every 2 hours is 12 checks/day (~360/month) — fine if the fallback is
rarely triggered (Duffel usually has your route covered), but if Duffel
*never* finds your specific route and every run falls through to
SerpApi, you'll exceed the free tier and start incurring charges. Worth
running it for a day or two and checking your SerpApi dashboard usage
before leaving it unattended for weeks.

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
| `DUFFEL_API_KEY` | your Duffel access token |
| `SERPAPI_API_KEY` | your SerpApi key (optional — omit to skip the fallback) |
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

## Costs

Duffel doesn't charge for searches on their own — you're billed per
confirmed *booking* (this script never books anything). There's a small
excess-search fee (a fraction of a cent) that only kicks in once your
search-to-booking ratio passes 1,500:1, which a script checking one
route every 2 hours won't come close to. In practice this should cost
you $0.

## On currency (why multi-currency checking is gone)

Amadeus let you request prices in any currency per search, so an
earlier version of this script checked several currencies and picked
the cheapest after conversion — catching cases where currency
conversion quirks worked in your favor.

Duffel doesn't support that: it returns prices in your account's fixed
settlement currency only. This script still converts that price into
your target `CURRENCY` for comparison/display, but it can no longer
"shop around" across currencies in a single search — that trick simply
isn't available through this API.

## Price history database

Every run logs the price it found — not just the ones that trigger a
notification — to a SQLite database, `flight_watcher.db`, which lives in
the repo and is committed back after each run (same mechanism the old
`state.json` used). This also replaced `state.json` entirely: the
database itself is now used to figure out whether a new check is a new
low worth notifying about.

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
sqlite3 flight_watcher.db "SELECT checked_at, converted_price, target_currency FROM price_checks ORDER BY checked_at DESC LIMIT 20;"
```
This is handy for plotting a price-history chart later, or just eyeballing
how much a route fluctuates over time.

**Note:** since the database is a binary file, `git diff` won't show
readable changes to it the way it did with `state.json`'s JSON — that's
expected and fine, just query the database itself to inspect the data
rather than reading the git history.

## Watching multiple routes

Duplicate the workflow file (e.g. `watch-route2.yml`) with different
variable names (`ORIGIN_2`, `DESTINATION_2`, etc.) and matching env vars
in the script call, or turn the route list into a JSON file and loop
over it in the script — happy to build that out if you want to track
more than one route.

## Notes & limitations

- This does not book anything — it just notifies you and gives you a
  link to check.
- Duffel's coverage depends on which airlines it has direct (NDC) or
  aggregator connections with; very small/regional carriers may not
  always appear. When Duffel finds nothing, and `SERPAPI_API_KEY` is
  set, the script automatically falls back to Google Flights results
  via SerpApi (see "Notification" logs/messages for which source found
  the fare — it's labeled either "Duffel" or "Google Flights (via
  SerpApi)").
- Round-trip pricing via the SerpApi fallback requires a two-step
  search (Google Flights' own flow), which the script handles for you.
- If Duffel's or SerpApi's pricing model changes or doesn't fit your
  use case, other legitimate options as of 2026 include a full travel-
  API aggregator (e.g. Ignav, built for exactly this post-Amadeus gap).
