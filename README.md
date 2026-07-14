# Cheapest Origin Finder

Find the cheapest origin city for flying to a given destination using the
[Travelpayouts Aviasales Data API](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API).

For each origin it also looks up a **DXB positioning** cost, then ranks by
**total = fare + positioning**.

| Cabin | API | Round-trip |
| --- | --- | --- |
| `economy` | v3 `prices_for_dates` | Native (set `--return-month`) |
| `business` / `first` | v2 `prices/month-matrix` + `trip_class` | Sum of two one-ways (outbound + return) |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set your Travelpayouts token:

```
TRAVELPAYOUTS_TOKEN=your_real_token
```

Get a token from: https://www.travelpayouts.com/programs/100/tools/api

## Usage

```bash
# Defaults: destination=JFK, depart-month=2026-12, currency=usd, cabin=economy
python find_cheapest.py

# Economy round-trip
python find_cheapest.py JFK --depart-month 2026-12 --return-month 2026-12 --cabin economy

# Business one-way
python find_cheapest.py JFK --depart-month 2026-12 --cabin business

# Business round-trip (sum of two one-ways)
python find_cheapest.py JFK --depart-month 2026-12 --return-month 2026-12 --cabin business --top 20

# First class, EUR, airline filter, debug
python find_cheapest.py LHR --depart-month 2026-08 --cabin first --currency eur --airline BA --debug

# Single diagnostic API call
python find_cheapest.py JFK --cabin business --test-request
```

### Flags

| Flag | Default | Description |
| --- | --- | --- |
| `destination` | `JFK` | Destination IATA code |
| `--depart-month` | `2026-12` | Departure month `YYYY-MM` |
| `--return-month` | _(none)_ | Return month `YYYY-MM` (enables round-trip) |
| `--currency` | `usd` | Price currency |
| `--cabin` | `economy` | `economy`, `business`, or `first` |
| `--airline` | _(none)_ | Filter by airline IATA (best for economy) |
| `--top` | `30` | How many ranked rows to show in the table |
| `--debug` | off | Verbose request/cache logging |
| `--test-request` | off | One sample API call, print JSON, exit |

### Behaviour notes

- **Parallelism:** origins are fetched with a 5-worker `ThreadPoolExecutor` and a
  200ms sleep per live request (~2 minutes for a cold 163-origin run).
- **Retries:** only network errors and HTTP 5xx are retried once. HTTP 200 with
  empty data is treated as “no data” and is **not** retried.
- **Positioning:** prefers `DXB → origin`. If missing, tries `origin → DXB` and
  labels it with `~`. Only shows `?` when both directions are empty (row flagged
  `*` and ranked by fare alone).
- **Business RT:** labeled **sum of two one-ways** in the Note column.

### Outputs

- Rich ranked table (top N)
- Summary line: how many origins had data vs no data
- `results.csv` — full ranked list (overwritten each run)
- `runs/results_<dest>_<month>_<cabin>_<timestamp>.csv` — timestamped copy
- `cache.json` — API responses keyed by request URL

Origin airports live in `origins.py` (~160 hubs across the Middle East, Asia,
Europe, and Africa).
