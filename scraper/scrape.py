#!/usr/bin/env python3
"""
PPP (Whitsunday Coast Airport) flight tracker scraper.

Fetches today's live arrivals and departures from the CreativeTen FIDS
feed, pairs each arrival with the departure that (most likely) uses the
same physical aircraft, and writes a single data.json consumed by the PWA.

Pairing logic: turnarounds at PPP reliably use flight number N (arrival)
-> N+1 (departure) on the same carrier, e.g. JQ832 in / JQ833 out. This
was confirmed against the published schedule (Jetstar, Virgin, Skytrans
all follow it at PPP). If a matching N+1 departure doesn't exist, the
arrival is reported with no linked departure.
"""

import csv
import json
import re
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ARRIVALS_URL = "https://www.creativeten.com.au/CloudTenWeb/FIDS/Arrivals.aspx?AirportCode=PPP&theme=light"
DEPARTURES_URL = "https://www.creativeten.com.au/CloudTenWeb/FIDS/Departures.aspx?AirportCode=PPP&theme=light"

ROTATION_CSV = Path(__file__).resolve().parent / "rotation_pairs.csv"

BRISBANE_TZ = ZoneInfo("Australia/Brisbane")  # PPP is same offset, no DST

DAY_RE = re.compile(
    r"(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY)\s*,?\s*"
    r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s*(\d{1,2})",
    re.IGNORECASE,
)

HEADERS = {"User-Agent": "Mozilla/5.0 (PPP-flight-tracker personal script)"}


def fetch_html(url: str) -> str:
    resp = requests.get(url, timeout=25, headers=HEADERS)
    resp.raise_for_status()
    return resp.text


def today_key():
    now = datetime.now(BRISBANE_TZ)
    return now.strftime("%A").upper(), now.strftime("%b").upper(), now.day


def split_by_day(html: str) -> dict:
    """Chunk raw HTML by the day-heading text that precedes each table block."""
    matches = list(DAY_RE.finditer(html))
    chunks = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        key = (m.group(1).upper(), m.group(2).upper()[:3], int(m.group(3)))
        # Keep the first occurrence of a given day (page sometimes repeats
        # the same block for a desktop table + a mobile card layout)
        chunks.setdefault(key, html[start:end])
    return chunks


def find_table(html_chunk: str, required_cols) -> pd.DataFrame:
    """Return the first table in html_chunk whose columns look like what we want."""
    try:
        tables = pd.read_html(StringIO(html_chunk))
    except ValueError:
        return pd.DataFrame()
    for df in tables:
        cols = [str(c).strip().upper() for c in df.columns]
        if all(any(req in c for c in cols) for req in required_cols):
            df.columns = cols
            return df
    return pd.DataFrame()


def get_today_table(url: str, required_cols):
    html = fetch_html(url)
    chunks = split_by_day(html)
    weekday, month, day = today_key()
    key = (weekday, month, day)
    if key not in chunks:
        # Fall back to the first block on the page if the label didn't
        # match exactly (e.g. different month abbreviation format).
        if not chunks:
            return pd.DataFrame()
        key = next(iter(chunks))
    return find_table(chunks[key], required_cols)


def load_rotation_map() -> dict:
    """Load the authoritative arrival-number -> departure-number lookup,
    derived from the published schedule (flight_schedule_paired.xlsx).
    Falls back to an empty map (pure N+1 heuristic) if the CSV is missing.
    """
    if not ROTATION_CSV.exists():
        print(f"Warning: {ROTATION_CSV} not found, using N+1 heuristic only", file=sys.stderr)
        return {}
    mapping = {}
    with open(ROTATION_CSV, newline="") as f:
        for row in csv.DictReader(f):
            try:
                mapping[int(row["arrival_number"])] = int(row["departure_number"])
            except (KeyError, ValueError):
                continue
    return mapping


def clean_time(val: str):
    if not isinstance(val, str):
        return None
    val = val.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", val):
        return val
    return None
    if not isinstance(val, str):
        return None
    val = val.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", val):
        return val
    return None


