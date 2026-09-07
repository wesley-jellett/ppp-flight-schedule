"""Local-only test: exercises scrape.py's parsing/pairing logic against
mock HTML fixtures, without hitting the network. Not part of the
production scraper — delete or ignore once you've confirmed the real
site's structure works after the first GitHub Actions run.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import scrape

with open("test_fixture_arrivals.html") as f:
    arr_html = f.read()
with open("test_fixture_departures.html") as f:
    dep_html = f.read()

arr_chunks = scrape.split_by_day(arr_html)
dep_chunks = scrape.split_by_day(dep_html)
print("Arrival day blocks found:", list(arr_chunks.keys()))
print("Departure day blocks found:", list(dep_chunks.keys()))

weekday, month, day = scrape.today_key()
key = (weekday, month, day)
print("Looking for today's key:", key)

arr_df = scrape.find_table(arr_chunks[key], ["FLIGHT", "FROM", "STA", "ETA"])
dep_df = scrape.find_table(dep_chunks[key], ["FLIGHT", "TO", "STD", "ETD"])

arrivals = scrape.parse_arrivals(arr_df)
departures = scrape.parse_departures(dep_df)
print(f"\nParsed {len(arrivals)} arrivals, {len(departures)} departures")

rotation_map = scrape.load_rotation_map()
print(f"Loaded {len(rotation_map)} rotations from schedule CSV")

pairs = scrape.pair_rotations(arrivals, departures, rotation_map)
for p in pairs:
    p["at_risk"] = scrape.risk_flag(p)

print("\n--- Paired rows ---")
for p in pairs:
    a = p["arrival"]
    d = p["departure"]
    a_str = f"{a['flight']} {a['origin']} STA{a['sta']} ETA{a['eta']} {a['status'] or ''}" if a else "—"
    d_str = f"{d['flight']} {d['destination']} STD{d['std']} ETD{d['etd']} {d['status'] or ''}" if d else "—"
    print(f"{a_str:55} | {d_str:55} | at_risk={p['at_risk']}")

assert len(arrivals) == 6, f"expected 6 arrivals, got {len(arrivals)}"
assert len(departures) == 6, f"expected 6 departures, got {len(departures)}"
assert len(pairs) == 6, f"expected 6 paired rows, got {len(pairs)}"

# JQ834 (arrival) should pair with JQ835 (departure)
jq834_pair = next(p for p in pairs if p["arrival"] and p["arrival"]["flight"] == "JQ834")
assert jq834_pair["departure"]["flight"] == "JQ835", "rotation pairing failed for JQ834->JQ835"
print("\n✓ JQ834 correctly paired with JQ835 (N+1 rotation rule works)")

print("\nAll assertions passed.")
