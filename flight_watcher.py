#!/usr/bin/env python3
"""
flight_watcher.py

Checks flight prices via the Duffel Flights API and sends an ntfy push
notification to your phone when the price drops under a threshold.

Designed to be run on a schedule (e.g. via GitHub Actions cron).
State (the last price we notified about) is kept in state.json so we
don't spam you every run — only when the price is new/lower.

Note on currency: Duffel returns offer prices in your Duffel account's
settlement currency (set in your Duffel dashboard), not a currency you
choose per-request. This script converts that price into your target
CURRENCY for comparison/display using live exchange rates, but it can't
"shop" the same search across multiple currencies the way some other
providers allow — that's a Duffel API limitation, not a bug here.
"""

import os
import json
import sys
from datetime import datetime

import requests

STATE_FILE = "state.json"
DUFFEL_API_BASE = "https://api.duffel.com"
DUFFEL_VERSION = "v2"


def get_env(name, required=True, default=None):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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
    url = f"{ntfy_server}/{topic}"
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": priority,
        "Tags": "airplane,moneybag",
    }
    resp = requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
    resp.raise_for_status()


def main():
    api_key = get_env("DUFFEL_API_KEY")
    ntfy_topic = get_env("NTFY_TOPIC")
    ntfy_server = get_env("NTFY_SERVER", required=False, default="https://ntfy.sh")

    origin = get_env("ORIGIN")               # e.g. "XNA" (Northwest Arkansas Regional)
    destination = get_env("DESTINATION")      # e.g. "LAX"
    depart_date = get_env("DEPART_DATE")      # "YYYY-MM-DD"
    return_date = get_env("RETURN_DATE", required=False, default=None)
    max_price = float(get_env("MAX_PRICE"))   # interpreted in CURRENCY (your target currency)
    target_currency = get_env("CURRENCY", required=False, default="USD")
    adults = int(get_env("ADULTS", required=False, default="1"))
    serpapi_key = get_env("SERPAPI_API_KEY", required=False, default=None)

    state = load_state()
    route_key = f"{origin}-{destination}-{depart_date}-{return_date}"

    offers = []
    try:
        offers = search_flights(api_key, origin, destination, depart_date, return_date, adults)
    except requests.RequestException as e:
        print(f"Duffel API request failed: {e}", file=sys.stderr)

    best = cheapest_offer(offers)
    source = "Duffel"

    if best:
        native_price = float(best["total_amount"])
        native_currency = best["total_currency"]
    elif serpapi_key:
        print("No Duffel offers found — trying Google Flights via SerpApi as a fallback...")
        fallback = search_google_flights_fallback(
            serpapi_key, origin, destination, depart_date, return_date, target_currency, adults
        )
        if not fallback:
            print("No offers found via Duffel or the SerpApi fallback.")
            return
        native_price, native_currency = fallback
        source = "Google Flights (via SerpApi)"
    else:
        print("No offers found via Duffel, and SERPAPI_API_KEY is not set so no fallback was tried.")
        return

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

    last_notified = state.get(route_key, {}).get("last_notified_price")
    should_notify = price <= max_price and (last_notified is None or price < last_notified)

    if should_notify:
        gf_link = (
            f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}"
            f"%20to%20{destination}%20on%20{depart_date}"
        )
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
            + f"\n\nCheck it: {gf_link}"
            + currency_note
        )
        send_ntfy_notification(ntfy_topic, title, message, priority="high", ntfy_server=ntfy_server)
        print("Notification sent.")

        state[route_key] = {
            "last_notified_price": price,
            "last_checked": datetime.utcnow().isoformat(),
        }
        save_state(state)
    else:
        print(f"No notification (price {price:.2f} {target_currency} vs max {max_price}, "
              f"last notified {last_notified}).")
        state.setdefault(route_key, {})["last_checked"] = datetime.utcnow().isoformat()
        save_state(state)


if __name__ == "__main__":
    main()