def clean_str(val) -> str | None:
    """Coerce a pandas cell to a plain string, treating NaN/blank as None."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    return s


def flight_number(code: str):
    """Split 'JQ832' -> ('JQ', 832)."""
    m = re.match(r"([A-Z]{2})\s*0*?(\d+)", str(code).strip().upper())
    if not m:
        return None, None
    return m.group(1), int(m.group(2))


def parse_arrivals(df: pd.DataFrame):
    records = []
    if df.empty:
        return records
    for _, row in df.iterrows():
        flight = clean_str(row.get("FLIGHT"))
        if not flight:
            continue
        carrier, num = flight_number(flight)
        if carrier is None:
            continue
        records.append(
            {
                "flight": flight,
                "carrier": carrier,
                "number": num,
                "origin": clean_str(row.get("FROM")),
                "sta": clean_time(row.get("STA")),
                "eta": clean_time(row.get("ETA")),
                "status": clean_str(row.get("STATUS")),
            }
        )
    return records


def parse_departures(df: pd.DataFrame):
    records = []
    if df.empty:
        return records
    for _, row in df.iterrows():
        flight = clean_str(row.get("FLIGHT"))
        if not flight:
            continue
        carrier, num = flight_number(flight)
        if carrier is None:
            continue
        records.append(
            {
                "flight": flight,
                "carrier": carrier,
                "number": num,
                "destination": clean_str(row.get("TO")),
                "std": clean_time(row.get("STD")),
                "etd": clean_time(row.get("ETD")),
                "status": clean_str(row.get("STATUS")),
            }
        )
    return records


def pair_rotations(arrivals, departures, rotation_map=None):
    """Pair each arrival with its rotation departure.

    Primary source: rotation_map (arrival_number -> departure_number),
    derived from the published schedule. Departures are matched by flight
    *number* only (not carrier code), since the live board's carrier
    prefix is treated as authoritative over the schedule's carrier label
    (see rotation_pairs.csv note re: CNS 81/82).

    Falls back to the N+1-same-carrier heuristic for any arrival not
    covered by rotation_map (e.g. a new/unscheduled flight number).
    """
    rotation_map = rotation_map or {}
    dep_by_number = {}
    for d in departures:
        dep_by_number.setdefault(d["number"], []).append(d)
    dep_by_carrier_number = {(d["carrier"], d["number"]): d for d in departures}

    paired = []
    used_departures = set()

    for arr in arrivals:
        dep = None
        expected_dep_num = rotation_map.get(arr["number"])
        if expected_dep_num is not None:
            candidates = dep_by_number.get(expected_dep_num, [])
            if candidates:
                dep = candidates[0]
        if dep is None:
            # Fallback: same-carrier N+1 heuristic
            dep = dep_by_carrier_number.get((arr["carrier"], arr["number"] + 1))
        if dep:
            used_departures.add((dep["carrier"], dep["number"]))
        paired.append({"arrival": arr, "departure": dep})

    # Any departures not claimed by a rotation pairing get their own row.
    for dep in departures:
        key = (dep["carrier"], dep["number"])
        if key not in used_departures:
            paired.append({"arrival": None, "departure": dep})

    def sort_key(p):
        t = None
        if p["arrival"] and p["arrival"]["eta"]:
            t = p["arrival"]["eta"]
        elif p["departure"] and p["departure"]["etd"]:
            t = p["departure"]["etd"]
        return t or "99:99"

    paired.sort(key=sort_key)
    return paired


def risk_flag(pair):
    """Flag departures at risk of being pushed back by a late-arriving aircraft."""
    arr, dep = pair["arrival"], pair["departure"]
    if not arr or not dep or not arr["sta"] or not arr["eta"]:
        return False
    try:
        sta_h, sta_m = map(int, arr["sta"].split(":"))
        eta_h, eta_m = map(int, arr["eta"].split(":"))
    except ValueError:
        return False
    delay_min = (eta_h * 60 + eta_m) - (sta_h * 60 + sta_m)
    if delay_min < 15:
        return False
    if not dep["std"]:
        return False
    try:
        std_h, std_m = map(int, dep["std"].split(":"))
    except ValueError:
        return False
    std_total = std_h * 60 + std_m
    eta_total = eta_h * 60 + eta_m
    # At risk if the (delayed) arrival now lands less than 40 min before
    # the departure's scheduled time, and the departure hasn't already
    # been pushed back to compensate.
    turnaround_buffer = std_total - eta_total
    already_adjusted = dep["etd"] and dep["etd"] != dep["std"]
    return turnaround_buffer < 40 and not already_adjusted



def load_previous_pairs(data_json_path: str) -> list:
    """Load pairs from the previous data.json, if it exists and is from today."""
    try:
        with open(data_json_path) as f:
            prev = json.load(f)
        if not prev.get("generated_at"):
            return []
        # Only use previous data if it was generated today (Brisbane time)
        gen = datetime.fromisoformat(prev["generated_at"])
        now = datetime.now(BRISBANE_TZ)
        if gen.date() != now.date():
            return []
        return prev.get("pairs", [])
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return []


def carry_forward_arrivals(new_pairs: list, prev_pairs: list) -> list:
    """
    For any arrival that was in prev_pairs but is missing from new_pairs,
    carry it forward if its paired departure hasn't yet departed.

    This handles the FIDS dropping landed arrivals from the board before
    the turnaround departure has pushed back.
    """
    if not prev_pairs:
        return new_pairs

    now_brisbane = datetime.now(BRISBANE_TZ)
    now_minutes = now_brisbane.hour * 60 + now_brisbane.minute

    # Index current pairs by departure flight number (the stable key once
    # the arrival drops off)
    new_dep_flights = {
        p["departure"]["flight"]
        for p in new_pairs
        if p.get("departure")
    }
    new_arr_flights = {
        p["arrival"]["flight"]
        for p in new_pairs
        if p.get("arrival")
    }

    carried = 0
    for prev_pair in prev_pairs:
        arr = prev_pair.get("arrival")
        dep = prev_pair.get("departure")
        if not arr:
            continue  # nothing to carry forward

        arr_flight = arr.get("flight")
        dep_flight = dep.get("flight") if dep else None

        # Skip if this arrival is already in the new data
        if arr_flight in new_arr_flights:
            continue

        # Skip if the paired departure has also already left the board
        # (meaning the whole rotation is done)
        if dep_flight and dep_flight not in new_dep_flights:
            # Check if the departure's ETD has passed — if so, rotation complete
            etd = dep.get("etd") or dep.get("std")
            if etd:
                try:
                    h, m = map(int, etd.split(":"))
                    dep_minutes = h * 60 + m
                    if now_minutes > dep_minutes + 15:
                        continue  # departed >15 min ago, drop the pair
                except ValueError:
                    pass
            continue  # departure gone from board, treat rotation as complete

        # Arrival has landed but departure is still on the board —
        # carry the arrival forward with Landed status
        carried_arr = dict(arr)
        if not carried_arr.get("status") or "landed" not in carried_arr["status"].lower():
            carried_arr["status"] = "Landed"
        carried_arr["delayed"] = False

        # Find the matching new pair (departure only, no arrival yet) and inject
        injected = False
        for new_pair in new_pairs:
            new_dep = new_pair.get("departure")
            if new_dep and new_dep.get("flight") == dep_flight and not new_pair.get("arrival"):
                new_pair["arrival"] = carried_arr
                injected = True
                carried += 1
                break

        if not injected and dep_flight in new_dep_flights:
            # The departure exists in a paired row — still inject if arrival slot empty
            for new_pair in new_pairs:
                nd = new_pair.get("departure")
                if nd and nd.get("flight") == dep_flight:
                    if not new_pair.get("arrival"):
                        new_pair["arrival"] = carried_arr
                        carried += 1
                        break

    if carried:
        print(f"Carried forward {carried} landed arrival(s) from previous fetch")
    return new_pairs


def detect_eta_slippage(new_pairs: list, prev_pairs: list, threshold_min: int = 15) -> list:
    """
    Compare current ETAs/ETDs against the previous fetch.
    Where a flight's estimated time has slipped by more than threshold_min,
    add an 'eta_slipped' dict to the leg with:
      - prev_eta: the ETA from the last fetch
      - slip_min: how many minutes later it is now
    This lets the UI show "was 11:35" even when the FIDS board doesn't
    explicitly label the flight as delayed.
    """
    if not prev_pairs:
        return new_pairs

    def to_min(t):
        if not t:
            return None
        try:
            h, m = map(int, t.split(":"))
            return h * 60 + m
        except ValueError:
            return None

    # Build lookup: flight_code -> (eta_or_etd, sta_or_std)
    prev_arr = {p["arrival"]["flight"]: p["arrival"]
                for p in prev_pairs if p.get("arrival")}
    prev_dep = {p["departure"]["flight"]: p["departure"]
                for p in prev_pairs if p.get("departure")}

    slipped = 0
    for pair in new_pairs:
        arr = pair.get("arrival")
        dep = pair.get("departure")

        if arr and arr.get("flight") in prev_arr:
            prev = prev_arr[arr["flight"]]
            cur_eta  = to_min(arr.get("eta") or arr.get("sta"))
            prev_eta = to_min(prev.get("eta") or prev.get("sta"))
            if cur_eta is not None and prev_eta is not None:
                slip = cur_eta - prev_eta
                if slip >= threshold_min:
                    arr["eta_slipped"] = {
                        "prev_eta": prev.get("eta") or prev.get("sta"),
                        "slip_min": slip,
                    }
                    # Also mark as delayed if the board hasn't already
                    arr["delayed"] = True
                    slipped += 1
                elif slip <= -threshold_min:
                    # Flight moved earlier — also worth flagging
                    arr["eta_slipped"] = {
                        "prev_eta": prev.get("eta") or prev.get("sta"),
                        "slip_min": slip,  # negative = earlier
                    }
                    slipped += 1

        if dep and dep.get("flight") in prev_dep:
            prev = prev_dep[dep["flight"]]
            cur_etd  = to_min(dep.get("etd") or dep.get("std"))
            prev_etd = to_min(prev.get("etd") or prev.get("std"))
            if cur_etd is not None and prev_etd is not None:
                slip = cur_etd - prev_etd
                if abs(slip) >= threshold_min:
                    dep["etd_slipped"] = {
                        "prev_etd": prev.get("etd") or prev.get("std"),
                        "slip_min": slip,
                    }
                    if slip > 0:
                        dep["delayed"] = True
                    slipped += 1

    if slipped:
        print(f"Detected {slipped} ETA/ETD slippage(s) vs previous fetch")
    return new_pairs


HISTORY_JSON = "docs/history.json"

def append_to_history(pairs: list, generated_at: str) -> None:
    """
    Append a snapshot of the current finalised flight states to history.json.

    Structure:
      { "date": "YYYY-MM-DD", "snapshots": [ { "at": "HH:MM", "flights": [...] } ] }

    One file per day (keyed by date). Each scrape run appends a snapshot only
    when something meaningful has changed vs the previous snapshot — i.e. at
    least one flight's ETA/ETD, status, or presence has changed — to avoid
    filling the file with identical rows.

    A flight record in history:
      { "flight", "kind" (arr/dep), "route", "scheduled", "estimated",
        "status", "delayed", "at_risk", "recorded_at" }
    """
    import os

    now = datetime.fromisoformat(generated_at)
    today_str = now.strftime("%Y-%m-%d")
    time_str  = now.strftime("%H:%M")

    # Build flat list of flight records from current pairs
    def flight_record(leg, kind, at_risk):
        if not leg:
            return None
        sched = leg.get("sta") or leg.get("std")
        est   = leg.get("eta") or leg.get("etd")
        return {
            "flight":    leg.get("flight"),
            "kind":      kind,
            "route":     leg.get("origin") or leg.get("destination"),
            "scheduled": sched,
            "estimated": est,
            "status":    leg.get("status"),
            "delayed":   leg.get("delayed", False),
            "at_risk":   at_risk,
        }

    current_flights = []
    for p in pairs:
        rec = flight_record(p.get("arrival"),   "arr", p.get("at_risk", False))
        if rec:
            current_flights.append(rec)
        rec = flight_record(p.get("departure"), "dep", p.get("at_risk", False))
        if rec:
            current_flights.append(rec)

    # Load existing history
    try:
        with open(HISTORY_JSON) as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}

    # Get or create today's entry
    day_entry = history.get(today_str, {"date": today_str, "snapshots": []})
    snapshots = day_entry["snapshots"]

    # Only append if something changed vs the last snapshot
    def flights_key(flights):
        return {r["flight"]: (r["estimated"], r["status"]) for r in flights}

    if snapshots:
        prev_key = flights_key(snapshots[-1]["flights"])
        curr_key = flights_key(current_flights)
        if prev_key == curr_key:
            return  # nothing changed, skip

    snapshots.append({"at": time_str, "flights": current_flights})
    day_entry["snapshots"] = snapshots
    history[today_str] = day_entry

    # Prune entries older than 90 days to keep the file from growing forever
    cutoff = (now.replace(tzinfo=None) - __import__('datetime').timedelta(days=90)).strftime("%Y-%m-%d")
    history = {k: v for k, v in history.items() if k >= cutoff}

    with open(HISTORY_JSON, "w") as f:
        json.dump(history, f, indent=2)

    print(f"History updated: {today_str} now has {len(snapshots)} snapshot(s)")

def main():
    try:
        arr_df = get_today_table(ARRIVALS_URL, ["FLIGHT", "FROM", "STA", "ETA"])
        dep_df = get_today_table(DEPARTURES_URL, ["FLIGHT", "TO", "STD", "ETD"])
    except requests.RequestException as e:
        print(f"Fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    arrivals = parse_arrivals(arr_df)
    departures = parse_departures(dep_df)
    rotation_map = load_rotation_map()
    pairs = pair_rotations(arrivals, departures, rotation_map)

    # Carry forward any arrivals that landed and dropped off the FIDS board
    # but whose departure is still upcoming / on the board.
    data_json_path = "docs/data.json"
    prev_pairs = load_previous_pairs(data_json_path)
    pairs = carry_forward_arrivals(pairs, prev_pairs)
    pairs = detect_eta_slippage(pairs, prev_pairs)

    for p in pairs:
        p["at_risk"] = risk_flag(p)
        if p["arrival"]:
            p["arrival"]["delayed"] = bool(
                p["arrival"]["status"] and "delay" in p["arrival"]["status"].lower()
            )
        if p["departure"]:
            p["departure"]["delayed"] = bool(
                p["departure"]["status"] and "delay" in p["departure"]["status"].lower()
            )

    out = {
        "generated_at": datetime.now(BRISBANE_TZ).isoformat(),
        "airport": "PPP",
        "pairs": pairs,
    }

    with open("docs/data.json", "w") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {len(pairs)} rows to docs/data.json")

    # Append to running history log
    append_to_history(pairs, out["generated_at"])


if __name__ == "__main__":
    main()
