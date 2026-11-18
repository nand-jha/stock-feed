"""Fetch Google News RSS per stock, apply per-stock rules, write kept/reject logs and per-company messages.

    python3 src/feed_filter.py --dry-run          # print new kept items, write nothing
    python3 src/feed_filter.py                    # one cycle
    python3 src/feed_filter.py --loop             # repeat every schedule.interval_seconds
    python3 src/feed_filter.py --loop --interval 300

Rules, first failing one rejects the item (reason is logged):
  1. already in today's feed file (same title or link)
  2. source in exclude_sources
  3. exclude_keywords (whole word)
  4. require_ticker_in_title: company name, alias, or ticker in the title
  5. include_keywords score below min_score

Kept items in outputs/feed-<day>.jsonl are the only dedupe state. Rejected items are not
remembered, so every cycle re-checks them and template changes apply to recent news.
Stdlib only, Python 3.9+.
"""
import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
USER_AGENT = "feed-filter/0.1"
DEFAULT_FIELDS = ["ticker", "title", "source", "published", "link", "score"]
REQUIRED_FIELDS = ("ticker", "title", "link")  # needed for dedupe and the per-company messages


# ---------- helpers ----------

def resolve(path):
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def load_json(path):
    with open(resolve(path), encoding="utf-8") as f:
        return json.load(f)


def whole_word(term, ignore_case=True):
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", flags)


def clean_text(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def item_keys(item):
    """Keys identifying an article: normalised title, plus link when present."""
    keys = {"t:" + re.sub(r"[^a-z0-9]", "", item["title"].lower())[:120]}
    if item.get("link"):
        keys.add("l:" + item["link"])
    return keys


def format_time(iso):
    try:
        return dt.datetime.fromisoformat(iso).astimezone().strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return iso or "--"


def validate(cfg):
    for st in cfg["stocks"]:
        if not st.get("ticker") or not st.get("name"):
            sys.exit(f"config error: every stock needs 'ticker' and 'name': {st}")


# ---------- feed ----------

def fetch(cfg, stock):
    feed = cfg["feed"]
    query = feed["query_template"].format(name=stock["name"], ticker=stock["ticker"],
                                          lookback=feed.get("lookback", "1h"))
    url = feed["url_template"].format(query=urllib.parse.quote_plus(query))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=feed.get("timeout_seconds", 15)) as resp:
        return resp.read()


def parse_items(xml_bytes, stock, limit):
    items = []
    for node in ET.fromstring(xml_bytes).iter("item"):
        source = node.find("source")
        pub = node.findtext("pubDate") or ""
        try:
            published = email.utils.parsedate_to_datetime(pub).isoformat()
        except (TypeError, ValueError):
            published = pub
        src = clean_text(source.text) if source is not None else ""
        title = clean_text(node.findtext("title"))
        if src and title.endswith(" - " + src):  # Google appends "- Source" to titles; the source field has it
            title = title[: -len(" - " + src)]
        items.append({
            "ticker": stock["ticker"],
            "title": title,
            "source": src,
            "published": published,
            "link": (node.findtext("link") or "").strip(),
            "description": clean_text(node.findtext("description")),
        })
        if len(items) >= limit:
            break
    return items


# ---------- rules ----------

def stock_template(base, stock):
    """Base template (shared, or the stock's own file) with the stock's overrides applied."""
    tpl = load_json(stock["template"]) if stock.get("template") else dict(base)
    tpl["include_keywords"] = {**tpl.get("include_keywords", {}), **stock.get("include_keywords", {})}
    tpl["exclude_keywords"] = [*tpl.get("exclude_keywords", []), *stock.get("exclude_keywords", [])]
    tpl["exclude_sources"] = [*tpl.get("exclude_sources", []), *stock.get("exclude_sources", [])]
    if "min_score" in stock:
        tpl["min_score"] = stock["min_score"]
    return tpl


FIRM_PHRASE = r"(?<!\w)(?:by|from|at)\s+"


class Rules:
    def __init__(self, tpl, stock):
        self.excl = [(kw, whole_word(kw)) for kw in tpl.get("exclude_keywords", [])]
        self.sources = set(tpl.get("exclude_sources", []))
        self.incl = [(w, whole_word(kw)) for kw, w in tpl.get("include_keywords", {}).items()]
        self.require_title = tpl.get("require_ticker_in_title", True)
        self.min_score = tpl.get("min_score", 1)
        # company names match case-insensitively; the ticker case-sensitively so "ON" != "on"
        names = [stock["name"], *stock.get("aliases", [])]
        self.title_pat = [whole_word(n) for n in names] + [whole_word(stock["ticker"], ignore_case=False)]
        # "Maintained by Wells Fargo": the name is the analyst firm, not the subject. Strip those phrases first.
        self.strip_firm = tpl.get("reject_analyst_firm_mentions", True)
        self.firm_pat = re.compile(FIRM_PHRASE + "(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)",
                                   re.IGNORECASE)

    def check(self, item, kept_keys):
        """Return (reason, score); reason is None when the item is kept."""
        if item_keys(item) & kept_keys:
            return "duplicate", 0
        return self._rule(item)

    def _rule(self, item):
        if item["source"] in self.sources:
            return f"excluded_source:{item['source']}", 0
        text = f"{item['title']} {item['description']}"
        for kw, pat in self.excl:
            if pat.search(text):
                return f"exclude_keyword:{kw}", 0
        if self.require_title:
            title = item["title"]
            subject = self.firm_pat.sub(" ", title) if self.strip_firm else title
            if not any(p.search(subject) for p in self.title_pat):
                firm_only = self.strip_firm and any(p.search(title) for p in self.title_pat)
                return ("analyst_firm_mention" if firm_only else "no_company_in_title"), 0
        score = sum(w for w, pat in self.incl if pat.search(text))
        if score < self.min_score:
            return f"low_score:{score}", score
        return None, score


