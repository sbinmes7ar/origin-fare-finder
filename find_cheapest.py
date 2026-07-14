#!/usr/bin/env python3
"""Find the cheapest origin city for flying to a given destination.

Economy uses Aviasales v3 prices_for_dates (native round-trip supported).
Business/first use v2 prices/month-matrix with trip_class (one-way only;
round-trip is estimated as the sum of two one-ways).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from origins import ORIGINS

V3_API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
V2_MATRIX_API = "https://api.travelpayouts.com/v2/prices/month-matrix"
AVIASALES_BASE = "https://www.aviasales.com"
CACHE_PATH = Path(__file__).resolve().parent / "cache.json"
RESULTS_PATH = Path(__file__).resolve().parent / "results.csv"
RUNS_DIR = Path(__file__).resolve().parent / "runs"
REQUEST_SLEEP_S = 0.2
MAX_WORKERS = 5
POSITIONING_ORIGIN = "DXB"

CABIN_TO_TRIP_CLASS = {
    "economy": 0,
    "business": 1,
    "first": 2,
}

console = Console()
_cache_lock = threading.Lock()
_print_lock = threading.Lock()


def load_token() -> str:
    load_dotenv()
    token = os.getenv("TRAVELPAYOUTS_TOKEN", "").strip()
    if not token:
        console.print(
            "[red]Missing TRAVELPAYOUTS_TOKEN.[/red] "
            "Copy .env.example to .env and set your API token."
        )
        sys.exit(1)
    return token


def load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cache(cache: dict[str, Any]) -> None:
    with CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def month_to_ymd(month: str) -> str:
    """Accept YYYY-MM or YYYY-MM-DD; return YYYY-MM-DD (first of month)."""
    month = month.strip()
    if len(month) == 7:
        return f"{month}-01"
    return month


def month_yyyy_mm(month: str) -> str:
    return month_to_ymd(month)[:7]


def build_cache_key(url: str, params: dict[str, str]) -> str:
    return f"{url}?{urlencode(sorted(params.items()))}"


def _should_retry_status(status_code: int) -> bool:
    return status_code >= 500


def _safe_print(message: str) -> None:
    with _print_lock:
        console.print(message)


def fetch_json(
    token: str,
    cache: dict[str, Any],
    url: str,
    params: dict[str, str],
    *,
    debug: bool = False,
) -> dict[str, Any] | None:
    """GET JSON with cache, per-call sleep, and one retry on network/5xx only.

    Returns the payload dict on success (including HTTP 200 with empty data).
    Returns None on hard failure after retry.
    Does NOT retry when the API returns HTTP 200 with empty/no data.
    """
    cache_key = build_cache_key(url, params)

    with _cache_lock:
        if cache_key in cache:
            cached = cache[cache_key]
            if isinstance(cached, dict):
                if debug:
                    _safe_print(f"[dim]cache hit[/dim] {cache_key}")
                return cached

    headers = {"X-Access-Token": token}
    last_error: Exception | None = None

    for attempt in range(2):
        try:
            time.sleep(REQUEST_SLEEP_S)
            if debug:
                _safe_print(
                    f"[dim]GET[/dim] {url} params={params} (attempt {attempt + 1})"
                )
            response = requests.get(url, params=params, headers=headers, timeout=30)

            # HTTP 200 with empty body/data: cache and return — do not retry
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError as exc:
                    last_error = exc
                    if attempt == 0:
                        _safe_print(f"[yellow]Retry[/yellow] bad JSON: {exc}")
                        continue
                    break
                if not isinstance(payload, dict):
                    payload = {"data": payload}
                with _cache_lock:
                    cache[cache_key] = payload
                    save_cache(cache)
                return payload

            # 4xx (except we never retry): treat as failure, no retry
            if 400 <= response.status_code < 500:
                if debug:
                    _safe_print(
                        f"[yellow]HTTP {response.status_code}[/yellow] {url} {params}"
                    )
                return None

            # 5xx: retry once
            if _should_retry_status(response.status_code):
                last_error = requests.HTTPError(
                    f"{response.status_code} Server Error for {response.url}"
                )
                if attempt == 0:
                    _safe_print(f"[yellow]Retry[/yellow] {last_error}")
                    continue
                break

            response.raise_for_status()

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            if attempt == 0:
                _safe_print(f"[yellow]Retry[/yellow] network: {exc}")
                continue
            break
        except requests.RequestException as exc:
            # Other request errors: retry once if it looks transient
            last_error = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and 400 <= status < 500:
                return None
            if attempt == 0:
                _safe_print(f"[yellow]Retry[/yellow] {exc}")
                continue
            break

    if last_error and debug:
        _safe_print(f"[dim]Failed after retry: {last_error}[/dim]")
    return None


def extract_v3_tickets(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def extract_v2_tickets(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Normalize v2 month-matrix rows to a common shape with price/airline/link."""
    if not payload:
        return []
    data = payload.get("data") or []
    if not isinstance(data, list):
        return []
    tickets: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        price = row.get("value", row.get("price"))
        if price is None:
            continue
        origin = str(row.get("origin") or "")
        destination = str(row.get("destination") or "")
        depart = str(row.get("depart_date") or "")
        tickets.append(
            {
                "price": float(price),
                "airline": row.get("airline") or "?",
                "link": row.get("link")
                or _build_search_link(origin, destination, depart),
                "origin": origin,
                "destination": destination,
                "depart_date": depart,
                "raw": row,
            }
        )
    return tickets


