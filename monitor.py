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

PINS_PAGE = "https://ilwu502.ca/greaseboard/work-pins/"
BOARD_430_PAGE = "https://ilwu502.ca/greaseboard/work-board-430pm/"
BOARD_8AM_PAGE = "https://ilwu502.ca/greaseboard/work-board-8am/"
BOARD_1AM_PAGE = "https://ilwu502.ca/greaseboard/work-board-1am/"
BCMEA_FORECAST_PAGE = "https://workinfo.bcmea.com/#/forecasts"

STATE_FILE = Path("state.json")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ILWU502BoardMonitor/1.0)",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def fetch(url):
    last_error = None

    for attempt in range(3):
        try:
            sep = "&" if "?" in url else "?"
            request_url = f"{url}{sep}_monitor_ts={time.time_ns()}"

            print(f"Fetching {url} - attempt {attempt + 1}/3")

            req = urllib.request.Request(
                request_url,
                headers=HEADERS,
            )

            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode(
                    "utf-8",
                    errors="replace",
                )

        except Exception as e:
            last_error = e
            print(
                f"Fetch failed for {url}: {e}"
            )

            if attempt < 2:
                print("Waiting 5 seconds before retry...")
                time.sleep(5)

    raise last_error

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
        pattern = r"(\d+)\s*(?:LASHERS?|LASH)\b"
    else:
        return 0

    return sum(int(x) for x in re.findall(pattern, text))

def telegram(text):
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
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
            