# ---------- storage ----------

class Store:
    """One day's files: kept feed, rejects, and per-company messages. Created only when written."""

    EXT = {"feed": "jsonl", "rejects": "jsonl"}

    def __init__(self, out_dir):
        self.dir = resolve(out_dir)
        self.day = dt.date.today().isoformat()

    def path(self, name):
        return os.path.join(self.dir, f"{name}-{self.day}.{self.EXT[name]}")

    def _rows(self, name):
        if not os.path.exists(self.path(name)):
            return []
        with open(self.path(name), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def kept_keys(self):
        keys = set()
        for row in self._rows("feed"):
            keys |= item_keys(row)
        return keys

    def reject_keys(self):
        return {(r["ticker"], r["title"], r["reject_reason"]) for r in self._rows("rejects")}

    def append(self, name, rows):
        if not rows:
            return
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path(name), "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def write_messages(self, stocks, publish):
        """One Teams-ready Markdown file per company: outputs/messages/<day>/<TICKER>.md."""
        rows = self._rows("feed")
        out = os.path.join(self.dir, "messages", self.day)
        os.makedirs(out, exist_ok=True)
        cap = publish.get("max_items_per_message", 10)
        skip_empty = publish.get("skip_empty", True)
        for stock in stocks:
            mine = sorted((r for r in rows if r["ticker"] == stock["ticker"]),
                          key=lambda r: r.get("published") or "", reverse=True)
            path = os.path.join(out, f"{stock['ticker']}.md")
            if not mine and skip_empty:
                if os.path.exists(path):  # drop a stale file so the folder matches the current state
                    os.remove(path)
                continue
            lines = [publish["header"].format(name=stock["name"], ticker=stock["ticker"],
                                              count=len(mine), date=self.day)]
            if not mine:
                lines.append(publish["empty"])
            for r in mine[:cap]:
                lines.append(publish["item"].format(time=format_time(r.get("published")), title=r["title"],
                                                    source=r.get("source") or "unknown", link=r["link"]))
            if len(mine) > cap:
                lines.append(publish["more"].format(n=len(mine) - cap))
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")


# ---------- cycle ----------

def run_cycle(cfg, rules, fields, store, publish, dry_run=False):
    """One fetch+filter pass over all stocks. Returns the newly kept rows."""
    kept_keys = set() if dry_run else store.kept_keys()
    logged = set() if dry_run else store.reject_keys()
    kept, rejects = [], []

    for i, stock in enumerate(cfg["stocks"]):
        if i:  # space requests out to stay under Google's rate limit
            time.sleep(cfg["feed"].get("delay_between_requests_seconds", 3))
        try:
            items = parse_items(fetch(cfg, stock), stock, cfg["feed"].get("max_items_per_ticker", 50))
        except Exception as exc:  # one failing stock shouldn't stop the cycle
            print(f"  [warn] {stock['ticker']}: fetch failed: {exc}", file=sys.stderr)
            continue

        new = 0
        for item in items:
            reason, score = rules[stock["ticker"]].check(item, kept_keys)
            if reason is None:
                new += 1
                kept_keys |= item_keys(item)
                values = {**item, "score": score}
                kept.append({f: values.get(f) for f in fields})
            elif reason != "duplicate":
                key = (stock["ticker"], item["title"], reason)
                if key not in logged:
                    logged.add(key)
                    rejects.append({"ticker": stock["ticker"], "title": item["title"],
                                    "source": item["source"], "reject_reason": reason})
        print(f"  {stock['ticker']}: {len(items)} fetched / {new} new kept")

    kept.sort(key=lambda r: (r["ticker"], -r["score"]))
    if dry_run:
        for r in kept:
            print(json.dumps(r, ensure_ascii=False))
        return kept

    store.append("feed", kept)
    store.append("rejects", rejects)
    store.write_messages(cfg["stocks"], publish)
    return kept


def cycle_log(cfg, rules, fields, store, publish, cycle_no=None, dry_run=False):
    label = f"cycle {cycle_no}" if cycle_no else "cycle"
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {label} start")
    kept = run_cycle(cfg, rules, fields, store, publish, dry_run=dry_run)
    for r in kept:
        print(f"  + {r['ticker']} score={r['score']} {r['title']}")
    print(f"  {label} done: {len(kept)} new kept")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config/tickers.json")
    ap.add_argument("--dry-run", action="store_true", help="print new kept items, write nothing")
    ap.add_argument("--loop", action="store_true", help="repeat every interval; Ctrl-C to stop")
    ap.add_argument("--interval", type=int, help="seconds between cycles (overrides config)")
    args = ap.parse_args()

    cfg = load_json(args.config)
    validate(cfg)
    base = load_json(cfg["template"])
    rules = {st["ticker"]: Rules(stock_template(base, st), st) for st in cfg["stocks"]}
    fields = list(dict.fromkeys([*base.get("fields", DEFAULT_FIELDS), *REQUIRED_FIELDS]))
    publish = base["publish"]
    store = Store(cfg.get("output_dir", "outputs"))

    if not args.loop:
        cycle_log(cfg, rules, fields, store, publish, dry_run=args.dry_run)
        return

    interval = args.interval or cfg.get("schedule", {}).get("interval_seconds", 300)
    print(f"Running every {interval}s. Ctrl-C to stop.")
    n = 0
    try:
        while True:
            n += 1
            try:
                cycle_log(cfg, rules, fields, store, publish, cycle_no=n)
                print(f"  next in {interval}s")
            except Exception as exc:  # keep the loop alive
                print(f"  [error] cycle {n}: {exc}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