def _build_search_link(origin: str, destination: str, depart_date: str) -> str:
    if not origin or not destination:
        return ""
    # Aviasales deep-ish search URL; date optional (YYYY-MM-DD → DDMM)
    if depart_date and len(depart_date) >= 10:
        ddmm = depart_date[8:10] + depart_date[5:7]
        return f"{AVIASALES_BASE}/search/{origin}{ddmm}{destination}1"
    return f"{AVIASALES_BASE}/search/?origin_iata={origin}&destination_iata={destination}"


def aviasales_link(ticket: dict[str, Any]) -> str:
    link = ticket.get("link") or ""
    if not link:
        return ""
    if link.startswith("http"):
        return link
    return f"{AVIASALES_BASE}{link}"


def pick_cheapest(
    tickets: list[dict[str, Any]], airline_filter: str | None
) -> dict[str, Any] | None:
    if not tickets:
        return None
    filtered = tickets
    if airline_filter:
        code = airline_filter.upper()
        filtered = [
            t for t in tickets if str(t.get("airline", "")).upper() == code
        ]
        if not filtered:
            return None
    return min(filtered, key=lambda t: float(t.get("price") or float("inf")))


def fetch_economy_leg(
    token: str,
    cache: dict[str, Any],
    *,
    origin: str,
    destination: str,
    depart_month: str,
    return_month: str | None,
    currency: str,
    airline_filter: str | None,
    debug: bool,
) -> dict[str, Any] | None:
    """v3 prices_for_dates — native one-way or round-trip."""
    params: dict[str, str] = {
        "origin": origin,
        "destination": destination,
        "departure_at": month_yyyy_mm(depart_month),
        "currency": currency.lower(),
        "sorting": "price",
        "limit": "100" if airline_filter else "30",
        "page": "1",
    }
    if return_month:
        params["return_at"] = month_yyyy_mm(return_month)
        params["one_way"] = "false"
    else:
        params["one_way"] = "true"

    payload = fetch_json(token, cache, V3_API, params, debug=debug)
    return pick_cheapest(extract_v3_tickets(payload), airline_filter)


def fetch_cabin_one_way(
    token: str,
    cache: dict[str, Any],
    *,
    origin: str,
    destination: str,
    month: str,
    currency: str,
    trip_class: int,
    airline_filter: str | None,
    debug: bool,
) -> dict[str, Any] | None:
    """v2 month-matrix one-way for business/first (trip_class 1/2)."""
    params = {
        "origin": origin,
        "destination": destination,
        "month": month_to_ymd(month),
        "currency": currency.lower(),
        "trip_class": str(trip_class),
        "one_way": "true",
        "show_to_affiliates": "true",
        "limit": "31",
    }
    payload = fetch_json(token, cache, V2_MATRIX_API, params, debug=debug)
    tickets = extract_v2_tickets(payload)
    # Keep only rows matching the requested cabin when the API mixes classes
    matched = [
        t
        for t in tickets
        if int((t.get("raw") or {}).get("trip_class", trip_class)) == trip_class
    ]
    return pick_cheapest(matched or tickets, airline_filter)


