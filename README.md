# CinderVex public data

- `data/delisted-stocks.json`: the weekly list of US stocks delisted since 2016 (common stock only), built from the US regulator's delisting filings (Form 25-NSE). Regulator information only: no prices. Best effort. Mergers and buyouts are included; moves to another big exchange are marked "moved".
  Download: https://raw.githubusercontent.com/cindervex47/cindervex_data/main/data/delisted-stocks.json
- Built every Sunday night by `.github/workflows/delisted-stocks.yml` (5 requests a second, contact support@getcindervex.com). The first run backfills 2016 onward and restarts itself until done.
- The script's master copy and its tests live in the CinderVex app's code (`scripts/delisted/`, lab126, D-160).