def send_current_update(h, b, b8, b_1, nw):
    lines = [
        "📊 CURRENT ILWU UPDATE",
        "",
        f"🚢 H BOARD: {h['value']}",
        f'🔗 <a href="{PINS_PAGE}">View work pins</a>'
        "",
        f"🌅 8 AM: {b8['total']} jobs",
        f"📋 4:30 PM: {b['total']} jobs",
        f"🌙 1 AM: {b_1['total']} jobs",
        "",
        "📋 4:30 BREAKDOWN",
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
            marker = "🚨🔥"
        elif qty >= 25:
            marker = "🔥"
        else:
            marker = "•"

        lines.append(f"{marker} {row['date']}: {qty} gangs")
    lines.append(f'🔗 <a href="{BCMEA_FORECAST_PAGE}">View BCMEA forecast</a>')

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


HISTORY_FILE = Path("history.json")


def load_history():
    if not HISTORY_FILE.exists():
        return {
            "board_430": [],
            "board_8am": [],
            "board_1am": [],
            "pin_moves": [],
            "bcmea_forecasts": [],
        }

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    for key in [
        "board_430",
        "board_8am",
        "board_1am",
        "pin_moves",
        "bcmea_forecasts",
    ]:
        history.setdefault(key, [])
    return history


def save_history(history):
    HISTORY_FILE.write_text(
        json.dumps(history, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def record_board_history(history, history_key, board, captured_at):
    entry = {
        "captured_at": captured_at,
        "board_time": board["modified"],
        "total": board["total"],
        "gang_jobs": board["gang_total"],
        "ship_jobs": board["ship_jobs_total"],
        "auto_dr": board["auto_dr_total"],
        "containers": board["container_total"],
        "container_ht": board["container_ht_total"],
        "container_lashers": board["container_lashers_total"],
        "rated": board["rated_total"],
        "fsd": board["fsd_total"],
        "deltaport": board["dp_total"],
    }

    rows = history.setdefault(history_key, [])
    if rows:
        previous = rows[-1]
        comparison_keys = [key for key in entry if key != "captured_at"]
        if all(previous.get(key) == entry.get(key) for key in comparison_keys):
            return False

    rows.append(entry)
    return True


def record_bcmea_forecast_history(history, forecast, captured_at):
    snapshots = history.setdefault("bcmea_forecasts", [])

    # Save only genuine forecast revisions. Repeated successful polls of the
    # same forecast update its freshness in state.json but do not create
    # duplicate historical snapshots.
    if snapshots and snapshots[-1].get("forecast") == forecast:
        return False

    snapshots.append({
        "captured_at": captured_at,
        "forecast": forecast,
    })
    return True


def format_delta(new_value, old_value):
    if old_value is None:
        return ""

    difference = new_value - old_value
    if difference > 0:
        return f" ⬆️ +{difference}"
    if difference < 0:
        return f" ⬇️ -{abs(difference)}"
    return " ➡️ no change"


def build_change_lines(changes):
    lines = []

    for label, new_value, old_value in changes:
        if old_value is None or new_value == old_value:
            continue

        difference = new_value - old_value
        marker = "⬆️" if difference > 0 else "⬇️"
        sign = "+" if difference > 0 else "-"
        lines.append(
            f"{label}: {old_value} → {new_value} "
            f"({marker} {sign}{abs(difference)})"
        )

    if not lines:
        return ["No job-count changes detected."]

    return lines


def find_pin_board(gb, board_letter):
    wanted_job = f"{board_letter.upper()} BOARD"
    work_pins = gb.get("work_pins", {})

    for section in ["for_8am", "for_430pm", "for_1am"]:
        for item in work_pins.get(section, []) or []:
            if str(item.get("job", "")).strip().upper() == wanted_job:
                return {
                    "value": str(item.get("from", "")).strip(),
                    "to": str(item.get("to", "")).strip(),
                    "section": section,
                    "modified": work_pins.get("modified_timestamp", ""),
                }

    raise RuntimeError(f"{wanted_job} was not found in work_pins.")


def attributed_shift(detected_at):
    minutes = detected_at.hour * 60 + detected_at.minute

    if 6 * 60 + 45 <= minutes <= 15 * 60 + 14:
        return "8 AM"
    if 15 * 60 + 15 <= minutes <= 16 * 60 + 14:
        return "4:30"
    return "Graveyard"


def parse_saved_time(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("America/Vancouver"))
    except (TypeError, ValueError):
        return None


def interval_crossed_shift_boundary(previous_check, current_check):
    if previous_check is None or previous_check >= current_check:
        return None

    day = previous_check.date()
    final_day = current_check.date()

    while day <= final_day:
        for hour, minute in [(6, 45), (15, 15), (16, 15)]:
            boundary = datetime(
                day.year,
                day.month,
                day.day,
                hour,
                minute,
                tzinfo=current_check.tzinfo,
            )
            if previous_check < boundary <= current_check:
                return True
        day = day.fromordinal(day.toordinal() + 1)

    return False


def numeric_pin_movement(old_value, new_value):
    old_match = re.search(r"(\d+)\s*$", str(old_value))
    new_match = re.search(r"(\d+)\s*$", str(new_value))

    if not old_match or not new_match:
        return None

    return int(new_match.group(1)) - int(old_match.group(1))


def shift_board_for_name(shift_name, board_430, board_8am, board_1am):
    if shift_name == "8 AM":
        return board_8am
    if shift_name == "4:30":
        return board_430
    return board_1am


def make_shift_breakdown(board):
    if board is None:
        return None

    return {
        "total": board["total"],
        "gang_jobs": board["gang_total"],
        "ship_jobs": board["ship_jobs_total"],
        "auto_dr": board["auto_dr_total"],
        "containers": board["container_total"],
        "container_ht": board["container_ht_total"],
        "container_lashers": board["container_lashers_total"],
        "rated": board["rated_total"],
        "fsd": board["fsd_total"],
        "deltaport": board["dp_total"],
        "board_time": board["modified"],
    }


def record_pin_move(
    history,
    board_letter,
    old_value,
    new_value,
    detected_at,
    previous_check,
    shift_name,
    crossed_boundary,
    shift_breakdown,
):
    movement = numeric_pin_movement(old_value, new_value)
    event = {
        "detected_at": detected_at.isoformat(),
        "previous_check_at": (
            previous_check.isoformat() if previous_check is not None else None
        ),
        "board": board_letter,
        "member_position": "H-16",
        "old_pin": old_value,
        "new_pin": new_value,
        "movement": movement,
        "attributed_shift": shift_name,
        "crossed_shift_boundary": crossed_boundary,
        "attribution_is_inferred": True,
        "shift_breakdown": shift_breakdown,
    }
    history.setdefault("pin_moves", []).append(event)
    return event


def pin_move_alert(event):
    movement = event["movement"]
    if movement is None:
        movement_text = ""
    elif movement > 0:
        movement_text = f" (⬆️ +{movement})"
    elif movement < 0:
        movement_text = f" (⬇️ -{abs(movement)})"
    else:
        movement_text = ""

    crossed = event["crossed_shift_boundary"]
    if crossed is True:
        confidence = "⚠️ Uncertain — check interval crossed a shift boundary"
    elif crossed is False:
        confidence = "🟢 Detection window stayed within one shift period"
    else:
        confidence = "⚪ Confidence unknown — no previous check time was available"

    lines = [
        f"📌 {event['board']} BOARD MOVED",
        f"{event['old_pin']} → {event['new_pin']}{movement_text}",
        "",
        "Your position: H-16",
        f"Likely shift: {event['attributed_shift']}",
        confidence,
    ]

    breakdown = event["shift_breakdown"]
    if breakdown is None:
        lines.extend([
            "",
            "Shift breakdown unavailable because that source did not return fresh data.",
        ])
    else:
        lines.extend([
            "",
            f"Shift volume: {breakdown['total']} jobs",
            f"🚗 Auto drivers: {breakdown['auto_dr']}",
            (
                f"📦 Containers: {breakdown['containers']} "
                f"({breakdown['container_ht']} HT + "
                f"{breakdown['container_lashers']} lashers)"
            ),
            (
                f"🎟 Rated: {breakdown['rated']} "
                f"(FSD {breakdown['fsd']} + DP {breakdown['deltaport']})"
            ),
            f"Board time: {breakdown['board_time'] or 'unknown'}",
        ])

    lines.extend(["", f'<a href="{PINS_PAGE}">View work pins</a>'])
    return "\n".join(lines)





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

def calculate_board(gb, board_key):
    board = gb.get(board_key, {})
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
    last_error = None

    for attempt in range(3):
        try:
            print(f"Fetching {url} - attempt {attempt + 1}/3")

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": HEADERS["User-Agent"],
                    "Accept": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(
                    r.read().decode("utf-8")
                )

        except Exception as e:
            last_error = e
            print(
                f"Fetch failed for {url}: {e}"
            )

            if attempt < 2:
                print("Waiting 5 seconds before retry...")
                time.sleep(5)

    raise last_error

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


HISTORY_FILE = Path("history.json")


def load_history():
    if not HISTORY_FILE.exists():
        return {
            "board_430": [],
            "board_8am": [],
            "board_1am": [],
            "pin_moves": [],
            "bcmea_forecasts": [],
        }

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    for key in [
        "board_430",
        "board_8am",
        "board_1am",
        "pin_moves",
        "bcmea_forecasts",
    ]:
        history.setdefault(key, [])
    return history


def save_history(history):
    HISTORY_FILE.write_text(
        json.dumps(history, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def record_board_history(history, history_key, board, captured_at):
    entry = {
        "captured_at": captured_at,
        "board_time": board["modified"],
        "total": board["total"],
        "gang_jobs": board["gang_total"],
        "ship_jobs": board["ship_jobs_total"],
        "auto_dr": board["auto_dr_total"],
        "containers": board["container_total"],
        "container_ht": board["container_ht_total"],
        "container_lashers": board["container_lashers_total"],
        "rated": board["rated_total"],
        "fsd": board["fsd_total"],
        "deltaport": board["dp_total"],
    }

    rows = history.setdefault(history_key, [])
    if rows:
        previous = rows[-1]
        comparison_keys = [key for key in entry if key != "captured_at"]
        if all(previous.get(key) == entry.get(key) for key in comparison_keys):
            return False

    rows.append(entry)
    return True


def record_bcmea_forecast_history(history, forecast, captured_at):
    snapshots = history.setdefault("bcmea_forecasts", [])

    # Save only genuine forecast revisions. Repeated successful polls of the
    # same forecast update its freshness in state.json but do not create
    # duplicate historical snapshots.
    if snapshots and snapshots[-1].get("forecast") == forecast:
        return False

    snapshots.append({
        "captured_at": captured_at,
        "forecast": forecast,
    })
    return True


def format_delta(new_value, old_value):
    if old_value is None:
        return ""

    difference = new_value - old_value
    if difference > 0:
        return f" ⬆️ +{difference}"
    if difference < 0:
        return f" ⬇️ -{abs(difference)}"
    return " ➡️ no change"


def build_change_lines(changes):
    lines = []

    for label, new_value, old_value in changes:
        if old_value is None or new_value == old_value:
            continue

        difference = new_value - old_value
        marker = "⬆️" if difference > 0 else "⬇️"
        sign = "+" if difference > 0 else "-"
        lines.append(
            f"{label}: {old_value} → {new_value} "
            f"({marker} {sign}{abs(difference)})"
        )

    if not lines:
        return ["No job-count changes detected."]

    return lines


def find_pin_board(gb, board_letter):
    wanted_job = f"{board_letter.upper()} BOARD"
    work_pins = gb.get("work_pins", {})

    for section in ["for_8am", "for_430pm", "for_1am"]:
        for item in work_pins.get(section, []) or []:
            if str(item.get("job", "")).strip().upper() == wanted_job:
                return {
                    "value": str(item.get("from", "")).strip(),
                    "to": str(item.get("to", "")).strip(),
                    "section": section,
                    "modified": work_pins.get("modified_timestamp", ""),
                }

    raise RuntimeError(f"{wanted_job} was not found in work_pins.")


def attributed_shift(detected_at):
    minutes = detected_at.hour * 60 + detected_at.minute

    if 6 * 60 + 45 <= minutes <= 15 * 60 + 14:
        return "8 AM"
    if 15 * 60 + 15 <= minutes <= 16 * 60 + 14:
        return "4:30"
    return "Graveyard"


def parse_saved_time(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(ZoneInfo("America/Vancouver"))
    except (TypeError, ValueError):
        return None


def interval_crossed_shift_boundary(previous_check, current_check):
    if previous_check is None or previous_check >= current_check:
        return None

    day = previous_check.date()
    final_day = current_check.date()

    while day <= final_day:
        for hour, minute in [(6, 45), (15, 15), (16, 15)]:
            boundary = datetime(
                day.year,
                day.month,
                day.day,
                hour,
                minute,
                tzinfo=current_check.tzinfo,
            )
            if previous_check < boundary <= current_check:
                return True
        day = day.fromordinal(day.toordinal() + 1)

    return False


def numeric_pin_movement(old_value, new_value):
    old_match = re.search(r"(\d+)\s*$", str(old_value))
    new_match = re.search(r"(\d+)\s*$", str(new_value))

    if not old_match or not new_match:
        return None

    return int(new_match.group(1)) - int(old_match.group(1))


def shift_board_for_name(shift_name, board_430, board_8am, board_1am):
    if shift_name == "8 AM":
        return board_8am
    if shift_name == "4:30":
        return board_430
    return board_1am


def make_shift_breakdown(board):
    if board is None:
        return None

    return {
        "total": board["total"],
        "gang_jobs": board["gang_total"],
        "ship_jobs": board["ship_jobs_total"],
        "auto_dr": board["auto_dr_total"],
        "containers": board["container_total"],
        "container_ht": board["container_ht_total"],
        "container_lashers": board["container_lashers_total"],
        "rated": board["rated_total"],
        "fsd": board["fsd_total"],
        "deltaport": board["dp_total"],
        "board_time": board["modified"],
    }


def record_pin_move(
    history,
    board_letter,
    old_value,
    new_value,
    detected_at,
    previous_check,
    shift_name,
    crossed_boundary,
    shift_breakdown,
):
    movement = numeric_pin_movement(old_value, new_value)
    event = {
        "detected_at": detected_at.isoformat(),
        "previous_check_at": (
            previous_check.isoformat() if previous_check is not None else None
        ),
        "board": board_letter,
        "member_position": "H-16",
        "old_pin": old_value,
        "new_pin": new_value,
        "movement": movement,
        "attributed_shift": shift_name,
        "crossed_shift_boundary": crossed_boundary,
        "attribution_is_inferred": True,
        "shift_breakdown": shift_breakdown,
    }
    history.setdefault("pin_moves", []).append(event)
    return event


def pin_move_alert(event):
    movement = event["movement"]
    if movement is None:
        movement_text = ""
    elif movement > 0:
        movement_text = f" (⬆️ +{movement})"
    elif movement < 0:
        movement_text = f" (⬇️ -{abs(movement)})"
    else:
        movement_text = ""

    crossed = event["crossed_shift_boundary"]
    if crossed is True:
        confidence = "⚠️ Uncertain — check interval crossed a shift boundary"
    elif crossed is False:
        confidence = "🟢 Detection window stayed within one shift period"
    else:
        confidence = "⚪ Confidence unknown — no previous check time was available"

    lines = [
        f"📌 {event['board']} BOARD MOVED",
        f"{event['old_pin']} → {event['new_pin']}{movement_text}",
        "",
        "Your position: H-16",
        f"Likely shift: {event['attributed_shift']}",
        confidence,
    ]

    breakdown = event["shift_breakdown"]
    if breakdown is None:
        lines.extend([
            "",
            "Shift breakdown unavailable because that source did not return fresh data.",
        ])
    else:
        lines.extend([
            "",
            f"Shift volume: {breakdown['total']} jobs",
            f"🚗 Auto drivers: {breakdown['auto_dr']}",
            (
                f"📦 Containers: {breakdown['containers']} "
                f"({breakdown['container_ht']} HT + "
                f"{breakdown['container_lashers']} lashers)"
            ),
            (
                f"🎟 Rated: {breakdown['rated']} "
                f"(FSD {breakdown['fsd']} + DP {breakdown['deltaport']})"
            ),
            f"Board time: {breakdown['board_time'] or 'unknown'}",
        ])

    lines.extend(["", f'<a href="{PINS_PAGE}">View work pins</a>'])
    return "\n".join(lines)




if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
