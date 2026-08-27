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
BOARD_8AM_URL = "https://ilwu502.ca/greaseboard/work-board-8am/?gb_data_refresh"
BOARD_URL = "https://ilwu502.ca/greaseboard/work-board-430pm/?gb_data_refresh"
BOARD_1AM_URL = "https://ilwu502.ca/greaseboard/work-board-1am/?gb_data_refresh"
BCMEA_NW_URL = "https://corpreports.bcmea.com/corp_report_webapi/reports/forecast/NW"
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

def count_job_code(text, code):
    text = str(text or "").upper()

    if code == "DR":
        pattern = r"(\d+)\s*DR\b"
    elif code == "HT":
        pattern = r"(\d+)\s*HT\b"
    elif code == "LASHERS":
        pattern = r"(\d+)\s*LASHERS?\b"
    else:
        return 0

    return sum(int(x) for x in re.findall(pattern, text))

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

def get_telegram_updates(offset=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

    if offset is not None:
        url += f"?offset={offset}"

    req = urllib.request.Request(url)

    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read().decode())

    if not body.get("ok"):
        raise RuntimeError(f"Telegram getUpdates error: {body}")

    return body.get("result", [])

def send_current_update(h, b, nw):
    lines = [
        "📊 CURRENT ILWU UPDATE",
        "",
        f"🚢 H BOARD: {h['value']}",
        "",
        f"📋 4:30 BOARD: {b['total']} jobs",
        f"🚗 Auto drivers (DR): {b['auto_dr_total']}",
        (
            f"📦 Containers: {b['container_total']} "
            f"({b['container_ht_total']} HT + "
            f"{b['container_lashers_total']} lashers)"
        ),
        (
            f"🎟 Rated jobs: {b['rated_total']} "
            f"(FSD {b['fsd_total']} + Deltaport {b['dp_total']})"
        ),
        "",
        "📈 BCMEA NW FORECAST",
    ]

    for row in nw:
        qty = row["quantity"]

        if qty >= 30:
            marker = "🚨"
        elif qty >= 25:
            marker = "🔥"
        else:
            marker = "•"

        lines.append(
            f"{marker} {row['date']}: {qty} gangs"
        )

    telegram("\n".join(lines))

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

def gang_job_sum(text):
    text = str(text or "").strip()

    # Plain gang counts describe crews, not additional posted jobs.
    # Examples: "1 GANG", "4 GANGS", "#1"
    if re.fullmatch(r"#?\s*\d+\s+GANGS?", text, flags=re.I):
        return 0

    # Otherwise the GANGS field contains actual job quantities,
    # e.g. "1HT 1WD 79DR 1MECH 1MRNCHK".
    return numeric_sum(text)

def calculate_430(gb):
    board = gb.get("work_board_430pm", {})
    ships = board.get("ships_in_port", []) or []

    gang_jobs_total = sum(
        gang_job_sum(ship.get("gangs", ""))
        for ship in ships
    )

    ship_jobs_total = sum(
        numeric_sum(ship.get("jobs", ""))
        for ship in ships
    )

    # AUTO SHIPS: count DR only
    auto_dr_total = 0

    # CONTAINER SHIPS: count HT + lashers
    container_ht_total = 0
    container_lashers_total = 0

    for ship in ships:
        commodity = str(ship.get("commodities", "")).upper()
        gangs = ship.get("gangs", "")
        jobs = ship.get("jobs", "")

        if "AUTO" in commodity:
            auto_dr_total += count_job_code(gangs, "DR")
            auto_dr_total += count_job_code(jobs, "DR")

        if "CONTAINER" in commodity:
            container_ht_total += count_job_code(gangs, "HT")
            container_ht_total += count_job_code(jobs, "HT")

            container_lashers_total += count_job_code(gangs, "LASHERS")
            container_lashers_total += count_job_code(jobs, "LASHERS")

    container_total = container_ht_total + container_lashers_total

    # Rated jobs from the tables
    fsd_total = qty_sum(board.get("fsd_jobs", []))
    dp_total = qty_sum(board.get("dp_jobs", []))
    rated_total = fsd_total + dp_total

    total = (
        gang_jobs_total
        + ship_jobs_total
        + rated_total
    )

    return {
        "total": total,
        "gang_total": gang_jobs_total,
        "ship_jobs_total": ship_jobs_total,

        "auto_dr_total": auto_dr_total,

        "container_ht_total": container_ht_total,
        "container_lashers_total": container_lashers_total,
        "container_total": container_total,

        "fsd_total": fsd_total,
        "dp_total": dp_total,
        "rated_total": rated_total,

        "modified": board.get("modified_timestamp", ""),
    }

