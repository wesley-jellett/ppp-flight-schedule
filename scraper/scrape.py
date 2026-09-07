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


if __name__ == "__main__":
    main()
