# PPP Flight Board

A personal flight tracker for Whitsunday Coast Airport (PPP). Shows today's
arrivals and departures paired by aircraft rotation, so a delayed arrival's
knock-on effect on its onward departure is visible at a glance.

- `scraper/scrape.py` — fetches live PPP arrivals/departures, isolates
  today's rows, pairs each arrival with its rotation departure using
  `rotation_pairs.csv`, and writes `docs/data.json`.
- `scraper/rotation_pairs.csv` — the authoritative arrival→departure
  lookup, derived directly from `flight_schedule_paired.xlsx`'s header
  labels (e.g. "MEL 832/833"). 9 rotations, covering every route/carrier
  across the full week.
- `.github/workflows/scrape.yml` — runs the scraper every 10 minutes via
  GitHub Actions and commits the updated `data.json`.
- `docs/` — the PWA itself (served by GitHub Pages): `index.html`,
  `manifest.json`, `sw.js`, icons, and the live `data.json`.

## About rotation_pairs.csv

Each row says which departure flight number uses the same aircraft as a
given arrival (e.g. arrival 834 → departure 835, Brisbane, Jetstar). The
scraper matches by **flight number only**, not carrier code — the live
board's carrier prefix is treated as authoritative over the schedule's
carrier label.

This matters because of one known discrepancy: the schedule labels the
Monday/Tuesday Cairns rotation **"CNS 81/82 (Jetstar)"**, but the live
FIDS board has consistently shown that flight as **QN81** (Skytrans'
code), not JQ81. Matching by number means this doesn't break the pairing,
but it's worth resolving with the airport directly if you want the
`carrier`/`carrier_code_schedule` columns to be fully trustworthy.

If a new flight number ever appears that isn't in this CSV, `scrape.py`
falls back to the old N+1-same-carrier heuristic automatically.

## One-time setup

1. **Create a new GitHub repo** (public or private, either works) and push
   this whole folder to it:
   ```bash
   cd ppp-tracker
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```

2. **Turn on GitHub Pages**: repo → Settings → Pages → under "Build and
   deployment", set Source to "Deploy from a branch", branch `main`,
   folder `/docs`. Save. GitHub will give you a URL like
   `https://<your-username>.github.io/<repo-name>/`.

3. **Turn on Actions** (usually on by default for a new repo): repo →
   Actions tab → if prompted, click "I understand my workflows, enable
   them".

4. **Run the scraper once manually** to populate real data instead of the
   empty placeholder: Actions tab → "Update PPP flight data" → "Run
   workflow" → Run workflow. Watch the run — if it fails, open the log; see
   *Troubleshooting* below.

5. **Install on your tablet**: open the GitHub Pages URL from step 2 in
   Chrome on the tablet, then use the browser menu → "Add to Home screen".
   It'll behave like a normal app icon from then on.

## Troubleshooting the first run

I built and unit-tested the parsing/pairing logic against a mock version of
the page (since my dev sandbox can't reach the live airport site directly),
but I haven't been able to run it against the *real* `creativeten.com.au`
HTML. It's very likely fine — the parser uses `pandas.read_html`, which
only needs generic `<table>` tags and doesn't care about exact CSS classes
— but if the first Action run fails or produces an empty `data.json`:

1. Open the failed run's log (Actions tab → the run → the "Run scraper"
   step) and check the error.
2. Most likely fix: the day-heading text format differs slightly from what
   `DAY_RE` in `scrape.py` expects (e.g. `"Sep 7"` vs `"SEP 08"`). Paste the
   log output back to me and I'll adjust the regex.
3. Less likely: the site's tables don't carry proper `<table>` headers,
   in which case I'd switch to BeautifulSoup with explicit selectors — I'd
   need to see a snippet of the actual page HTML to write those.

## Notes

- The scraper only ever writes *today's* flights (Brisbane/PPP time,
  no DST) — tomorrow's board rows are ignored.
- "Tight turnaround" (red) means an arrival is more than 15 minutes late
  and less than 40 minutes remain before its paired departure's scheduled
  time, **and** that departure hasn't already been pushed back to
  compensate. It's a heuristic, not official airline data — always trust
  the actual departure board over this flag.
- If a departure has no matching arrival (e.g. the first flight of the
  day, positioning in from overnight elsewhere), it's shown on its own
  row with no linked arrival, and vice versa.