def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": HEADERS["User-Agent"],
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def normalize_nw_forecast(rows):
    forecast = []

    for row in rows or []:
        forecast.append({
            "day": str(row.get("day", "")).strip(),
            "date": str(row.get("date", "")).strip(),
            "quantity": int(row.get("quantity", 0) or 0),
            "status": row.get("status"),
        })

    return forecast

def main():
    state = load_state()
    pins_gb = extract_gbdata(fetch(PINS_URL))
    board_gb = extract_gbdata(fetch(BOARD_URL))

    h = find_h_board(pins_gb)
    b = calculate_430(board_gb)
    nw = normalize_nw_forecast(fetch_json(BCMEA_NW_URL))

    # Telegram commands
    last_telegram_update_id = state.get("telegram_update_id", 0)

    updates = get_telegram_updates(last_telegram_update_id + 1)

    for update in updates:
        update_id = update.get("update_id", 0)
        message = update.get("message", {})
        text = str(message.get("text", "")).strip().lower()
        chat_id = str(message.get("chat", {}).get("id", ""))

        if chat_id == str(CHAT_ID):
            if text in ["update", "/update"]:
                send_current_update(h, b, nw)

        last_telegram_update_id = max(
            last_telegram_update_id,
            update_id
        )

    state["telegram_update_id"] = last_telegram_update_id


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
            f"Total: {b['total']} jobs\n\n"

            f"🚗 Auto drivers (DR): {b['auto_dr_total']}\n"
            f"📦 Containers: {b['container_total']} "
            f"({b['container_ht_total']} HT + "
            f"{b['container_lashers_total']} lashers)\n"
            f"🎟 Rated jobs: {b['rated_total']} "
            f"(FSD {b['fsd_total']} + Deltaport {b['dp_total']})\n\n"

            f"Gang job breakdowns: {b['gang_total']}\n"
            f"Ship jobs: {b['ship_jobs_total']}\n"
            f"Board time: {b['modified'] or 'unknown'}"
        )

    old_nw = state.get("bcmea_nw_forecast")

    if old_nw is not None and nw != old_nw:
        lines = ["📈 BCMEA NW FORECAST UPDATED"]

        busy_days = []

        for row in nw:
            qty = row["quantity"]

            if qty >= 30:
                marker = "🚨"
            elif qty >= 25:
                marker = "🔥"
            else:
                marker = "•"

            lines.append(
                f"{marker} {row['date']}: {qty} gangs"
            )

            if qty >= 25:
                busy_days.append(row)

        if busy_days:
            lines.append("")
            lines.append("Busy forecast:")

            for row in busy_days:
                label = "VERY BUSY" if row["quantity"] >= 30 else "BUSY"
                lines.append(
                    f"{row['date']}: {row['quantity']} gangs — {label}"
                )

        telegram("\n".join(lines))

    # Daily 1 PM status message.
    last_daily_status = state.get("last_daily_status")
    if local_now.hour == 13 and last_daily_status != today:
        telegram(
            "🕐 1 PM ILWU BOARD STATUS\n\n"
            f"🚢 H BOARD: {h['value']}\n\n"
            f"📋 4:30 BOARD: {b['total']} jobs\n"
            f"🚗 Auto drivers (DR): {b['auto_dr_total']}\n"
            f"📦 Containers: {b['container_total']} "
            f"({b['container_ht_total']} HT + "
            f"{b['container_lashers_total']} lashers)\n"
            f"🎟 Rated jobs: {b['rated_total']} "
            f"(FSD {b['fsd_total']} + Deltaport {b['dp_total']})\n\n"
            f"Board time: {b['modified'] or 'unknown'}"
        )
        state["last_daily_status"] = today

    state.update({
        "h_board": h["value"],
        "h_modified": h["modified"],
        "board_430_total": b["total"],
        "board_430_modified": b["modified"],
        "bcmea_nw_forecast": nw,
    })
    save_state(state)

    print(f"H BOARD: {h['value']} ({h['modified']})")
    print(
    f"4:30 total: {b['total']} = "
    f"gang jobs {b['gang_total']} + "
    f"ship jobs {b['ship_jobs_total']} + "
    f"FSD {b['fsd_total']} + "
    f"DP {b['dp_total']}"
)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
