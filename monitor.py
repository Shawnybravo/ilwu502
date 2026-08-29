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
            

def load_state():
    if not STATE_FILE.exists():
        return {}

    return json.loads(
        STATE_FILE.read_text(encoding="utf-8")
    )

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


def morning_volume_marker(total, shift_name):
    if shift_name == "8 AM":
        if total >= 300:
            return "🚨"
        if total >= 250:
            return "🔥🔥"
        if total >= 200:
            return "🔥"
        return ""

    if shift_name == "Graveyard":
        if total >= 250:
            return "🚨"
        if total >= 200:
            return "🔥🔥"
        if total >= 150:
            return "🔥"
        return ""

    if total > 200:
        return "🔥"
    return ""


def recent_pin_move_lines(history, local_now, hours=15):
    cutoff_timestamp = local_now.timestamp() - hours * 60 * 60
    recent = []

    for event in history.get("pin_moves", []):
        detected = parse_saved_time(event.get("detected_at"))
        if detected is None or detected.timestamp() < cutoff_timestamp:
            continue

        movement = event.get("movement")
        if movement is None:
            movement_text = ""
        elif movement > 0:
            movement_text = f" (⬆️ +{movement})"
        elif movement < 0:
            movement_text = f" (⬇️ -{abs(movement)})"
        else:
            movement_text = ""

        recent.append(
            f"{event.get('board', '?')}: "
            f"{event.get('old_pin', '?')} → {event.get('new_pin', '?')}"
            f"{movement_text} — likely {event.get('attributed_shift', 'unknown')}"
        )

    return recent


def morning_briefing_text(h, t, board_430, board_8am, board_1am, forecast, history, local_now):
    pin_lines = recent_pin_move_lines(history, local_now)
    marker_8 = morning_volume_marker(board_8am["total"], "8 AM")
    marker_430 = morning_volume_marker(board_430["total"], "4:30")
    marker_1 = morning_volume_marker(board_1am["total"], "Graveyard")

    lines = [
        "☀️ ILWU MORNING BRIEFING",
        "",
        "📌 WORK PINS",
        f"H Board: {h['value']}",
        f"T Board: {t['value']}",
    ]

    if pin_lines:
        lines.append("Recent movement:")
        lines.extend(pin_lines)
    else:
        lines.append("Recent movement: none detected")

    lines.extend([
        "",
        "📊 CURRENT BOARDS",
        f"😴 8 AM: {board_8am['total']} jobs {marker_8}".rstrip(),
        f"📋 4:30: {board_430['total']} jobs {marker_430}".rstrip(),
        f"⚰️ Graveyard: {board_1am['total']} jobs {marker_1}".rstrip(),
        "",
        "📈 BCMEA NW FORECAST",
    ])

    for row in forecast[:3]:
        quantity = row["quantity"]
        if quantity >= 30:
            marker = "🚨🔥"
        elif quantity >= 25:
            marker = "🔥"
        else:
            marker = "•"
        lines.append(f"{marker} {row['date']}: {quantity} gangs")

    lines.extend([
        "",
        "🟢 All briefing sources returned fresh data",
    ])
    return "\n".join(lines)



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



