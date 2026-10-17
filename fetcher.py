#!/usr/bin/env python3
"""Auto-fetch lottery results -> POST to Worker /api/ingest.

Sources (public result pages; numbers are facts republished from the
Directorate of Nagaland State Lotteries / Kerala State Lotteries Dept):
  - Nagaland (3 draws): https://www.nagalandstatelottery.org/
  - Kerala (daily 3pm): https://keralalottery.co.com/
Official PDF verification: https://www.nagalandlotteries.com/old_results/
Polite: runs every 15 min, 2 small GETs per run (+ HEADs for PDFs).
"""
import json, os, re, sys, html as ihtml
from datetime import datetime, timezone, timedelta

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
IST = timezone(timedelta(hours=5, minutes=30))
NAG_URL = "https://www.nagalandstatelottery.org/"
KER_URL = "https://keralalottery.co.com/"
NL_PDF_BASE = "https://www.nagalandlotteries.com/old_results/"
STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".fetcher-state.json")

def today_ist():
    return datetime.now(IST).strftime("%Y-%m-%d")

def text_of(html):
    t = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = ihtml.unescape(t)
    return re.sub(r"\s+", " ", t).strip()

SAMBAD_URLS = {
    "1pm": "https://sambad.com/today-1pm",
    "6pm": "https://sambad.com/today-6pm",
    "8pm": "https://sambad.com/today-8pm",
}

def parse_sambad_nagaland(html, slot):
    """Parse Nagaland result from sambad.com (faster source). Returns draw dict. Raises ValueError."""
    # Validate freshness: "Last Updated: October 17, 2026, 1:10 PM (GMT+5:30)"
    # Reject stale pages (e.g. 6PM page showing yesterday's result with midnight timestamp)
    DRAW_HOUR = {"1pm": 13, "6pm": 18, "8pm": 20}
    um = re.search(r"Last Updated:(?:</strong>)?\s*(\w+) (\d+), (\d{4}), (\d+):(\d+) (AM|PM)", html)
    if um:
        months = {"January": "01", "February": "02", "March": "03", "April": "04", "May": "05", "June": "06",
                  "July": "07", "August": "08", "September": "09", "October": "10", "November": "11", "December": "12"}
        upd_date = f"{um.group(3)}-{months.get(um.group(1), '01')}-{um.group(2).zfill(2)}"
        upd_hour = int(um.group(4)) + (12 if um.group(6) == "PM" and int(um.group(4)) != 12 else 0)
        upd_hour = 0 if um.group(6) == "AM" and int(um.group(4)) == 12 else upd_hour
        today = datetime.now().strftime("%Y-%m-%d")
        # Must be updated today AND after the draw time
        if upd_date != today or upd_hour < DRAW_HOUR[slot]:
            raise ValueError(f"sambad {slot}: stale (updated {upd_date} {upd_hour}:00, need today after {DRAW_HOUR[slot]}:00)")
    # Date from title: "Lottery Sambad 1PM Result Today — 39C 06366 (17 Oct 2026) | Nagaland"
    date = None
    m = re.search(r"\((\d{1,2}) (\w{3}) (\d{4})\)", html)
    if m:
        months = {"Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04", "May": "05", "Jun": "06",
                  "Jul": "07", "Aug": "08", "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12"}
        date = f"{m.group(3)}-{months.get(m.group(2), '01')}-{m.group(1).zfill(2)}"
    if not date:
        raise ValueError(f"sambad {slot}: date not found")
    # Tiers from result table
    pattern = r"<th[^>]*>(?:<img[^>]*>)?\s*(1st|2nd|3rd|4th|5th|Consolation) Prize\s*</th><td>([^<]+)</td>"
    tiers = dict(re.findall(pattern, html))
    if "1st" not in tiers:
        raise ValueError(f"sambad {slot}: 1st prize not found")
    fm = re.match(r"([A-Z0-9]{2,4})\s*(\d{5})", tiers["1st"].strip())
    if not fm:
        raise ValueError(f"sambad {slot}: 1st prize format invalid")
    first_series, first_number = fm.group(1), fm.group(2)
    consolation = tiers.get("Consolation", "").strip().split(",")[0].strip()
    def nums(key, expected):
        n = [x.strip() for x in tiers.get(key, "").split(",") if x.strip()]
        if len(n) != expected:
            raise ValueError(f"sambad {slot}: {key} count {len(n)} != {expected}")
        return n
    second = nums("2nd", 10)
    third = nums("3rd", 10)
    fourth = nums("4th", 10)
    fifth = nums("5th", 100)
    # Draw name from page
    dm = re.search(r"<title>[^—]*—\s*[A-Z0-9]+ \d+ \(\d+ \w+ \d+\)", html)
    draw_name = ""
    return {
        "date": date, "state": "nagaland", "slot": slot,
        "draw_name": draw_name, "draw_no": "",
        "first_series": first_series, "first_number": first_number,
        "consolation_number": consolation,
        "second": second, "third": third, "fourth": fourth, "fifth": fifth,
        "source_url": SAMBAD_URLS[slot],
    }

def parse_nagaland(html):
    """Returns list of draw dicts. Raises ValueError if validation fails."""
    t = text_of(html)
    date = None
    m = re.search(r"Latest Result[ —–-]* (\d{2})\.(\d{2})\.(\d{4})", t)
    if m:
        date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if not date:
        m = re.search(r"Result Today\s*[—–-]\s*(\d{2})\.(\d{2})\.(\d{4})", t)
        if m:
            date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if not date:
        m = re.search(r"Updated (\d{4}-\d{2}-\d{2})", t)
        date = m.group(1) if m else None
    if not date:
        raise ValueError("nagaland: date not found")
    draws = []
    # Split into the three draw blocks
    blocks = re.split(r"(Morning \(1:00 PM\)|Day \(6:00 PM\)|Evening \(8:00 PM\))", t)
    # blocks = [pre, label, body, label, body, ...]
    # Each slot is parsed independently: a not-yet-published slot (placeholder
    # text) must not abort the others.
    for i in range(1, len(blocks) - 1, 2):
        label, body = blocks[i], blocks[i + 1]
        slot = {"Morning (1:00 PM)": "1pm", "Day (6:00 PM)": "6pm", "Evening (8:00 PM)": "8pm"}[label]
        # cut body at next draw label or footer marker
        body = re.split(r"Numbers are republished", body)[0]
        if "not published yet" in body.lower():
            print(f"nagaland {slot}: not published yet, skipping", flush=True)
            continue
        try:
            draws.append(_parse_nagaland_slot(date, label, slot, body))
        except ValueError as e:
            print(f"nagaland {slot}: skipped ({e})", flush=True)
    if not draws:
        raise ValueError("nagaland: no draws parsed (site layout may have changed)")
    return draws


def _parse_nagaland_slot(date, label, slot, body):
    nm = re.search(r"(Morning \(1:00 PM\)|Day \(6:00 PM\)|Evening \(8:00 PM\))\s+(.+?)\s*[·•]\s*Draw\s*(\d+)", label + " " + body)
    draw_name = nm.group(2).strip() if nm else ""
    draw_no = nm.group(3) if nm else ""
    f = re.search(r"1st Prize[ ,]*([A-Z0-9]{2,4})\s*(\d{5})", body)
    if not f:
        raise ValueError(f"nagaland {slot}: 1st prize not found")
    first_series, first_number = f.group(1), f.group(2)
    c = re.search(r"Consolation Prize[ ,]*(\d{5})", body)
    consolation = c.group(1) if c else ""
    def tier(name, n, digits):
        mm = re.search(name + r"\s+((?:\d{%d}\s*){%d})" % (digits, n), body)
        if not mm:
            raise ValueError(f"nagaland {slot}: {name} tier parse failed")
        return mm.group(1).split()
    second = tier("2nd Prize", 10, 5)
    third = tier("3rd Prize", 10, 4)
    fourth = tier("4th Prize", 10, 4)
    fm = re.search(r"5th Prize[ —–-]*\s*\d+\s*numbers\s+((?:\d{4}\s*)+)", body)
    if not fm:
        raise ValueError(f"nagaland {slot}: 5th prize parse failed")
    fifth = fm.group(1).split()
    if len(fifth) < 90:  # tolerate minor site variation, but not garbage
        raise ValueError(f"nagaland {slot}: 5th prize count {len(fifth)} too low")
    return {
        "date": date, "state": "nagaland", "slot": slot,
        "draw_name": draw_name.title() if draw_name else "", "draw_no": draw_no,
        "first_series": first_series, "first_number": first_number,
        "consolation_number": consolation,
        "second": second, "third": third, "fourth": fourth, "fifth": fifth,
        "source_url": NAG_URL,
    }

