# feed

Pulls Google News RSS for a list of stocks, applies per-stock rules, and writes kept items, rejects, and one Teams-ready message per company. Nothing is posted to Teams yet.

## Layout
- `config/tickers.json` — `stocks` (ticker, name, aliases, optional overrides), `feed` (URL, query, lookback, delay), shared `template`, `output_dir`, `schedule`.
- `templates/default.json` — shared rules (`require_ticker_in_title`, `reject_analyst_firm_mentions`, `exclude_keywords`, `exclude_sources`, weighted `include_keywords`, `min_score`), output `fields`, and the `publish` block (message format).
- `src/feed_filter.py` — fetch, apply rules, write outputs. Stdlib only, Python 3.9+.
- `outputs/feed-<day>.jsonl` — kept items. These are also the dedupe state, re-read each cycle.
- `outputs/rejects-<day>.jsonl` — dropped items with `reject_reason`. Each (ticker, title, reason) is logged once.
- `outputs/messages/<day>/<TICKER>.md` — one Teams-ready Markdown message per company, rebuilt each cycle.
- `outputs/state.json` — start time of the last fully successful cycle. Sets the next fetch window.

## Message format (`publish` in the template)
- `header`, `item`, `empty`, `more`: text templates for each message.
- `max_items_per_message` (default 10): extra items are summarised as "+N more not shown".
- `skip_empty` (default `true`): companies with no items get no file, and any stale file is removed. Set `false` to write an "empty" message for every company.

## Per-stock rules
Each stock inherits the shared template and can override it:
```json
{"ticker": "NVDA", "name": "Nvidia", "aliases": ["Nvidia Corp"],
 "include_keywords": {"Blackwell": 2}, "exclude_keywords": [], "exclude_sources": [], "min_score": 3}
```
- `include_keywords` are merged into the shared set. `exclude_keywords` and `exclude_sources` are added. `min_score` replaces the shared value.
- `template` (optional, not in the example above) names a full template file to use as the base instead of the shared one. The other fields are still applied on top.

## Rule order
1. already in today's feed file (same title or link)
2. excluded source
3. exclude keyword (whole word)
4. company name, alias, or ticker in title (if `require_ticker_in_title`). With `reject_analyst_firm_mentions` (default `true`), a name that appears only after "by", "from", or "at" is rejected as `analyst_firm_mention`.
5. include-keyword score >= `min_score`

Rejected items aren't remembered, so they are re-checked each cycle. A template change therefore applies to items still inside the fetch window.

Known limit: a phrase like "led by Nvidia" can also be dropped, because "by" is treated as the analyst-firm marker. Set `reject_analyst_firm_mentions` to `false` to turn the rule off.

## Lookback and rate limits
- The window follows the time since the last successful cycle, so a late or missed cycle does not leave a gap. Each cycle's window is `ceil(time since last cycle + overlap_hours)`, capped at `max_lookback_hours` (default 24). `overlap_hours` defaults to 0. With a 1h interval the window is `1h`, and with a 5-minute interval it is also `1h`, because Google returns nothing for `5m`, `15m`, or `30m` (tested). Repeats across cycles are removed by dedupe.
- With no `outputs/state.json` yet (first run, or after deleting it), `feed.lookback` (`1h`) is used.
- Google's `when:` operator returned no results for `15m`, so the window is never shorter than one hour.
- The cursor only advances when every stock fetched successfully. If a stock fails, the next cycle's window covers the gap.
- Dry runs do not update the cursor.
- `feed.delay_between_requests_seconds` (default 3) spaces requests. Google returns HTTP 429 if requests come too fast. A failed stock is logged as a warning and skipped for that cycle.
- A 49-stock cycle takes roughly 2.5 minutes at the 3-second delay (timing not yet measured). For a load test, copy `config/tickers.json`, change the stock list and `output_dir`, then pass it with `--config`.

## Run
    python3 src/feed_filter.py --dry-run                       # print new kept items, write nothing
    python3 src/feed_filter.py                                 # one cycle
    python3 src/feed_filter.py --loop                          # every schedule.interval_seconds
    python3 src/feed_filter.py --loop --interval 300           # override the interval