def main():
    state = load_state()
    history = load_history()
    history_changed = False

    # Each source starts unavailable. A source is assigned a value only after
    # its fetch AND parsing/calculation both succeed.
    h = None
    t = None
    b = None
    b8 = None
    b_1 = None
    nw = None

    print("Fetching H and T work pins...")
    try:
        pins_gb = extract_gbdata(fetch(PINS_URL))
        h = find_pin_board(pins_gb, "H")
        t = find_pin_board(pins_gb, "T")
    except Exception as e:
        print(
            f"WARNING: H/T work pins failed after retries: {e} "
            "-- keeping previous H and T pin state"
        )

    print("Fetching 4:30 board...")
    try:
        board_gb = extract_gbdata(fetch(BOARD_URL))
        b = calculate_board(board_gb, "work_board_430pm")
    except Exception as e:
        print(
            f"WARNING: 4:30 board failed after retries: {e} "
            "-- keeping all previous 4:30 state"
        )

    print("Fetching 8 AM board...")
    try:
        board_8am_gb = extract_gbdata(fetch(BOARD_8AM_URL))
        b8 = calculate_board(board_8am_gb, "work_board_8am")
    except Exception as e:
        print(
            f"WARNING: 8 AM board failed after retries: {e} "
            "-- keeping previous 8 AM state"
        )

    print("Fetching graveyard board...")
    try:
        board_1am_gb = extract_gbdata(fetch(BOARD_1AM_URL))
        b_1 = calculate_board(board_1am_gb, "work_board_1am")
    except Exception as e:
        print(
            f"WARNING: graveyard board failed after retries: {e} "
            "-- keeping previous graveyard state"
        )

    print("Fetching BCMEA NW forecast...")
    try:
        nw = normalize_nw_forecast(fetch_json(BCMEA_NW_URL))
    except Exception as e:
        print(
            f"WARNING: BCMEA forecast failed after retries: {e} "
            "-- keeping previous BCMEA state"
        )

    local_now = datetime.now(timezone.utc).astimezone(
        ZoneInfo("America/Vancouver")
    )
    today = local_now.date().isoformat()

    # H/T PINS: log and alert only from a fresh successful Pins response.
    if h is not None and t is not None:
        pins_success_at = datetime.now(timezone.utc)
        pins_detected_local = pins_success_at.astimezone(
            ZoneInfo("America/Vancouver")
        )
        previous_pins_check = parse_saved_time(state.get("pins_last_success"))
        shift_name = attributed_shift(pins_detected_local)
        crossed_boundary = interval_crossed_shift_boundary(
            previous_pins_check,
            pins_detected_local,
        )
        shift_board = shift_board_for_name(shift_name, b, b8, b_1)
        shift_breakdown = make_shift_breakdown(shift_board)

        for board_letter, current_pin, state_key in [
            ("H", h, "h_board"),
            ("T", t, "t_board"),
        ]:
            old_value = state.get(state_key)
            new_value = current_pin["value"]

            # The first successful run establishes the baseline without
            # creating a false movement event.
            if (
                previous_pins_check is not None
                and old_value is not None
                and new_value != old_value
            ):
                event = record_pin_move(
                    history=history,
                    board_letter=board_letter,
                    old_value=old_value,
                    new_value=new_value,
                    detected_at=pins_detected_local,
                    previous_check=previous_pins_check,
                    shift_name=shift_name,
                    crossed_boundary=crossed_boundary,
                    shift_breakdown=shift_breakdown,
                )
                history_changed = True
                telegram(pin_move_alert(event))

        state["h_board"] = h["value"]
        state["h_modified"] = h["modified"]
        state["h_last_success"] = pins_success_at.isoformat()
        state["t_board"] = t["value"]
        state["t_modified"] = t["modified"]
        state["t_last_success"] = pins_success_at.isoformat()
        state["pins_last_success"] = pins_success_at.isoformat()

    # 4:30: this entire compare/alert/update path uses fresh 4:30 data only.
    if b is not None:
        board_430_success_at = datetime.now(timezone.utc).isoformat()
        old_430_modified = state.get("board_430_modified")
        old_430_total = state.get("board_430_total")
        changed_430 = (
            b["modified"] != old_430_modified
            or b["total"] != old_430_total
        )

        if old_430_modified is not None and changed_430:
            headline = "📋 4:30 BOARD UPDATED"
            if b["total"] > 200:
                headline += " — 🔥 OVER 200 JOBS"

            change_lines = build_change_lines([
                ("🚗 Auto drivers", b["auto_dr_total"], state.get("board_430_auto_dr_total")),
                ("📦 Containers", b["container_total"], state.get("board_430_container_total")),
                ("   HT", b["container_ht_total"], state.get("board_430_container_ht_total")),
                ("   Lashers", b["container_lashers_total"], state.get("board_430_container_lashers_total")),
                ("🎟 Rated jobs", b["rated_total"], state.get("board_430_rated_total")),
                ("   FSD", b["fsd_total"], state.get("board_430_fsd_total")),
                ("   Deltaport", b["dp_total"], state.get("board_430_dp_total")),
            ])
            change_text = "\n".join(change_lines)

            telegram(
                f"{headline}\n"
                f"Total: {b['total']} jobs"
                f"{format_delta(b['total'], old_430_total)}\n\n"
                "CHANGES\n"
                f"{change_text}\n\n"
                f"🚗 Auto drivers (DR): {b['auto_dr_total']}\n"
                f"📦 Containers: {b['container_total']} "
                f"({b['container_ht_total']} HT + "
                f"{b['container_lashers_total']} lashers)\n"
                f"🎟 Rated jobs: {b['rated_total']} "
                f"(FSD {b['fsd_total']} + Deltaport {b['dp_total']})\n\n"
                f"Gang job breakdowns: {b['gang_total']}\n"
                f"Ship jobs: {b['ship_jobs_total']}\n"
                f"Board time: {b['modified'] or 'unknown'}\n"
                f'<a href="{BOARD_430_PAGE}">View 4:30 board</a>'
            )

        state.update({
            "board_430_total": b["total"],
            "board_430_modified": b["modified"],
            "board_430_auto_dr_total": b["auto_dr_total"],
            "board_430_container_total": b["container_total"],
            "board_430_container_ht_total": b["container_ht_total"],
            "board_430_container_lashers_total": b["container_lashers_total"],
            "board_430_rated_total": b["rated_total"],
            "board_430_fsd_total": b["fsd_total"],
            "board_430_dp_total": b["dp_total"],
            "board_430_last_success": board_430_success_at,
        })
        history_changed = record_board_history(
            history, "board_430", b, board_430_success_at
        ) or history_changed

    # 8 AM is deliberately outside the 4:30 block.
    if b8 is not None:
        board_8am_success_at = datetime.now(timezone.utc).isoformat()
        old_8am_modified = state.get("board_8am_modified")
        old_8am_total = state.get("board_8am_total")
        changed_8am = (
            b8["modified"] != old_8am_modified
            or b8["total"] != old_8am_total
        )

        if (
            old_8am_modified is not None
            and changed_8am
            and b8["total"] >= 200
        ):
            if b8["total"] >= 300:
                level = "🚨 HUGE"
            elif b8["total"] >= 250:
                level = "🔥🔥 VERY BUSY"
            else:
                level = "🔥 BUSY"

            change_lines = build_change_lines([
                ("🚗 Auto drivers", b8["auto_dr_total"], state.get("board_8am_auto_dr_total")),
                ("📦 Containers", b8["container_total"], state.get("board_8am_container_total")),
                ("   HT", b8["container_ht_total"], state.get("board_8am_container_ht_total")),
                ("   Lashers", b8["container_lashers_total"], state.get("board_8am_container_lashers_total")),
                ("🎟 Rated jobs", b8["rated_total"], state.get("board_8am_rated_total")),
                ("   FSD", b8["fsd_total"], state.get("board_8am_fsd_total")),
                ("   Deltaport", b8["dp_total"], state.get("board_8am_dp_total")),
            ])
            change_text = "\n".join(change_lines)

            telegram(
                f"😴 8 AM BOARD — {level}\n"
                f"Total: {b8['total']} jobs"
                f"{format_delta(b8['total'], old_8am_total)}\n\n"
                "CHANGES\n"
                f"{change_text}\n\n"
                f"🚗 Auto drivers: {b8['auto_dr_total']}\n"
                f"📦 Containers: {b8['container_total']} "
                f"({b8['container_ht_total']} HT + "
                f"{b8['container_lashers_total']} lashers)\n"
                f"🎟 Rated: {b8['rated_total']} "
                f"(FSD {b8['fsd_total']} + DP {b8['dp_total']})\n"
                f"Board time: {b8['modified'] or 'unknown'}\n"
                f'<a href="{BOARD_8AM_PAGE}">View 8 AM board</a>'
            )

        state.update({
            "board_8am_total": b8["total"],
            "board_8am_modified": b8["modified"],
            "board_8am_auto_dr_total": b8["auto_dr_total"],
            "board_8am_container_total": b8["container_total"],
            "board_8am_container_ht_total": b8["container_ht_total"],
            "board_8am_container_lashers_total": b8["container_lashers_total"],
            "board_8am_rated_total": b8["rated_total"],
            "board_8am_fsd_total": b8["fsd_total"],
            "board_8am_dp_total": b8["dp_total"],
            "board_8am_last_success": board_8am_success_at,
        })
        history_changed = record_board_history(
            history, "board_8am", b8, board_8am_success_at
        ) or history_changed

    # Graveyard is deliberately outside the 4:30 block.
    if b_1 is not None:
        board_1am_success_at = datetime.now(timezone.utc).isoformat()
        old_1am_modified = state.get("board_1am_modified")
        old_1am_total = state.get("board_1am_total")
        changed_1am = (
            b_1["modified"] != old_1am_modified
            or b_1["total"] != old_1am_total
        )

        if (
            old_1am_modified is not None
            and changed_1am
            and b_1["total"] >= 150
        ):
            if b_1["total"] >= 250:
                level = "🚨 HUGE"
            elif b_1["total"] >= 200:
                level = "🔥🔥 VERY BUSY"
            else:
                level = "🔥 BUSY"

            change_lines = build_change_lines([
                ("🚗 Auto drivers", b_1["auto_dr_total"], state.get("board_1am_auto_dr_total")),
                ("📦 Containers", b_1["container_total"], state.get("board_1am_container_total")),
                ("   HT", b_1["container_ht_total"], state.get("board_1am_container_ht_total")),
                ("   Lashers", b_1["container_lashers_total"], state.get("board_1am_container_lashers_total")),
                ("🎟 Rated jobs", b_1["rated_total"], state.get("board_1am_rated_total")),
                ("   FSD", b_1["fsd_total"], state.get("board_1am_fsd_total")),
                ("   Deltaport", b_1["dp_total"], state.get("board_1am_dp_total")),
            ])
            change_text = "\n".join(change_lines)

            telegram(
                f"⚰️ 1 AM BOARD — {level}\n"
                f"Total: {b_1['total']} jobs"
                f"{format_delta(b_1['total'], old_1am_total)}\n\n"
                "CHANGES\n"
                f"{change_text}\n\n"
                f"🚗 Auto drivers: {b_1['auto_dr_total']}\n"
                f"📦 Containers: {b_1['container_total']} "
                f"({b_1['container_ht_total']} HT + "
                f"{b_1['container_lashers_total']} lashers)\n"
                f"🎟 Rated: {b_1['rated_total']} "
                f"(FSD {b_1['fsd_total']} + DP {b_1['dp_total']})\n"
                f"Board time: {b_1['modified'] or 'unknown'}\n"
                f'<a href="{BOARD_1AM_PAGE}">View graveyard board</a>'
            )

        state.update({
            "board_1am_total": b_1["total"],
            "board_1am_modified": b_1["modified"],
            "board_1am_auto_dr_total": b_1["auto_dr_total"],
            "board_1am_container_total": b_1["container_total"],
            "board_1am_container_ht_total": b_1["container_ht_total"],
            "board_1am_container_lashers_total": b_1["container_lashers_total"],
            "board_1am_rated_total": b_1["rated_total"],
            "board_1am_fsd_total": b_1["fsd_total"],
            "board_1am_dp_total": b_1["dp_total"],
            "board_1am_last_success": board_1am_success_at,
        })
        history_changed = record_board_history(
            history, "board_1am", b_1, board_1am_success_at
        ) or history_changed

    # BCMEA: compare and update only after a fresh successful response.
    if nw is not None:
        bcmea_success_at = datetime.now(timezone.utc).isoformat()
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

                lines.append(f"{marker} {row['date']}: {qty} gangs")

                if qty >= 25:
                    busy_days.append(row)

            lines.append(
                f'<a href="{BCMEA_FORECAST_PAGE}">View BCMEA forecast</a>'
            )

            if busy_days:
                lines.append("")
                lines.append("Busy forecast:")

                for row in busy_days:
                    label = "VERY BUSY" if row["quantity"] >= 30 else "BUSY"
                    lines.append(
                        f"{row['date']}: {row['quantity']} gangs — {label}"
                    )

            telegram("\n".join(lines))

        state["bcmea_nw_forecast"] = nw
        state["bcmea_last_success"] = bcmea_success_at
        history_changed = record_bcmea_forecast_history(
            history,
            nw,
            bcmea_success_at,
        ) or history_changed

    # Send the daily message only when both sources used by it are fresh.
    # If either fails at 1 PM, do not mark today as sent; a later run can retry.
    last_daily_status = state.get("last_daily_status")
    if local_now.hour == 13 and last_daily_status != today:
        if h is not None and b is not None:
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
        else:
            print(
                "WARNING: Daily status deferred because H board or 4:30 "
                "fresh data is unavailable"
            )

    # Send one morning briefing on the first fully successful run from
    # 6:30 AM through 7:59 AM Vancouver time.
    minutes_now = local_now.hour * 60 + local_now.minute
    last_morning_briefing = state.get("last_morning_briefing")
    if (
        6 * 60 + 30 <= minutes_now < 8 * 60
        and last_morning_briefing != today
    ):
        required_sources = [h, t, b, b8, b_1, nw]

        if all(source is not None for source in required_sources):
            telegram(
                morning_briefing_text(
                    h=h,
                    t=t,
                    board_430=b,
                    board_8am=b8,
                    board_1am=b_1,
                    forecast=nw,
                    history=history,
                    local_now=local_now,
                )
            )
            state["last_morning_briefing"] = today
        else:
            print(
                "WARNING: Morning briefing deferred because one or more "
                "required sources did not return fresh data"
            )

    # Successful sources have already updated their own keys. Failed sources
    # never touched their keys, so their previous state remains unchanged.
    save_state(state)
    if history_changed or not HISTORY_FILE.exists():
        save_history(history)

    if h is not None:
        print(f"H BOARD: {h['value']} ({h['modified']})")
    if t is not None:
        print(f"T BOARD: {t['value']} ({t['modified']})")
    if b is not None:
        print(
            f"4:30 total: {b['total']} = "
            f"gang jobs {b['gang_total']} + "
            f"ship jobs {b['ship_jobs_total']} + "
            f"FSD {b['fsd_total']} + "
            f"DP {b['dp_total']}"
        )
    if b8 is not None:
        print(f"8 AM total: {b8['total']}")
    if b_1 is not None:
        print(f"1 AM total: {b_1['total']}")
    if nw is not None:
        print(f"BCMEA forecast rows: {len(nw)}")




if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise
