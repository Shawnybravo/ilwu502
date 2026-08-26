import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import urllib.parse
import urllib.request
from pathlib import Path

PINS_URL = "https://ilwu502.ca/greaseboard/work-pins/?gb_data_refresh"
BOARD_URL = "https://ilwu502.ca/greaseboard/work-board-430pm/?gb_data_refresh"
STATE_FILE = Path("state.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ILWU502BoardMonitor/1.0)",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def fetch(url):
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}_monitor_ts={time.time_ns()}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")

def extract_gbdata(html):
    m = re.search(
        r"var\s+gbData\s*=\s*(\{.*?\})\s*;\s*console\.log\s*\(\s*gbData\s*\)",
        html,
        flags=re.S,
    )
    if not m:
        m = re.search(r"var\s+gbData\s*=\s*(\{.*\})\s*;\s*</script>", html, flags=re.S)
    if not m:
        raise RuntimeError("Could not find embedded gbData in ILWU HTML.")
    return json.loads(m.group(1))

def telegram(text):
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode())
        if not body.get("ok"):
            raise RuntimeError(f"Telegram error: {body}")

def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def find_h_board(gb):
    wp = gb.get("work_pins", {})
    for key in ["for_8am", "for_430pm", "for_1am"]:
        for item in wp.get(key, []) or []:
            if str(item.get("job", "")).strip().upper() == "H BOARD":
                return {
                    "value": str(item.get("from", "")).strip(),
                    "to": str(item.get("to", "")).strip(),
                    "section": key,
                    "modified": wp.get("modified_timestamp", ""),
                }
    raise RuntimeError("H BOARD was not found in work_pins.")

def numeric_sum(text):
    return sum(int(x) for x in re.findall(r"\d+", str(text or "")))

def qty_sum(rows):
    total = 0
    for row in rows or []:
        raw = str(row.get("qty", "")).strip()
        if re.fullmatch(r"\d+", raw):
            total += int(raw)
    return total

def calculate_430(gb):
    board = gb.get("work_board_430pm", {})
    gang_total = sum(numeric_sum(ship.get("gangs", "")) for ship in (board.get("ships_in_port", []) or []))
    fsd_total = qty_sum(board.get("fsd_jobs", []))
    dp_total = qty_sum(board.get("dp_jobs", []))
    rated_total = fsd_total + dp_total
    return {
        "total": gang_total + rated_total,
        "gang_total": gang_total,
        "rated_total": rated_total,
        "fsd_total": fsd_total,
        "dp_total": dp_total,
        "modified": board.get("modified_timestamp", ""),
    }

def main():
    state = load_state()
    pins_gb = extract_gbdata(fetch(PINS_URL))
    board_gb = extract_gbdata(fetch(BOARD_URL))

    h = find_h_board(pins_gb)
    b = calculate_430(board_gb)
    local_now = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Vancouver"))
    today = local_now.date().isoformat()
    old_h = state.get("h_board")
    if old_h is not None and h["value"] != old_h:
        telegram(
            "🚢 H BOARD UPDATED\n"
            f"{old_h} → {h['value']}\n"
            f"Board time: {h['modified'] or 'unknown'}"
        )
    old_430_modified = state.get("board_430_modified")
    old_430_total = state.get("board_430_total")

    changed = (
        b["modified"] != old_430_modified
        or b["total"] != old_430_total
    )

    if old_430_modified is not None and changed:
        headline = "📋 4:30 BOARD UPDATED"
        if b["total"] > 200:
            headline += " — 🔥 OVER 200 JOBS"

        telegram(
            f"{headline}\n"
            f"Total: {b['total']} jobs\n"
            f"Gangs: {b['gang_total']}\n"
            f"Rated jobs: {b['rated_total']}\n"
            f"FSD: {b['fsd_total']}\n"
            f"DP: {b['dp_total']}\n"
            f"Board time: {b['modified'] or 'unknown'}"
        )

    # Daily 1 PM status message.
    last_daily_status = state.get("last_daily_status")
    if local_now.hour == 13 and last_daily_status != today:
        telegram(
            "🕐 1 PM ILWU BOARD STATUS\n"
            f"4:30 total: {b['total']} jobs\n"
            f"Gangs: {b['gang_total']}\n"
            f"Rated jobs: {b['rated_total']} "
            f"(FSD {b['fsd_total']} + DP {b['dp_total']})\n"
            f"H BOARD: {h['value']}\n"
            f"Board time: {b['modified'] or 'unknown'}"
        )
        state["last_daily_status"] = today

    state.update({
        "h_board": h["value"],
        "h_modified": h["modified"],
        "board_430_total": b["total"],
        "board_430_modified": b["modified"],
    })
    save_state(state)

    print(f"H BOARD: {h['value']} ({h['modified']})")
    print(f"4:30 total: {b['total']} = gangs {b['gang_total']} + rated {b['rated_total']}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