def nl_pdf_url(date, slot):
    """HEAD-check the official PDF; return URL or None."""
    ddmmyy = datetime.strptime(date, "%Y-%m-%d").strftime("%d%m%y")
    prefix = {"1pm": "MN", "8pm": "EN"}.get(slot)
    if not prefix:
        return None
    url = f"{NL_PDF_BASE}{prefix}{ddmmyy}.PDF"
    for attempt in range(2):
        try:
            r = requests.head(url, headers=UA, timeout=30, allow_redirects=True)
            if r.status_code == 200 and "pdf" in r.headers.get("content-type", "").lower():
                return url
        except Exception as e:
            print(f"pdf HEAD {url} attempt {attempt+1} failed: {e}", flush=True)
    return None

def parse_kerala(html):
    t = text_of(html)
    m = re.search(r"Today'?s result (\d{2})/(\d{2})/(\d{4})", t)
    if not m:
        raise ValueError("kerala: date not found")
    date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    code = re.search(r"\b([A-Z]{2})-(\d+)\b", t)
    draw_no = f"{code.group(1)}-{code.group(2)}" if code else ""
    nm = re.search(r"([A-Z][A-Z ]+) " + re.escape(draw_no) + r" RESULT TODAY" if draw_no else r"([A-Z][A-Z ]+) RESULT TODAY", t)
    draw_name = (nm.group(1).strip().title() + " Lottery") if nm else "Kerala Lottery"
    f = re.search(r"First prize\s+([A-Z]{2})\s*(\d{6})", t, re.I)
    if not f:
        raise ValueError("kerala: 1st prize not found")
    first_series, first_number = f.group(1).upper(), f.group(2)
    def one(label):
        mm = re.search(label + r".*?([A-Z]{2})\s*(\d{6})", t, re.I)
        return f"{mm.group(1).upper()} {mm.group(2)}" if mm else ""
    second = [one("SECOND PRIZE")] if one("SECOND PRIZE") else []
    third = [one("THIRD PRIZE")] if one("THIRD PRIZE") else []
    cm = re.search(r"CONSOLATION PRIZE.*?₹ ?[\d,]+\s+((?:[A-Z]{2}\s*\d{6}\s*)+)", t, re.I | re.S)
    consolation = []
    if cm:
        consolation = re.findall(r"[A-Z]{2}\s*\d{6}", cm.group(1))
        consolation = [" ".join(x.split()) for x in consolation]
    def tier_list(label):
        mm = re.search(label + r"\s+.*?([\d ]+?)\s*\d+\s*/\s*\d+\s*drawn", t, re.I | re.S)
        if not mm:
            return []
        return [x for x in mm.group(1).split() if re.fullmatch(r"\d{4}", x)]
    fourth, fifth = tier_list("FOURTH PRIZE"), tier_list("FIFTH PRIZE")
    ex = {"sixth": tier_list("SIXTH PRIZE"), "seventh": tier_list("SEVENTH PRIZE"),
          "eighth": tier_list("EIGHTH PRIZE"), "ninth": tier_list("NINTH PRIZE")}
    if not second or not third or len(consolation) < 10:
        raise ValueError("kerala: tier validation failed")
    return {
        "date": date, "state": "kerala", "slot": "daily",
        "draw_name": draw_name, "draw_no": draw_no,
        "first_series": first_series, "first_number": first_number,
        "consolation_extra": consolation,
        "second": second, "third": third, "fourth": fourth, "fifth": fifth,
        "extra": ex, "source_url": KER_URL,
    }

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}

# Expected draws each day: (state, slot, draw time IST, alert grace minutes).
# Matches the cron instruction: alert if the 1PM draw is still missing 45+ min
# after draw time (by ~1:45 PM), and 90+ min for the 3PM/6PM/8PM draws.
EXPECTED_DRAWS = [
    ("nagaland", "1pm", "13:00", 45),
    ("kerala", "daily", "15:00", 90),
    ("nagaland", "6pm", "18:00", 90),
    ("nagaland", "8pm", "20:00", 90),
]

