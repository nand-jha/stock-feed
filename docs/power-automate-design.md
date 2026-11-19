# Power Automate design

This is the spec for porting the feed pipeline to Power Automate (premium) with AI Builder credits. `src/feed_filter.py` is the reference implementation: where this document and the script differ, the script's behaviour is the intended one until the two are reconciled.

## Components

| Part | Power Automate piece |
|---|---|
| Schedule | Recurrence trigger, hourly |
| Stock list | SharePoint list `FeedStocks` (or a Dataverse table) |
| Rules | `templates/default.json` values copied into a SharePoint list `FeedRules` |
| Window state | SharePoint list `FeedState`, one row, `LastFetch` (UTC) |
| Fetch | HTTP action (premium) per stock |
| Parse | Parse XML, or the RSS connector |
| Rule checks | Condition and Filter array actions |
| Model judgment | AI Builder prompt (see below), only for items that pass the rules |
| Kept items | SharePoint list `FeedKept` (dedupe source and audit trail) |
| Rejects | SharePoint list `FeedRejects` (logged once per ticker, title, reason) |
| Teams output | Post message in a chat or channel, one per company |

## Cycle

1. **Read state.** Get `LastFetch` from `FeedState`. If empty, the window is `1h`.
2. **Compute window.** `hours = min(max(1, ceil(hours_since(LastFetch) + overlap_hours)), max_lookback_hours)`, with `overlap_hours` = 1 and `max_lookback_hours` = 24. Build `when:{hours}h`.
3. **For each active stock** (Apply to each, sequential, with a 3-second Delay between requests):
   - HTTP GET `https://news.google.com/rss/search?q={name} stock when:{hours}h&hl=en-US&gl=US&ceid=US:en`. Encode the query with URL encoding.
   - Parse the items (title, source, pubDate, link, description, max 50).
   - Strip a trailing ` - {source}` from titles, as the script does.
   - Skip the item if its link or normalised title already exists in `FeedKept` for today.
   - Run the rule checks in order (below). Rejects are written to `FeedRejects` once per (ticker, title, reason).
   - Items that pass go to the AI step, then to `FeedKept` if kept.
4. **Write messages.** For each company with kept items today, build one message (format below) and post it to Teams. Companies with no items get no post.
5. **Save state.** Set `LastFetch` to the cycle start time, but only if every stock fetched successfully. If any HTTP call failed, leave it unchanged so the next window covers the gap.

## Rule checks (in order, first failure rejects)

1. Duplicate: link or normalised title already kept today.
2. Excluded source: source in `exclude_sources` (shared and per-stock).
3. Excluded keyword: whole-word match in title or description, from `exclude_keywords`.
4. Company match (if `require_ticker_in_title`): name, alias, or ticker in the title. Names match case-insensitively; the ticker matches case-sensitively, so "ON" is not "on".
5. Analyst-firm mention (if `reject_analyst_firm_mentions`): the name appears only after "by", "from", or "at". Strip those phrases before the match in step 4.
6. Weighted keywords: sum the weights of `include_keywords` found (whole word) in title or description. Reject if the total is below `min_score`.

Per-stock overrides: `include_keywords` merge into the shared set, `exclude_keywords` and `exclude_sources` add to the shared lists, and `min_score` replaces the shared value.

In Power Automate, step 6 is an expression sum over a keyword array in the stock's rules row.

## AI Builder step

Runs only on items that pass rules 1–6, so volume is small.

Prompt inputs: company name, ticker, title, source, published time, description.

Required output (JSON, validated):

```json
{"keep": true, "event": "earnings | guidance | m&a | rating_change | legal_regulatory | management | product | background | other", "reason": "one short sentence on why it matters to an investor in this company"}
```

Rules for the prompt:
- Keep only news about the named company's business, securities, or material events. Reject items where the company is only mentioned in passing.
- Reject analyst-firm headlines unless they change a rating or price target for the company.
- Reject sponsored or promotional items.
- If unsure, reject and state why.

If the AI step fails or returns invalid JSON, fall back to the rule result (keep if rules passed) and mark the row `ai_status = fallback` so it is visible. Do not drop items silently.

## Message format (Teams)

Header per company:

```
**{Company} ({TICKER})** · {count} item(s) · {date}
```

Item line:

```
- {time} · {title} · {source} · [link]({link})
```

Optional line under each item, from the AI `reason`: `  _{reason}_`.

Maximum 10 items per message. Extra items become `_+{n} more not shown_`.

## Lists

`FeedStocks`: Ticker (text), Name (text), Aliases (text, semicolon-separated), Active (yes/no), IncludeKeywords (text, `keyword:weight;...`), ExcludeKeywords (text), ExcludeSources (text), MinScore (number).

`FeedRules`: Key (text), Value (text). Shared settings: `require_ticker_in_title`, `reject_analyst_firm_mentions`, `min_score`, `overlap_hours`, `max_lookback_hours`, the shared include and exclude keyword lists.

`FeedState`: Key (text), LastFetch (date and time, UTC).

`FeedKept`: Ticker, Title, Source, Published (UTC), Link, Description, Score, Event, Reason, AiStatus, Day.

`FeedRejects`: Ticker, Title, Source, RejectReason, Day.

## Dev scope

- 4 companies in `FeedStocks` with Active = yes (AAPL, MSFT, NVDA, plus one chosen by the client).
- Test run hourly for two days before any wider rollout.
- Compare each kept and rejected item with the Python script's output for the same window. Differences are either a bug in the flow or a prompt to tune; record which.

## Open items

- Confirm the AI Builder prompt model and credit allowance for the expected volume.
- Confirm whether the RSS connector can take the Google News search URL, or whether the HTTP action is needed (the design assumes HTTP).
- Decide who owns the SharePoint site and the flow after handover.
- Decide the Teams destination: channel or chat, and the team and channel names.
- Decide whether the Python script stays as a reference or is retired.
