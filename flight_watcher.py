#!/usr/bin/env python3
"""
flight_watcher.py

Checks flight prices — primarily via Google Flights (through SerpApi) —
and sends an ntfy push notification to your phone when the price drops
under a threshold.

Duffel is supported as an optional secondary source, but only if you've
activated live mode there (a real identity/KYC process, since Duffel is
a regulated booking platform). A Duffel *test-mode* key is detected and
skipped automatically, since test-mode prices are synthetic sandbox
data, not real fares — using them would silently report fake prices.

Designed to be run on a schedule (e.g. via GitHub Actions cron). Every
check — not just the ones that notify — is logged to a SQLite database
(flight_watcher.db) so you have a full price history to query later, and
that same database is used to figure out whether a new check is a new
low (so we don't spam you every run).
"""

import os
import sqlite3
import sys
from datetime import datetime

import requests

DUFFEL_TEST_KEY_PREFIX = "duffel_test_"

DB_FILE = "flight_watcher(sept).db"
DUFFEL_API_BASE = "https://api.duffel.com"
DUFFEL_VERSION = "v2"


def get_env(name, required=True, default=None):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def init_db(db_path):
    """
    Creates the price_checks table if it doesn't already exist. Every run
    of the script logs one row here — a full history of every price this
    script has ever observed, not just the ones that triggered a notification.
    """
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at TEXT NOT NULL,        -- UTC ISO 8601 timestamp of this check
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            depart_date TEXT NOT NULL,
            return_date TEXT NOT NULL DEFAULT '',  -- '' for one-way trips
            source TEXT NOT NULL,            -- "Duffel" or "Google Flights (via SerpApi)"
            native_price REAL NOT NULL,
            native_currency TEXT NOT NULL,
            converted_price REAL NOT NULL,   -- native_price converted into target_currency
            target_currency TEXT NOT NULL,
            max_price REAL NOT NULL,         -- the MAX_PRICE threshold active at check time
            notified INTEGER NOT NULL DEFAULT 0  -- 1 if this check triggered a notification
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_price_checks_route
        ON price_checks (origin, destination, depart_date, return_date)
        """
    )
    conn.commit()
    return conn


def log_price_check(conn, *, origin, destination, depart_date, return_date,
                     source, native_price, native_currency,
                     converted_price, target_currency, max_price, notified):
    """Inserts one row for this run's price check, regardless of whether it notified."""
    conn.execute(
        """
        INSERT INTO price_checks (
            checked_at, origin, destination, depart_date, return_date,
            source, native_price, native_currency,
            converted_price, target_currency, max_price, notified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.utcnow().isoformat(),
            origin,
            destination,
            depart_date,
            return_date or "",
            source,
            native_price,
            native_currency,
            converted_price,
            target_currency,
            max_price,
            1 if notified else 0,
        ),
    )
    conn.commit()


def get_last_notified_price(conn, origin, destination, depart_date, return_date):
    """
    Returns the converted_price of the most recent check for this exact route
    that triggered a notification, or None if we've never notified for it.
    """
    row = conn.execute(
        """
        SELECT converted_price FROM price_checks
        WHERE origin = ? AND destination = ? AND depart_date = ? AND return_date = ?
          AND notified = 1
        ORDER BY checked_at DESC
        LIMIT 1
        """,
        (origin, destination, depart_date, return_date or ""),
    ).fetchone()
    return row[0] if row else None


def search_flights(api_key, origin, destination, depart_date, return_date, adults):
    """
    Creates a Duffel offer request and returns the list of offers.
    See: https://duffel.com/docs/api/offer-requests/create-offer-request
    """
    slices = [{"origin": origin, "destination": destination, "departure_date": depart_date}]
    if return_date:
        slices.append({"origin": destination, "destination": origin, "departure_date": return_date})

    body = {
        "data": {
            "slices": slices,
            "passengers": [{"type": "adult"} for _ in range(adults)],
            "cabin_class": "economy",
        }
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "Duffel-Version": DUFFEL_VERSION,
        "Authorization": f"Bearer {api_key}",
    }

    resp = requests.post(
        f"{DUFFEL_API_BASE}/air/offer_requests",
        params={"return_offers": "true", "supplier_timeout": 15000},
        headers=headers,
        json=body,
        timeout=30,
    )
    if not resp.ok:
        print(f"Duffel API error {resp.status_code}: {resp.text}", file=sys.stderr)
        resp.raise_for_status()

    data = resp.json()["data"]
    return data.get("offers", [])


def cheapest_offer(offers):
    if not offers:
        return None
    return min(offers, key=lambda o: float(o["total_amount"]))


def search_google_flights_fallback(api_key, origin, destination, depart_date, return_date, currency, adults):
    """
    Falls back to SerpApi's Google Flights engine when Duffel has no offers
    (e.g. a small/regional carrier Duffel doesn't have a direct connection to).
    Returns (price, currency) or None if nothing usable is found.

    Round-trip pricing on Google Flights requires a two-step flow: the first
    search returns outbound options each with a `departure_token`; you then
    re-search with that token to get the actual combined round-trip price.
    See: https://serpapi.com/google-flights-api
    """
    base_params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": destination,
        "outbound_date": depart_date,
        "currency": currency,
        "adults": adults,
        "hl": "en",
        "gl": "us",
        "api_key": api_key,
    }
    if return_date:
        base_params["type"] = "1"  # round trip
        base_params["return_date"] = return_date
    else:
        base_params["type"] = "2"  # one-way

    try:
        resp = requests.get("https://serpapi.com/search", params=base_params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"SerpApi request failed: {e}", file=sys.stderr)
        return None

    options = data.get("best_flights", []) + data.get("other_flights", [])
    if not options:
        return None

    cheapest = min(
        (o for o in options if o.get("price") is not None),
        key=lambda o: o["price"],
        default=None,
    )
    if cheapest is None:
        return None

    if not return_date:
        return cheapest["price"], currency

    # Round trip: re-search using the departure_token to get the real total price
    token = cheapest.get("departure_token")
    if not token:
        # No token provided — fall back to the (possibly outbound-only) price we have
        return cheapest["price"], currency

    try:
        params2 = dict(base_params)
        params2["departure_token"] = token
        resp2 = requests.get("https://serpapi.com/search", params=params2, timeout=30)
        resp2.raise_for_status()
        data2 = resp2.json()
    except requests.RequestException as e:
        print(f"SerpApi round-trip follow-up request failed: {e}", file=sys.stderr)
        return cheapest["price"], currency

    options2 = data2.get("best_flights", []) + data2.get("other_flights", [])
    cheapest2 = min(
        (o for o in options2 if o.get("price") is not None),
        key=lambda o: o["price"],
        default=None,
    )
    if cheapest2 is None:
        return cheapest["price"], currency

    return cheapest2["price"], currency


def get_exchange_rates(base_currency):
    """
    Fetch current exchange rates for 1 unit of base_currency -> other currencies,
    using the free, keyless Frankfurter API (ECB data). Returns a dict like
    {"EUR": 0.92, "GBP": 0.79, ...}, or None if the call fails.
    """
    try:
        resp = requests.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"base": base_currency},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("rates", {})
    except requests.RequestException as e:
        print(f"WARNING: could not fetch exchange rates ({e}).", file=sys.stderr)
        return None


def convert(amount, from_currency, to_currency):
    """Convert `amount` in `from_currency` into `to_currency`. Returns None on failure."""
    if from_currency == to_currency:
        return amount
    rates = get_exchange_rates(from_currency)  # 1 from_currency -> X to_currency
    if not rates:
        return None
    rate = rates.get(to_currency)
    if not rate:
        return None
    return amount * rate


def send_ntfy_notification(topic, title, message, priority="default", ntfy_server="https://ntfy.sh"):
    # URL-encode the topic in case it has been mistyped with spaces/special
    # characters — ntfy topics must be plain alphanumerics/hyphens/underscores.
    from urllib.parse import quote
    url = f"{ntfy_server}/{quote(topic.strip(), safe='')}"
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
        "Tags": "airplane,moneybag",
    }
    resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
    resp.raise_for_status()


def main():
    # SerpApi (Google Flights) is the primary source: it's real, live pricing
    # with no identity/KYC verification required — a much better fit for a
    # personal, search-only project than a full booking platform.
    serpapi_key = get_env("SERPAPI_API_KEY")

    # Duffel is optional. It's only usable here if you've completed Duffel's
    # live-mode activation (their real identity verification process) and
    # are using a duffel_live_ token. A duffel_test_ token is detected and
    # skipped automatically below, since test-mode prices are synthetic
    # sandbox data, not real fares.
    api_key = get_env("DUFFEL_API_KEY", required=False, default=None)

    ntfy_topic = get_env("NTFY_TOPIC")
    ntfy_server = get_env("NTFY_SERVER", required=False, default="https://ntfy.sh")

    origin = get_env("ORIGIN")               # e.g. "XNA" (Northwest Arkansas Regional)
    destination = get_env("DESTINATION")      # e.g. "LAX"
    depart_date = get_env("DEPART_DATE")      # "YYYY-MM-DD"
    return_date = get_env("RETURN_DATE", required=False, default=None)
    max_price = float(get_env("MAX_PRICE"))   # interpreted in CURRENCY (your target currency)
    target_currency = get_env("CURRENCY", required=False, default="USD")
    adults = int(get_env("ADULTS", required=False, default="1"))

    db_conn = init_db(DB_FILE)

    native_price = None
    native_currency = None
    source = None

    # Try Duffel first ONLY if it's a genuine live-mode key.
    if api_key and api_key.startswith(DUFFEL_TEST_KEY_PREFIX):
        print(
            "DUFFEL_API_KEY is a TEST-mode token — its prices are synthetic sandbox "
            "data, not real fares. Skipping Duffel and using SerpApi (Google Flights) instead.",
            file=sys.stderr,
        )
    elif api_key:
        try:
            offers = search_flights(api_key, origin, destination, depart_date, return_date, adults)
            best = cheapest_offer(offers)
            if best:
                native_price = float(best["total_amount"])
                native_currency = best["total_currency"]
                source = "Duffel"
        except requests.RequestException as e:
            print(f"Duffel API request failed: {e}", file=sys.stderr)

    # Fall back to (or primarily use) SerpApi/Google Flights if Duffel didn't
    # produce a usable, real result.
    if native_price is None:
        fallback = search_google_flights_fallback(
            serpapi_key, origin, destination, depart_date, return_date, target_currency, adults
        )
        if not fallback:
            print("No offers found.")
            db_conn.close()
            return
        native_price, native_currency = fallback
        source = "Google Flights (via SerpApi)"

    if native_currency == target_currency:
        price = native_price
    else:
        price = convert(native_price, native_currency, target_currency)
        if price is None:
            print(
                f"WARNING: could not convert {native_currency} to {target_currency}; "
                f"comparing raw amounts instead.",
                file=sys.stderr,
            )
            price = native_price
            target_currency = native_currency

    print(f"[{source}] Cheapest offer: {native_price:.2f} {native_currency}"
          + (f" (~{price:.2f} {target_currency})" if native_currency != target_currency else ""))

    last_notified = get_last_notified_price(db_conn, origin, destination, depart_date, return_date)
    should_notify = price <= max_price and (last_notified is None or price < last_notified)
    actually_notified = False

    if should_notify:
        # Kayak's deep-link format is simple and documented, and — unlike
        # Google's `/travel/flights?q=...` natural-language links — reliably
        # lands on the specific route/dates rather than a restricted/verification
        # page. That said, Kayak (like most travel metasearch sites) does run
        # its own bot-detection, so an occasional "confirm you're human" check
        # is still possible, especially in an embedded in-app browser (like
        # ntfy's) rather than a normal Safari session. If that happens, opening
        # the link in Safari directly usually clears it.
        booking_link = f"https://www.kayak.com/flights/{origin}-{destination}/{depart_date}"
        if return_date:
            booking_link += f"/{return_date}"
        booking_link += f"/{adults}adults?sort=bestflight_a"

        title = f"✈️ {origin}→{destination} for ~{price:.0f} {target_currency}!"

        currency_note = ""
        if native_currency != target_currency:
            currency_note = (
                f"\n\n(Priced at {native_price:.2f} {native_currency}, "
                f"converted to {target_currency} at today's exchange rate for comparison.)"
            )

        message = (
            f"Found a fare under your {max_price:.0f} {target_currency} target.\n"
            f"Price: ~{price:.2f} {target_currency}\n"
            f"Source: {source}\n"
            f"Depart: {depart_date}"
            + (f"\nReturn: {return_date}" if return_date else "")
            + f"\n\nCheck it: {booking_link}"
            + "\n(If that link asks you to verify you're human, tap to open in Safari instead of staying in this app.)"
            + currency_note
        )
        try:
            send_ntfy_notification(ntfy_topic, title, message, priority="high", ntfy_server=ntfy_server)
            print("Notification sent.")
            actually_notified = True
        except requests.RequestException as e:
            # Don't let a notification failure lose this run's data or crash the
            # workflow before the database gets committed. We deliberately leave
            # actually_notified False here so the *next* run will still see this
            # as a price worth notifying about and retry, instead of silently
            # giving up on a fare we never actually told you about.
            print(f"WARNING: found a qualifying price but the ntfy notification failed: {e}", file=sys.stderr)
    else:
        print(f"No notification (price {price:.2f} {target_currency} vs max {max_price}, "
              f"last notified {last_notified}).")

    log_price_check(
        db_conn,
        origin=origin,
        destination=destination,
        depart_date=depart_date,
        return_date=return_date,
        source=source,
        native_price=native_price,
        native_currency=native_currency,
        converted_price=price,
        target_currency=target_currency,
        max_price=max_price,
        notified=actually_notified,
    )
    db_conn.close()


if __name__ == "__main__":
    main()