def fetch_route_fare(
    token: str,
    cache: dict[str, Any],
    *,
    origin: str,
    destination: str,
    depart_month: str,
    return_month: str | None,
    currency: str,
    cabin: str,
    airline_filter: str | None,
    debug: bool,
) -> dict[str, Any] | None:
    """Return a normalized fare dict or None if no data.

    Keys: fare, airline, link, fare_note (optional label for RT business).
    """
    cabin = cabin.lower()
    if cabin == "economy":
        ticket = fetch_economy_leg(
            token,
            cache,
            origin=origin,
            destination=destination,
            depart_month=depart_month,
            return_month=return_month,
            currency=currency,
            airline_filter=airline_filter,
            debug=debug,
        )
        if not ticket:
            return None
        return {
            "fare": float(ticket.get("price") or 0),
            "airline": str(ticket.get("airline") or "?"),
            "link": aviasales_link(ticket),
            "fare_note": "",
        }

    trip_class = CABIN_TO_TRIP_CLASS[cabin]
    outbound = fetch_cabin_one_way(
        token,
        cache,
        origin=origin,
        destination=destination,
        month=depart_month,
        currency=currency,
        trip_class=trip_class,
        airline_filter=airline_filter,
        debug=debug,
    )
    if not outbound:
        return None

    if not return_month:
        return {
            "fare": float(outbound.get("price") or 0),
            "airline": str(outbound.get("airline") or "?"),
            "link": aviasales_link(outbound),
            "fare_note": "",
        }

    # Business/first round-trip: sum of two one-ways
    inbound = fetch_cabin_one_way(
        token,
        cache,
        origin=destination,
        destination=origin,
        month=return_month,
        currency=currency,
        trip_class=trip_class,
        airline_filter=airline_filter,
        debug=debug,
    )
    if not inbound:
        return None

    out_price = float(outbound.get("price") or 0)
    in_price = float(inbound.get("price") or 0)
    airlines = []
    for t in (outbound, inbound):
        a = str(t.get("airline") or "?")
        if a and a != "?":
            airlines.append(a)
    airline = "+".join(dict.fromkeys(airlines)) if airlines else "?"

    return {
        "fare": out_price + in_price,
        "airline": airline,
        "link": aviasales_link(outbound),
        "fare_note": "sum of two one-ways",
        "outbound_fare": out_price,
        "inbound_fare": in_price,
    }


def fetch_positioning(
    token: str,
    cache: dict[str, Any],
    *,
    origin: str,
    month: str,
    currency: str,
    cabin: str,
    debug: bool,
) -> tuple[float | None, str]:
    """Return (price, flag) where flag is '' | '~' | '?'.

    Prefer DXB→origin; if missing, try origin→DXB as estimate (~).
    Only '?' when both directions are empty.
    """
    if origin == POSITIONING_ORIGIN:
        return 0.0, ""

    # Positioning stays on economy cache when possible for broader coverage,
    # but honor cabin for premium searches.
    if cabin == "economy":
        forward = fetch_economy_leg(
            token,
            cache,
            origin=POSITIONING_ORIGIN,
            destination=origin,
            depart_month=month,
            return_month=None,
            currency=currency,
            airline_filter=None,
            debug=debug,
        )
        if forward:
            return float(forward.get("price") or 0), ""
        reverse = fetch_economy_leg(
            token,
            cache,
            origin=origin,
            destination=POSITIONING_ORIGIN,
            depart_month=month,
            return_month=None,
            currency=currency,
            airline_filter=None,
            debug=debug,
        )
        if reverse:
            return float(reverse.get("price") or 0), "~"
        return None, "?"

    trip_class = CABIN_TO_TRIP_CLASS[cabin]
    forward = fetch_cabin_one_way(
        token,
        cache,
        origin=POSITIONING_ORIGIN,
        destination=origin,
        month=month,
        currency=currency,
        trip_class=trip_class,
        airline_filter=None,
        debug=debug,
    )
    if forward:
        return float(forward.get("price") or 0), ""
    reverse = fetch_cabin_one_way(
        token,
        cache,
        origin=origin,
        destination=POSITIONING_ORIGIN,
        month=month,
        currency=currency,
        trip_class=trip_class,
        airline_filter=None,
        debug=debug,
    )
    if reverse:
        return float(reverse.get("price") or 0), "~"
    return None, "?"