def check_missing_draws(have_keys, now):
    """Print a MISSING-DRAW-ALERT line for each expected-but-unpublished draw.

    Alerts once per draw per day (recorded in the state file); re-arms when the
    draw is published. Only call when /api/latest was read successfully, so a
    site/API outage cannot look like missing draws.
    """
    state = load_state()
    today = now.strftime("%Y-%m-%d")
    alerted = state.get("missing_alerts", {})
    # keep only today's alerts for draws still unpublished
    alerted = {k: v for k, v in alerted.items()
               if k.startswith(today + "|") and tuple(k.split("|")[1:]) not in have_keys}
    for st, slot, hhmm, grace in EXPECTED_DRAWS:
        if (today, st, slot) in have_keys:
            continue
        hh, mm = map(int, hhmm.split(":"))
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=grace)
        key = f"{today}|{st}|{slot}"
        if now >= due and key not in alerted:
            print(f"MISSING-DRAW-ALERT {st} {slot} {today}: result not published; "
                  f"expected by {due.strftime('%-I:%M %p')} IST, sources checked and still missing",
                  flush=True)
            alerted[key] = now.isoformat()
    state["missing_alerts"] = alerted
    try:
        json.dump(state, open(STATE, "w"))
    except Exception as e:
        print(f"state save failed: {e}", flush=True)

def main():
    base = os.environ.get("SITE_URL", "").rstrip("/")
    key = os.environ.get("INGEST_KEY", "")
    if not base or not key:
        print("SITE_URL and INGEST_KEY env required", file=sys.stderr)
        sys.exit(2)
    # fetch sources
    draws = []
    # Nagaland: try sambad.com first (faster), fall back to nagalandstatelottery.org
    sambad_ok = True
    for slot, url in SAMBAD_URLS.items():
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
            d = parse_sambad_nagaland(r.text, slot)
            draws.append(d)
            print(f"nagaland {slot}: parsed from sambad ({d['date']})", flush=True)
        except Exception as e:
            print(f"nagaland {slot} sambad FAILED: {e}", flush=True)
            sambad_ok = False
    if not sambad_ok or not draws:
        try:
            r = requests.get(NAG_URL, headers=UA, timeout=30)
            r.raise_for_status()
            old_draws = parse_nagaland(r.text)
            # only add slots not already from sambad
            have_slots = {(d["date"], d["slot"]) for d in draws}
            for d in old_draws:
                if (d["date"], d["slot"]) not in have_slots:
                    draws.append(d)
            print(f"nagaland: fallback parsed {len(old_draws)} draws", flush=True)
        except Exception as e:
            print(f"nagaland fallback FAILED: {e}", flush=True)
    try:
        r = requests.get(KER_URL, headers=UA, timeout=30)
        r.raise_for_status()
        kd = parse_kerala(r.text)
        draws.append(kd)
        print(f"kerala: parsed {kd['draw_no']} {kd['date']}", flush=True)
    except Exception as e:
        print(f"kerala FAILED: {e}", flush=True)
    # official PDF links for nagaland 1pm/8pm
    for d in draws:
        if d["state"] == "nagaland":
            pdf = nl_pdf_url(d["date"], d["slot"])
            if pdf:
                d["pdf_url"] = pdf
    # diff against what's published
    have_ok = True
    try:
        have = requests.get(base + "/api/latest", timeout=20).json()
        have_keys = {(h["date"], h["state"], h["slot"]) for h in have}
    except Exception as e:
        print(f"api/latest failed: {e}; will push all", flush=True)
        have_keys = set()
        have_ok = False
    # alert (once per draw) if an expected draw is overdue and still missing
    if have_ok:
        check_missing_draws(have_keys, datetime.now(IST))
    pushed = 0
    for d in draws:
        if (d["date"], d["state"], d["slot"]) in have_keys:
            continue
        try:
            rr = requests.post(base + "/api/ingest", json=d,
                               headers={"x-ingest-key": key}, timeout=30)
            print(f"ingest {(d['date'], d['state'], d['slot'])} -> {rr.status_code}", flush=True)
            if rr.status_code == 200:
                pushed += 1
        except Exception as e:
            print(f"ingest failed: {e}", flush=True)
    print(f"done. pushed={pushed}", flush=True)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        for name, url, fn in [("nagaland", NAG_URL, parse_nagaland), ("kerala", KER_URL, parse_kerala)]:
            try:
                r = requests.get(url, headers=UA, timeout=30)
                out = fn(r.text)
                ds = out if isinstance(out, list) else [out]
                for d in ds:
                    print(name, d["date"], d["state"], d["slot"], d.get("draw_no"), d["first_series"], d["first_number"],
                          "| 2nd:", len(d["second"]), "3rd:", len(d["third"]), "4th:", len(d["fourth"]), "5th:", len(d["fifth"]))
            except Exception as e:
                print(name, "FAILED:", e)
    else:
        main()
