# Cheapest Origin Finder

Find the cheapest origin city for flying to a given destination using the
[Travelpayouts Aviasales Data API](https://support.travelpayouts.com/hc/en-us/articles/203956163-Aviasales-Data-API)
(`prices_for_dates`). For each origin it also looks up **DXB → origin** as a
positioning cost, then ranks by **total = fare + positioning**.

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
# Defaults: destination=JFK, month=2026-12, currency=usd
python find_cheapest.py

# Custom destination / month / currency
python find_cheapest.py LHR 2026-08 eur

# Filter results to a specific airline
python find_cheapest.py JFK 2026-12 usd --airline EK
```

### Outputs

- **Rich table** in the terminal: rank, origin, airline, fare, positioning, total, Aviasales link
- **`results.csv`** — same ranked data
- **`cache.json`** — API responses keyed by request URL (re-runs skip live calls)

Routes with no cached fare data are marked **no data** and skipped. If DXB→origin
positioning is missing, positioning shows `?`, the row is flagged (`*`), and
ranking uses the fare alone.

Origin airports live in `origins.py` (~150 hubs across the Middle East, Asia,
Europe, and Africa).