def process_origin(
    origin: str,
    *,
    token: str,
    cache: dict[str, Any],
    destination: str,
    depart_month: str,
    return_month: str | None,
    currency: str,
    cabin: str,
    airline_filter: str | None,
    debug: bool,
) -> dict[str, Any]:
    fare_info = fetch_route_fare(
        token,
        cache,
        origin=origin,
        destination=destination,
        depart_month=depart_month,
        return_month=return_month,
        currency=currency,
        cabin=cabin,
        airline_filter=airline_filter,
        debug=debug,
    )
    if fare_info is None:
        return {"origin": origin, "status": "no_data"}

    positioning, pos_flag = fetch_positioning(
        token,
        cache,
        origin=origin,
        month=depart_month,
        currency=currency,
        cabin=cabin,
        debug=debug,
    )

    fare = float(fare_info["fare"])
    if pos_flag == "?":
        total = fare
        missing_positioning = True
    else:
        total = fare + float(positioning or 0)
        missing_positioning = False

    return {
        "origin": origin,
        "status": "ok",
        "airline": fare_info["airline"],
        "fare": fare,
        "fare_note": fare_info.get("fare_note") or "",
        "outbound_fare": fare_info.get("outbound_fare"),
        "inbound_fare": fare_info.get("inbound_fare"),
        "positioning": positioning,
        "positioning_flag": pos_flag,
        "missing_positioning": missing_positioning,
        "total": total,
        "link": fare_info.get("link") or "",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find the cheapest origin city for flying to a destination, "
            "including DXB positioning cost."
        )
    )
    parser.add_argument(
        "destination",
        nargs="?",
        default="JFK",
        help="Destination IATA code (default: JFK)",
    )
    parser.add_argument(
        "--depart-month",
        default="2026-12",
        help="Departure month YYYY-MM (default: 2026-12)",
    )
    parser.add_argument(
        "--return-month",
        default=None,
        help="Return month YYYY-MM (optional; enables round-trip)",
    )
    parser.add_argument(
        "--currency",
        default="usd",
        help="Currency code (default: usd)",
    )
    parser.add_argument(
        "--cabin",
        default="economy",
        choices=sorted(CABIN_TO_TRIP_CLASS.keys()),
        help="Cabin class (default: economy). Business/first use v2 month-matrix.",
    )
    parser.add_argument(
        "--airline",
        default=None,
        help="Optional airline IATA filter, e.g. EK (best supported for economy)",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=30,
        help="Show only the top N ranked rows in the table (default: 30)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose request/cache logging",
    )
    parser.add_argument(
        "--test-request",
        action="store_true",
        help="Fire one sample API request and print the JSON response, then exit",
    )
    # Back-compat positional month/currency (optional)
    parser.add_argument(
        "legacy_month",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "legacy_currency",
        nargs="?",
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def run_test_request(token: str, args: argparse.Namespace) -> None:
    """Single diagnostic request for the selected cabin."""
    dest = args.destination.upper().strip()
    month = args.depart_month
    currency = args.currency.lower()
    cabin = args.cabin.lower()
    cache: dict[str, Any] = {}

    if cabin == "economy":
        params = {
            "origin": "DXB",
            "destination": dest,
            "departure_at": month_yyyy_mm(month),
            "currency": currency,
            "sorting": "price",
            "one_way": "true",
            "limit": "5",
            "page": "1",
        }
        if args.return_month:
            params["return_at"] = month_yyyy_mm(args.return_month)
            params["one_way"] = "false"
        console.print(f"[bold]Test v3[/bold] {V3_API}")
        console.print(params)
        payload = fetch_json(token, cache, V3_API, params, debug=True)
    else:
        params = {
            "origin": "DXB",
            "destination": dest,
            "month": month_to_ymd(month),
            "currency": currency,
            "trip_class": str(CABIN_TO_TRIP_CLASS[cabin]),
            "one_way": "true",
            "show_to_affiliates": "true",
            "limit": "31",
        }
        console.print(f"[bold]Test v2 month-matrix[/bold] {V2_MATRIX_API}")
        console.print(params)
        payload = fetch_json(token, cache, V2_MATRIX_API, params, debug=True)

    console.print_json(json.dumps(payload if payload is not None else {"error": "failed"}))


def write_results_csv(path: Path, csv_rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "rank",
        "origin",
        "airline",
        "fare",
        "fare_note",
        "positioning",
        "positioning_flag",
        "total",
        "missing_positioning",
        "link",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    destination = args.destination.upper().strip()
    depart_month = (args.legacy_month or args.depart_month).strip()
    return_month = args.return_month.strip() if args.return_month else None
    currency = (args.legacy_currency or args.currency).lower().strip()
    cabin = args.cabin.lower().strip()
    airline_filter = args.airline.upper().strip() if args.airline else None
    top_n = max(1, args.top)
    debug = bool(args.debug)

    token = load_token()

    if args.test_request:
        run_test_request(token, args)
        return

    cache = load_cache()

    trip_label = (
        f"{depart_month} → {return_month} RT"
        if return_month
        else f"{depart_month} OW"
    )
    console.print(
        f"[bold]Searching[/bold] origins → [cyan]{destination}[/cyan] "
        f"| [cyan]{trip_label}[/cyan] | cabin=[cyan]{cabin}[/cyan] "
        f"| {currency.upper()}"
        + (f" | airline={airline_filter}" if airline_filter else "")
    )
    if cabin != "economy" and return_month:
        console.print(
            "[dim]Business/first round-trip = sum of two one-ways "
            "(v2 month-matrix is one-way only).[/dim]"
        )
    console.print(
        f"Origins: {len(ORIGINS)} | Workers: {MAX_WORKERS} | Cache: {CACHE_PATH.name}"
    )

    rows: list[dict[str, Any]] = []
    no_data_count = 0
    done = 0
    total_origins = len(ORIGINS)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(
                process_origin,
                origin,
                token=token,
                cache=cache,
                destination=destination,
                depart_month=depart_month,
                return_month=return_month,
                currency=currency,
                cabin=cabin,
                airline_filter=airline_filter,
                debug=debug,
            ): origin
            for origin in ORIGINS
        }
        for future in as_completed(futures):
            origin = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — keep run alive
                _safe_print(f"[red]{origin} error:[/red] {exc}")
                no_data_count += 1
                continue

            if result.get("status") != "ok":
                no_data_count += 1
                _safe_print(
                    f"[dim]({done}/{total_origins})[/dim] "
                    f"[yellow]{origin}: no data[/yellow]"
                )
                continue

            rows.append(result)
            note = f" ({result['fare_note']})" if result.get("fare_note") else ""
            _safe_print(
                f"[dim]({done}/{total_origins})[/dim] "
                f"[green]{origin}[/green] fare={result['fare']:.0f}{note}"
            )

    with_data = len(rows)
    console.print(
        f"[bold]Summary:[/bold] {with_data} origins with data, "
        f"{no_data_count} no data "
        f"(of {total_origins})"
    )

    if not rows:
        console.print("[red]No routes with data found.[/red]")
        sys.exit(0)

    rows.sort(key=lambda r: r["total"])

    title = f"Cheapest origins → {destination} ({trip_label}, {cabin})"
    table = Table(title=title)
    table.add_column("#", justify="right", style="bold")
    table.add_column("Origin")
    table.add_column("Airline")
    table.add_column("Fare", justify="right")
    table.add_column("Positioning", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Note")
    table.add_column("Link")

    csv_rows: list[dict[str, Any]] = []
    display_rows = rows[:top_n]

    for rank, row in enumerate(rows, start=1):
        pos_flag = row.get("positioning_flag") or ""
        if pos_flag == "?":
            pos_display = "?"
        elif pos_flag == "~":
            pos_display = f"~{row['positioning']:.0f}"
        else:
            pos_display = f"{row['positioning']:.0f}"

        flag = " *" if row["missing_positioning"] else ""
        total_display = f"{row['total']:.0f}{flag}"
        note = row.get("fare_note") or ""
        if pos_flag == "~" and not note:
            note = "pos. ~ reverse"
        elif pos_flag == "~" and note:
            note = f"{note}; pos. ~ reverse"

        if rank <= top_n:
            table.add_row(
                str(rank),
                row["origin"],
                row["airline"],
                f"{row['fare']:.0f}",
                pos_display,
                total_display,
                note or "—",
                row["link"] or "—",
            )

        csv_rows.append(
            {
                "rank": rank,
                "origin": row["origin"],
                "airline": row["airline"],
                "fare": row["fare"],
                "fare_note": row.get("fare_note") or "",
                "positioning": (
                    "?"
                    if pos_flag == "?"
                    else (
                        f"~{row['positioning']}"
                        if pos_flag == "~"
                        else row["positioning"]
                    )
                ),
                "positioning_flag": pos_flag,
                "total": row["total"],
                "missing_positioning": row["missing_positioning"],
                "link": row["link"],
            }
        )

    console.print(table)
    if len(rows) > top_n:
        console.print(
            f"[dim]Showing top {top_n} of {len(rows)} ranked results "
            f"(use --top to change).[/dim]"
        )
    console.print(
        "[dim]* Total marked with * ranked by fare only "
        "(no DXB positioning in either direction). "
        "~ positioning = reverse origin→DXB estimate.[/dim]"
    )

    write_results_csv(RESULTS_PATH, csv_rows)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_name = (
        f"results_{destination}_{month_yyyy_mm(depart_month)}"
        f"_{cabin}_{stamp}.csv"
    )
    run_path = RUNS_DIR / run_name
    write_results_csv(run_path, csv_rows)

    console.print(
        f"[green]Saved[/green] {RESULTS_PATH.name} and {run_path.relative_to(Path.cwd())} "
        f"({len(csv_rows)} rows; table showed {len(display_rows)})"
    )


if __name__ == "__main__":
    main()
