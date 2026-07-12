#!/usr/bin/env python3
"""Find the cheapest origin city for flying to a given destination."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from origins import ORIGINS

API_BASE = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
AVIASALES_BASE = "https://www.aviasales.com"
CACHE_PATH = Path(__file__).resolve().parent / "cache.json"
RESULTS_PATH = Path(__file__).resolve().parent / "results.csv"
REQUEST_SLEEP_S = 0.2
POSITIONING_ORIGIN = "DXB"

console = Console()


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


def build_request_url(params: dict[str, str]) -> str:
    # Stable key for cache: sorted query string, no token in URL
    return f"{API_BASE}?{urlencode(sorted(params.items()))}"


def fetch_prices(
    token: str,
    cache: dict[str, Any],
    *,
    origin: str,
    destination: str,
    departure_at: str,
    currency: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Fetch cheapest tickets for a route. Returns empty list on no data / failure."""
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": departure_at,
        "currency": currency.lower(),
        "sorting": "price",
        "one_way": "true",
        "limit": str(limit),
        "page": "1",
    }
    cache_key = build_request_url(params)

    if cache_key in cache:
        cached = cache[cache_key]
        if isinstance(cached, dict) and "data" in cached:
            data = cached.get("data") or []
            return data if isinstance(data, list) else []
        return []

    headers = {"X-Access-Token": token}
    last_error: Exception | None = None

    for attempt in range(2):  # initial try + one retry
        try:
            time.sleep(REQUEST_SLEEP_S)
            response = requests.get(
                API_BASE, params=params, headers=headers, timeout=30
            )
            response.raise_for_status()
            payload = response.json()
            cache[cache_key] = payload
            save_cache(cache)
            data = payload.get("data") or []
            return data if isinstance(data, list) else []
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 0:
                console.print(
                    f"[yellow]Retry[/yellow] {origin}->{destination}: {exc}"
                )
            continue

    console.print(
        f"[dim]Failed {origin}->{destination} after retry: {last_error}[/dim]"
    )
    # Cache empty result so we don't hammer a broken route on re-runs of same key
    # only when we got a valid empty success; on hard failure leave uncached.
    return []


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
    # API sorts by price, but be defensive
    return min(filtered, key=lambda t: float(t.get("price") or float("inf")))


def aviasales_link(ticket: dict[str, Any]) -> str:
    link = ticket.get("link") or ""
    if not link:
        return ""
    if link.startswith("http"):
        return link
    return f"{AVIASALES_BASE}{link}"


def parse_args() -> argparse.Namespace:
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
        "month",
        nargs="?",
        default="2026-12",
        help="Departure month YYYY-MM (default: 2026-12)",
    )
    parser.add_argument(
        "currency",
        nargs="?",
        default="usd",
        help="Currency code (default: usd)",
    )
    parser.add_argument(
        "--airline",
        default=None,
        help="Optional airline IATA filter, e.g. EK",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    destination = args.destination.upper().strip()
    month = args.month.strip()
    currency = args.currency.lower().strip()
    airline_filter = args.airline.upper().strip() if args.airline else None

    token = load_token()
    cache = load_cache()

    console.print(
        f"[bold]Searching[/bold] origins → [cyan]{destination}[/cyan] "
        f"in [cyan]{month}[/cyan] ({currency.upper()})"
        + (f", airline={airline_filter}" if airline_filter else "")
    )
    console.print(f"Origins: {len(ORIGINS)} | Cache: {CACHE_PATH.name}")

    rows: list[dict[str, Any]] = []

    for i, origin in enumerate(ORIGINS, start=1):
        console.print(f"[dim]({i}/{len(ORIGINS)}) {origin} → {destination}[/dim]")

        tickets = fetch_prices(
            token,
            cache,
            origin=origin,
            destination=destination,
            departure_at=month,
            currency=currency,
            limit=100 if airline_filter else 30,
        )
        cheapest = pick_cheapest(tickets, airline_filter)

        if cheapest is None:
            console.print(f"  [yellow]{origin}: no data[/yellow]")
            continue

        fare = float(cheapest.get("price") or 0)
        airline = str(cheapest.get("airline") or "?")
        link = aviasales_link(cheapest)

        # Positioning: DXB → origin (same month). Skip when origin is DXB.
        positioning: float | None
        missing_positioning = False
        if origin == POSITIONING_ORIGIN:
            positioning = 0.0
        else:
            pos_tickets = fetch_prices(
                token,
                cache,
                origin=POSITIONING_ORIGIN,
                destination=origin,
                departure_at=month,
                currency=currency,
                limit=30,
            )
            pos_cheapest = pick_cheapest(pos_tickets, None)
            if pos_cheapest is None:
                positioning = None
                missing_positioning = True
            else:
                positioning = float(pos_cheapest.get("price") or 0)

        if missing_positioning:
            # Rank by fare alone when DXB→origin positioning is unavailable
            total = fare
        else:
            total = fare + (positioning or 0.0)

        rows.append(
            {
                "origin": origin,
                "airline": airline,
                "fare": fare,
                "positioning": positioning,
                "missing_positioning": missing_positioning,
                "total": total,
                "link": link,
            }
        )

    if not rows:
        console.print("[red]No routes with data found.[/red]")
        sys.exit(0)

    rows.sort(key=lambda r: r["total"])

    table = Table(title=f"Cheapest origins → {destination} ({month})")
    table.add_column("#", justify="right", style="bold")
    table.add_column("Origin")
    table.add_column("Airline")
    table.add_column("Fare", justify="right")
    table.add_column("Positioning", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Link")

    csv_rows: list[dict[str, Any]] = []

    for rank, row in enumerate(rows, start=1):
        pos_display = (
            "?"
            if row["missing_positioning"]
            else f"{row['positioning']:.0f}"
        )
        flag = " *" if row["missing_positioning"] else ""
        total_display = f"{row['total']:.0f}{flag}"

        table.add_row(
            str(rank),
            row["origin"],
            row["airline"],
            f"{row['fare']:.0f}",
            pos_display,
            total_display,
            row["link"] or "—",
        )
        csv_rows.append(
            {
                "rank": rank,
                "origin": row["origin"],
                "airline": row["airline"],
                "fare": row["fare"],
                "positioning": "?" if row["missing_positioning"] else row["positioning"],
                "total": row["total"],
                "missing_positioning": row["missing_positioning"],
                "link": row["link"],
            }
        )

    console.print(table)
    console.print(
        "[dim]* Total marked with * ranked by fare only "
        "(DXB→origin positioning missing).[/dim]"
    )

    with RESULTS_PATH.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "rank",
                "origin",
                "airline",
                "fare",
                "positioning",
                "total",
                "missing_positioning",
                "link",
            ],
        )
        writer.writeheader()
        writer.writerows(csv_rows)

    console.print(f"[green]Saved[/green] {RESULTS_PATH} ({len(csv_rows)} rows)")


if __name__ == "__main__":
    main()
