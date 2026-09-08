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
        pattern = r"(\d+)\s*(?:DR|DRV|DRVS|DRVR|DRVRS|DRIVERS?)\b"
    elif code == "HT":
        pattern = r"(\d+)\s*HT\b"
    elif code == "LASHERS":
        pattern = r"(\d+)\s*(?:LASHERS?|LASH)\b"
    else:
        return 0

    return sum(int(value) for value in re.findall(pattern, text))


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
            "shift_outcomes": [],
            "worked_reports": [],
        }
    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    for key in [
        "board_430",
        "board_8am",
        "board_1am",
        "pin_moves",
        "bcmea_forecasts",
        "shift_outcomes",
        "worked_reports",
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
        "ship_count": board["ship_count"],
        "ship_types": board["ship_types"],
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
        "ship_count": board["ship_count"],
        "ship_types": board["ship_types"],
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
    for label, board in [
        ("8 AM", board_8am),
        ("4:30", board_430),
        ("Graveyard", board_1am),
    ]:
        if board["total"] == 0:
            lines.extend(["", f"🚢 {label} SHIPS"])
            lines.extend(ship_summary_lines(board))
    lines.extend(["", "📈 BCMEA NW FORECAST"])
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
def ship_summary_lines(board):
    lines = [f"Ships in port: {board['ship_count']}"]
    for ship_type, count in sorted(board["ship_types"].items()):
        label = ship_type.title() if ship_type != "UNKNOWN" else "Unknown type"
        lines.append(f"• {label}: {count}")
    if board["ship_count"] == 0:
        lines.append("• No ships listed")
    return lines
def notable_category_lines(history, history_key, board, now_utc):
    cutoff_timestamp = now_utc.timestamp() - 30 * 24 * 60 * 60
    recent_rows = []
    for row in history.get(history_key, []):
        captured = parse_saved_time(row.get("captured_at"))
        if captured is not None and captured.timestamp() >= cutoff_timestamp:
            recent_rows.append(row)
    # A record based on only one or two earlier boards is not meaningful.
    if len(recent_rows) < 5:
        return []
    categories = [
        ("🚗 Auto drivers", "auto_dr", board["auto_dr_total"]),
        ("📦 Containers", "containers", board["container_total"]),
        ("   HT", "container_ht", board["container_ht_total"]),
        ("   Lashers", "container_lashers", board["container_lashers_total"]),
        ("🎟 Rated jobs", "rated", board["rated_total"]),
        ("   FSD", "fsd", board["fsd_total"]),
        ("   Deltaport", "deltaport", board["dp_total"]),
    ]
    notable = []
    for label, history_field, current_value in categories:
        previous_values = [
            row.get(history_field)
            for row in recent_rows
            if isinstance(row.get(history_field), (int, float))
        ]
        if not previous_values:
            continue
        previous_high = max(previous_values)
        if current_value > previous_high:
            notable.append(
                f"{label}: {current_value} — new 30-day high "
                f"(previous {previous_high})"
            )
    return notable
def anomaly_lines(history, history_key, board, local_now):
    cutoff_timestamp = local_now.timestamp() - 30 * 24 * 60 * 60
    latest_by_day = {}
    for row in history.get(history_key, []):
        captured = parse_saved_time(row.get("captured_at"))
        if captured is None or captured.timestamp() < cutoff_timestamp:
            continue
        day_key = captured.date().isoformat()
        previous = latest_by_day.get(day_key)
        if previous is None or captured > previous[0]:
            latest_by_day[day_key] = (captured, row)
    daily_rows = [pair[1] for pair in latest_by_day.values()]
    if len(daily_rows) < 14:
        return []
    categories = [
        ("Total jobs", "total", board["total"]),
        ("🚗 Auto drivers", "auto_dr", board["auto_dr_total"]),
        ("📦 Containers", "containers", board["container_total"]),
        ("   HT", "container_ht", board["container_ht_total"]),
        ("   Lashers", "container_lashers", board["container_lashers_total"]),
        ("🎟 Rated jobs", "rated", board["rated_total"]),
        ("   FSD", "fsd", board["fsd_total"]),
        ("   Deltaport", "deltaport", board["dp_total"]),
    ]
    unusual = []
    for label, history_field, current_value in categories:
        values = [
            row.get(history_field)
            for row in daily_rows
            if isinstance(row.get(history_field), (int, float))
        ]
        if len(values) < 14:
            continue
        average = sum(values) / len(values)
        # Very small averages create exaggerated percentages and noisy alerts.
        if average < 5:
            continue
        difference = current_value - average
        percentage = difference / average * 100
        variance = sum((value - average) ** 2 for value in values) / len(values)
        standard_deviation = variance ** 0.5
        percentage_is_unusual = abs(percentage) >= 30
        statistically_unusual = (
            abs(difference) >= 2 * standard_deviation
            if standard_deviation > 0
            else difference != 0
        )
        if percentage_is_unusual and statistically_unusual:
            marker = "⬆️" if difference > 0 else "⬇️"
            unusual.append(
                f"{label}: {current_value} — normally {average:.0f} "
                f"({marker} {abs(percentage):.0f}%)"
            )
    return unusual
def shift_identity(local_time):
    minutes = local_time.hour * 60 + local_time.minute
    today = local_time.date()
    if 6 * 60 + 45 <= minutes < 15 * 60 + 15:
        return "8 AM", today
    if 15 * 60 + 15 <= minutes < 16 * 60 + 15:
        return "4:30", today
    if minutes >= 16 * 60 + 15:
        outcome_date = today.fromordinal(today.toordinal() + 1)
    else:
        outcome_date = today
    return "Graveyard", outcome_date
def shift_window(shift_name, outcome_date):
    zone = ZoneInfo("America/Vancouver")
    if shift_name == "8 AM":
        start = datetime(
            outcome_date.year, outcome_date.month, outcome_date.day,
            6, 45, tzinfo=zone,
        )
        end = datetime(
            outcome_date.year, outcome_date.month, outcome_date.day,
            15, 15, tzinfo=zone,
        )
    elif shift_name == "4:30":
        start = datetime(
            outcome_date.year, outcome_date.month, outcome_date.day,
            15, 15, tzinfo=zone,
        )
        end = datetime(
            outcome_date.year, outcome_date.month, outcome_date.day,
            16, 15, tzinfo=zone,
        )
    else:
        previous_day = outcome_date.fromordinal(outcome_date.toordinal() - 1)
        start = datetime(
            previous_day.year, previous_day.month, previous_day.day,
            16, 15, tzinfo=zone,
        )
        end = datetime(
            outcome_date.year, outcome_date.month, outcome_date.day,
            6, 45, tzinfo=zone,
        )
    return start, end
def start_shift_observation(
    shift_name,
    outcome_date,
    observed_at,
    h_value,
    t_value,
    previous_h_value=None,
    previous_t_value=None,
):
    window_start, _ = shift_window(shift_name, outcome_date)
    start_delay = max(0, (observed_at - window_start).total_seconds() / 60)
    boundary_change_detected = (
        previous_h_value is not None
        and previous_t_value is not None
        and (
            previous_h_value != h_value
            or previous_t_value != t_value
        )
    )
    return {
        "key": f"{outcome_date.isoformat()}|{shift_name}",
        "date": outcome_date.isoformat(),
        "shift": shift_name,
        "started_observing_at": observed_at.isoformat(),
        "start_delay_minutes": round(start_delay, 1),
        "h_start": (
            previous_h_value if boundary_change_detected else h_value
        ),
        "t_start": (
            previous_t_value if boundary_change_detected else t_value
        ),
        "h_last": h_value,
        "t_last": t_value,
        "last_observed_at": observed_at.isoformat(),
        "boundary_change_detected": boundary_change_detected,
    }
def finalize_shift_outcome(active, history, board):
    outcome_date = datetime.strptime(active["date"], "%Y-%m-%d").date()
    _, window_end = shift_window(active["shift"], outcome_date)
    last_observed = parse_saved_time(active.get("last_observed_at"))
    end_gap = (
        abs((window_end - last_observed).total_seconds() / 60)
        if last_observed is not None
        else None
    )
    h_movement = numeric_pin_movement(active["h_start"], active["h_last"])
    t_movement = numeric_pin_movement(active["t_start"], active["t_last"])
    h_moved = active["h_start"] != active["h_last"]
    t_moved = active["t_start"] != active["t_last"]
    coverage_confident = (
        active.get("start_delay_minutes", 9999) <= 30
        and end_gap is not None
        and end_gap <= 30
        and not active.get("boundary_change_detected", False)
    )
    breakdown = make_shift_breakdown(board)
    outcome = {
        "key": active["key"],
        "date": active["date"],
        "shift": active["shift"],
        "started_observing_at": active["started_observing_at"],
        "last_observed_at": active["last_observed_at"],
        "start_delay_minutes": active.get("start_delay_minutes"),
        "end_gap_minutes": round(end_gap, 1) if end_gap is not None else None,
        "coverage_confident": coverage_confident,
        "boundary_change_detected": active.get(
            "boundary_change_detected", False
        ),
        "h_start": active["h_start"],
        "h_end": active["h_last"],
        "h_moved": h_moved,
        "h_movement": h_movement,
        "t_start": active["t_start"],
        "t_end": active["t_last"],
        "t_moved": t_moved,
        "t_movement": t_movement,
        "any_movement": h_moved or t_moved,
        "shift_breakdown": breakdown,
    }
    outcomes = history.setdefault("shift_outcomes", [])
    if not any(existing.get("key") == outcome["key"] for existing in outcomes):
        outcomes.append(outcome)
        return True
    return False
def update_shift_outcomes(state, history, observed_at, h_value, t_value, b, b8, b_1):
    shift_name, outcome_date = shift_identity(observed_at)
    current_key = f"{outcome_date.isoformat()}|{shift_name}"
    active = state.get("active_shift_observation")
    changed = False
    if not isinstance(active, dict):
        state["active_shift_observation"] = start_shift_observation(
            shift_name, outcome_date, observed_at, h_value, t_value
        )
        return False
    if active.get("key") != current_key:
        previous_board = shift_board_for_name(active.get("shift"), b, b8, b_1)
        changed = finalize_shift_outcome(active, history, previous_board)
        state["active_shift_observation"] = start_shift_observation(
            shift_name,
            outcome_date,
            observed_at,
            h_value,
            t_value,
            previous_h_value=state.get("h_board"),
            previous_t_value=state.get("t_board"),
        )
        return changed
    active["h_last"] = h_value
    active["t_last"] = t_value
    active["last_observed_at"] = observed_at.isoformat()
    state["active_shift_observation"] = active
    return False
def weekly_daily_rows(history, history_key, local_now):
    monday = local_now.date().fromordinal(
        local_now.date().toordinal() - local_now.weekday()
    )
    week_start = datetime(
        monday.year,
        monday.month,
        monday.day,
        tzinfo=ZoneInfo("America/Vancouver"),
    )
    cutoff_timestamp = week_start.timestamp()
    latest_by_day = {}
    for row in history.get(history_key, []):
        captured = parse_saved_time(row.get("captured_at"))
        if captured is None or captured.timestamp() < cutoff_timestamp:
            continue
        day_key = captured.date().isoformat()
        previous = latest_by_day.get(day_key)
        if previous is None or captured > previous[0]:
            latest_by_day[day_key] = (captured, row)
    return [
        pair[1]
        for pair in sorted(latest_by_day.values(), key=lambda pair: pair[0])
    ]
def weekly_shift_lines(title, rows, busy_threshold):
    totals = [row["total"] for row in rows]
    average = round(sum(totals) / len(totals))
    busiest = max(rows, key=lambda row: row["total"])
    busy_count = sum(total >= busy_threshold for total in totals)
    busiest_time = parse_saved_time(busiest.get("captured_at"))
    busiest_date = "unknown date"
    if busiest_time is not None:
        busiest_date = busiest_time.strftime("%a %b %d").replace(" 0", " ")
    return [
        title,
        f"Average: {average} jobs",
        f"Busiest: {busiest['total']} jobs — {busiest_date}",
        f"Busy days: {busy_count} of {len(rows)}",
    ]
def weekly_pin_lines(history, local_now):
    monday = local_now.date().fromordinal(
        local_now.date().toordinal() - local_now.weekday()
    )
    week_start = datetime(
        monday.year,
        monday.month,
        monday.day,
        tzinfo=ZoneInfo("America/Vancouver"),
    )
    cutoff_timestamp = week_start.timestamp()
    moves = []
    for event in history.get("pin_moves", []):
        detected = parse_saved_time(event.get("detected_at"))
        if detected is not None and detected.timestamp() >= cutoff_timestamp:
            moves.append(event)
    lines = ["📌 WORK PIN MOVEMENT"]
    for board_letter in ["H", "T"]:
        board_moves = [
            event for event in moves if event.get("board") == board_letter
        ]
        numeric_moves = [
            event.get("movement")
            for event in board_moves
            if isinstance(event.get("movement"), (int, float))
        ]
        net = sum(numeric_moves)
        net_text = f"{net:+g}" if numeric_moves else "unknown"
        lines.append(
            f"{board_letter}: {len(board_moves)} movements — net {net_text}"
        )
        if numeric_moves:
            average_size = sum(abs(value) for value in numeric_moves) / len(numeric_moves)
            lines.append(f"   Average size: {average_size:.1f} positions")
    shift_counts = {
        "8 AM": 0,
        "4:30": 0,
        "Graveyard": 0,
    }
    confident = 0
    uncertain = 0
    for event in moves:
        shift = event.get("attributed_shift")
        if shift in shift_counts:
            shift_counts[shift] += 1
        if event.get("crossed_shift_boundary") is False:
            confident += 1
        else:
            uncertain += 1
    lines.extend([
        "Attributed shifts: "
        f"8 AM {shift_counts['8 AM']} · "
        f"4:30 {shift_counts['4:30']} · "
        f"Graveyard {shift_counts['Graveyard']}",
        f"Confidence: {confident} higher · {uncertain} uncertain",
    ])
    # H and T can move during the same check. Count the attached shift
    # breakdown only once when calculating associated-volume averages.
    unique_breakdowns = {}
    for event in moves:
        breakdown = event.get("shift_breakdown")
        if not isinstance(breakdown, dict):
            continue
        key = (event.get("detected_at"), event.get("attributed_shift"))
        unique_breakdowns[key] = breakdown
    breakdowns = list(unique_breakdowns.values())
    if breakdowns:
        average_total = round(
            sum(row.get("total", 0) for row in breakdowns) / len(breakdowns)
        )
        average_auto = round(
            sum(row.get("auto_dr", 0) for row in breakdowns) / len(breakdowns)
        )
        average_containers = round(
            sum(row.get("containers", 0) for row in breakdowns) / len(breakdowns)
        )
        average_rated = round(
            sum(row.get("rated", 0) for row in breakdowns) / len(breakdowns)
        )
        lines.extend([
            f"Average associated volume: {average_total} jobs",
            (
                f"Associated mix: 🚗 {average_auto} auto · "
                f"📦 {average_containers} containers · "
                f"🎟 {average_rated} rated"
            ),
        ])
    return lines
def weekly_category_high_lines(all_rows):
    categories = [
        ("Auto drivers", "auto_dr"),
        ("Containers", "containers"),
        ("HT", "container_ht"),
        ("Lashers", "container_lashers"),
        ("Rated jobs", "rated"),
        ("FSD", "fsd"),
        ("Deltaport", "deltaport"),
    ]
    lines = ["🏆 CATEGORY HIGHS"]
    for label, key in categories:
        values = [
            row.get(key)
            for row in all_rows
            if isinstance(row.get(key), (int, float))
        ]
        if values:
            lines.append(f"{label}: {max(values)}")
    return lines
def board_history_date(row):
    board_time = str(row.get("board_time", "")).strip()
    try:
        return datetime.strptime(
            board_time,
            "%B %d, %Y @ %I:%M %p",
        ).date()
    except ValueError:
        captured = parse_saved_time(row.get("captured_at"))
        return captured.date() if captured is not None else None
def forecast_row_date(date_text, snapshot_time):
    text = str(date_text or "").strip()
    candidates = []
    for year in [snapshot_time.year - 1, snapshot_time.year, snapshot_time.year + 1]:
        try:
            candidate = datetime.strptime(
                f"{text} {year}",
                "%a %b %d %Y",
            ).date()
            candidates.append(candidate)
        except ValueError:
            continue
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda candidate: abs((candidate - snapshot_time.date()).days),
    )
def forecast_known_before_430(history, target_date):
    cutoff = datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        15,
        15,
        tzinfo=ZoneInfo("America/Vancouver"),
    )
    best = None
    for snapshot in history.get("bcmea_forecasts", []):
        captured = parse_saved_time(snapshot.get("captured_at"))
        if captured is None or captured > cutoff:
            continue
        for row in snapshot.get("forecast", []):
            row_date = forecast_row_date(row.get("date"), captured)
            if row_date != target_date:
                continue
            if best is None or captured > best[0]:
                best = (captured, row)
    return best[1] if best is not None else None
def weekly_forecast_actual_lines(history, rows_430):
    comparisons = []
    for row in rows_430:
        actual_date = board_history_date(row)
        if actual_date is None:
            continue
        forecast = forecast_known_before_430(history, actual_date)
        if forecast is None:
            continue
        comparisons.append({
            "date": actual_date,
            "forecast_gangs": int(forecast.get("quantity", 0) or 0),
            "actual_jobs": row["total"],
        })
    lines = ["📈 BCMEA VS FINAL 4:30"]
    if not comparisons:
        lines.append("No comparable days recorded yet.")
        return lines
    for comparison in comparisons:
        date_label = comparison["date"].strftime("%a %b %d").replace(" 0", " ")
        lines.append(
            f"{date_label}: {comparison['forecast_gangs']} gangs "
            f"→ {comparison['actual_jobs']} jobs"
        )
    busy_forecasts = [
        comparison
        for comparison in comparisons
        if comparison["forecast_gangs"] >= 25
    ]
    if busy_forecasts:
        busy_results = sum(
            comparison["actual_jobs"] >= 200
            for comparison in busy_forecasts
        )
        lines.append(
            f"25+ gang forecasts reached 200+ jobs: "
            f"{busy_results} of {len(busy_forecasts)}"
        )
    return lines
def weekly_report_text(history, local_now):
    rows_430 = weekly_daily_rows(history, "board_430", local_now)
    rows_8am = weekly_daily_rows(history, "board_8am", local_now)
    rows_1am = weekly_daily_rows(history, "board_1am", local_now)
    # Wait until each shift has at least three different recorded days.
    if min(len(rows_430), len(rows_8am), len(rows_1am)) < 3:
        return None
    monday = local_now.date().fromordinal(
        local_now.date().toordinal() - local_now.weekday()
    )
    date_range = (
        f"{monday.strftime('%b %d')}–{local_now.strftime('%b %d, %Y')}"
    )
    lines = ["📊 WEEKLY ILWU REPORT", date_range, ""]
    lines.extend(weekly_shift_lines("📋 4:30", rows_430, 200))
    lines.append("")
    lines.extend(weekly_shift_lines("😴 8 AM", rows_8am, 200))
    lines.append("")
    lines.extend(weekly_shift_lines("⚰️ GRAVEYARD", rows_1am, 150))
    lines.append("")
    lines.extend(weekly_pin_lines(history, local_now))
    lines.append("")
    lines.extend(weekly_category_high_lines(rows_430 + rows_8am + rows_1am))
    lines.append("")
    lines.extend(weekly_forecast_actual_lines(history, rows_430))
    return "\n".join(lines)



def numeric_sum(text):
    text = str(text or "")

    # Count only one-to-three-digit quantities followed by a job label.
    # This counts "55 DRIVERS", "1HT" and "14 LASHERS",
    # but ignores phone numbers and four-digit dispatch times.
    quantities = re.findall(
        r"(?<![\d-])(\d{1,3})(?!\d)\s*(?=[A-Za-z])",
        text,
    )
    return sum(int(quantity) for quantity in quantities)

def qty_sum(rows):
    total = 0
    for row in rows or []:
        raw = str(row.get("qty", "")).strip()
        if re.fullmatch(r"\d+", raw):
            total += int(raw)
    return total

def gang_job_sum(text):
    text = str(text or "").strip()

    # A plain gang count describes crews, not additional jobs.
    # Ignore trailing descriptions such as "4 GANGS (3STEP)" too.
    if re.match(
        r"^\s*#?\s*\d+\s+GANGS?\b",
        text,
        flags=re.I,
    ):
        return 0

    # Count quantities attached to actual job labels.
    # Example: "1HT 1WD 55 DRIVERS 2 MECH".
    return numeric_sum(text)

def classify_ship_type(commodities):
    text = re.sub(
        r"\s+",
        " ",
        str(commodities or "").strip().upper(),
    )

    if "CONTAINER" in text:
        return "CONTAINER"

    if "AUTO" in text:
        return "AUTO"

    if "GRAIN" in text:
        return "GRAIN"

    if "COAL" in text:
        return "COAL"

    if "BULK" in text:
        return "BULK"

    if "LUMBER" in text or "FOREST" in text:
        return "FOREST PRODUCTS"

    if not text:
        return "OTHER / UNSPECIFIED"

    return text

def calculate_board(gb, board_key):
    board = gb.get(board_key, {})
    ships = board.get("ships_in_port", []) or []

    ship_types = {}

    for ship in ships:
        ship_type = classify_ship_type(
            ship.get("commodities", "")
        )
        ship_types[ship_type] = ship_types.get(ship_type, 0) + 1

    gang_jobs_total = sum(
        gang_job_sum(ship.get("gangs", ""))
        for ship in ships
    )

    ship_jobs_total = sum(
        numeric_sum(ship.get("jobs", ""))
        for ship in ships
    )

    auto_dr_total = 0
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
    fsd_total = qty_sum(board.get("fsd_jobs", []))
    dp_total = qty_sum(board.get("dp_jobs", []))
    rated_total = fsd_total + dp_total

    total = gang_jobs_total + ship_jobs_total + rated_total

    return {
        "total": total,
        "gang_total": gang_jobs_total,
        "ship_jobs_total": ship_jobs_total,
        "ship_count": len(ships),
        "ship_types": ship_types,
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

def send_fresh_update(h, t, b8, b, b_1, nw, local_now):
    checked_time = local_now.strftime("%b %-d at %-I:%M %p")
    lines = [
        "🔄 FRESH ILWU UPDATE",
        f"Checked {checked_time}",
        "",
        "📌 WORK PINS",
    ]

    if h is not None and t is not None:
        lines.extend([
            f"H Board: {h['value']}",
            f"T Board: {t['value']}",
        ])
    else:
        lines.append("⚠️ Work Pins unavailable")

    boards = [
        ("😴 8 AM", b8),
        ("📋 4:30", b),
        ("⚰️ Graveyard", b_1),
    ]

    for label, board in boards:
        lines.extend(["", label])

        if board is None:
            lines.append("⚠️ Source unavailable")
            continue

        lines.extend([
            f"Total: {board['total']} jobs",
            (
                f"🚗 Drivers: {board['auto_dr_total']} · "
                f"📦 Containers: {board['container_total']} · "
                f"🎟 Rated: {board['rated_total']}"
            ),
            f"Board time: {board['modified'] or 'unknown'}",
        ])

        lines.extend(ship_summary_lines(board))

    lines.extend(["", "📈 BCMEA NW FORECAST"])

    if nw is None:
        lines.append("⚠️ Source unavailable")
    elif not nw:
        lines.append("No forecast rows returned")
    else:
        for row in nw:
            quantity = row["quantity"]

            if quantity >= 30:
                marker = "🚨"
            elif quantity >= 25:
                marker = "🔥"
            else:
                marker = "•"

            lines.append(
                f"{marker} {row['date']}: {quantity} gangs"
            )

    lines.extend([
        "",
        "This update used only responses fetched during this run.",
    ])

    telegram("\n".join(lines))

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
        history_changed = update_shift_outcomes(
            state=state,
            history=history,
            observed_at=pins_detected_local,
            h_value=h["value"],
            t_value=t["value"],
            b=b,
            b8=b8,
            b_1=b_1,
        ) or history_changed
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
            notable_lines = notable_category_lines(
                history,
                "board_430",
                b,
                datetime.now(timezone.utc),
            )
            notable_text = ""
            if notable_lines:
                notable_text = (
                    "\nNOTABLE VOLUME\n"
                    + "\n".join(notable_lines)
                    + "\n"
                )
            unusual_lines = anomaly_lines(
                history,
                "board_430",
                b,
                local_now,
            )
            unusual_text = ""
            if unusual_lines:
                unusual_text = (
                    "\n⚠️ UNUSUAL VS 30-DAY NORMAL\n"
                    + "\n".join(unusual_lines)
                    + "\n"
                )
            telegram(
                f"{headline}\n"
                f"Total: {b['total']} jobs"
                f"{format_delta(b['total'], old_430_total)}\n\n"
                "CHANGES\n"
                f"{change_text}\n\n"
                f"{notable_text}"
                f"{unusual_text}"
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
            "board_430_ship_count": b["ship_count"],
            "board_430_ship_types": b["ship_types"],
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
        unusual_lines_8am = anomaly_lines(
            history,
            "board_8am",
            b8,
            local_now,
        )
        if (
            old_8am_modified is not None
            and changed_8am
            and b8["total"] >= 200
            and (b8["total"] >= 200 or unusual_lines_8am)
        ):
            if b8["total"] >= 300:
                level = "🚨 HUGE"
            elif b8["total"] >= 250:
                level = "🔥🔥 VERY BUSY"
            elif b8["total"] >= 200:
                level = "🔥 BUSY"
            else:
                level = "⚠️ UNUSUAL"
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
            notable_lines = notable_category_lines(
                history,
                "board_8am",
                b8,
                datetime.now(timezone.utc),
            )
            notable_text = ""
            if notable_lines:
                notable_text = (
                    "\nNOTABLE VOLUME\n"
                    + "\n".join(notable_lines)
                    + "\n"
                )
            unusual_text = ""
            if unusual_lines_8am:
                unusual_text = (
                    "\n⚠️ UNUSUAL VS 30-DAY NORMAL\n"
                    + "\n".join(unusual_lines_8am)
                    + "\n"
                )
            telegram(
                f"😴 8 AM BOARD — {level}\n"
                f"Total: {b8['total']} jobs"
                f"{format_delta(b8['total'], old_8am_total)}\n\n"
                "CHANGES\n"
                f"{change_text}\n\n"
                f"{notable_text}"
                f"{unusual_text}"
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
            "board_8am_ship_count": b8["ship_count"],
            "board_8am_ship_types": b8["ship_types"],
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
        unusual_lines_1am = anomaly_lines(
            history,
            "board_1am",
            b_1,
            local_now,
        )
        if (
            old_1am_modified is not None
            and changed_1am
            and b_1["total"] >= 150
            and (b_1["total"] >= 150 or unusual_lines_1am)
        ):
            if b_1["total"] >= 250:
                level = "🚨 HUGE"
            elif b_1["total"] >= 200:
                level = "🔥🔥 VERY BUSY"
            elif b_1["total"] >= 150:
                level = "🔥 BUSY"
            else:
                level = "⚠️ UNUSUAL"
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
            notable_lines = notable_category_lines(
                history,
                "board_1am",
                b_1,
                datetime.now(timezone.utc),
            )
            notable_text = ""
            if notable_lines:
                notable_text = (
                    "\nNOTABLE VOLUME\n"
                    + "\n".join(notable_lines)
                    + "\n"
                )
            unusual_text = ""
            if unusual_lines_1am:
                unusual_text = (
                    "\n⚠️ UNUSUAL VS 30-DAY NORMAL\n"
                    + "\n".join(unusual_lines_1am)
                    + "\n"
                )
            telegram(
                f"⚰️ 1 AM BOARD — {level}\n"
                f"Total: {b_1['total']} jobs"
                f"{format_delta(b_1['total'], old_1am_total)}\n\n"
                "CHANGES\n"
                f"{change_text}\n\n"
                f"{notable_text}"
                f"{unusual_text}"
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
            "board_1am_ship_count": b_1["ship_count"],
            "board_1am_ship_types": b_1["ship_types"],
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
            lines = ["📈 BCMEA NW FORECAST UPDATED", "", "CHANGES"]
            old_by_date = {row["date"]: row for row in old_nw}
            new_by_date = {row["date"]: row for row in nw}

            for row in nw:
                date = row["date"]
                old_row = old_by_date.get(date)

                if old_row is None:
                    lines.append(
                        f"🆕 {date}: added at {row['quantity']} gangs"
                    )
                    continue

                old_qty = old_row["quantity"]
                new_qty = row["quantity"]

                if new_qty > old_qty:
                    lines.append(
                        f"⬆️ {date}: {old_qty} → {new_qty} gangs "
                        f"(+{new_qty - old_qty})"
                    )
                elif new_qty < old_qty:
                    lines.append(
                        f"⬇️ {date}: {old_qty} → {new_qty} gangs "
                        f"({new_qty - old_qty})"
                    )
                elif row.get("status") != old_row.get("status"):
                    lines.append(
                        f"🔄 {date}: status "
                        f"{old_row.get('status')} → {row.get('status')}"
                    )

            for row in old_nw:
                if row["date"] not in new_by_date:
                    lines.append(
                        f"➖ {row['date']}: removed "
                        f"(was {row['quantity']} gangs)"
                    )

            lines.extend(["", "CURRENT FORECAST"])
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
                lines.extend(["", "Busy forecast:"])

                for row in busy_days:
                    label = (
                        "VERY BUSY"
                        if row["quantity"] >= 30
                        else "BUSY"
                    )
                    lines.append(
                        f"{row['date']}: {row['quantity']} "
                        f"gangs — {label}"
                    )

            lines.extend([
                "",
                f'<a href="{BCMEA_FORECAST_PAGE}">'
                "View BCMEA forecast</a>",
            ])

            telegram("\n".join(lines))
        state["bcmea_nw_forecast"] = nw
        state["bcmea_last_success"] = bcmea_success_at
        history_changed = record_bcmea_forecast_history(
            history,
            nw,
            bcmea_success_at,
        ) or history_changed

    if os.getenv("FORCE_UPDATE", "").lower() == "true":
        send_fresh_update(
            h=h,
            t=t,
            b8=b8,
            b=b,
            b_1=b_1,
            nw=nw,
            local_now=local_now,
    )
        
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
    # Send one weekly report on the first run from 7:00 through 7:59 PM
    # Vancouver time on Sunday.
    weekly_key = local_now.strftime("%G-W%V")
    last_weekly_report = state.get("last_weekly_report")
    if (
        local_now.weekday() == 6
        and 19 * 60 <= minutes_now < 20 * 60
        and last_weekly_report != weekly_key
    ):
        report_text = weekly_report_text(history, local_now)
        if report_text is not None:
            telegram(report_text)
            state["last_weekly_report"] = weekly_key
        else:
            print(
                "Weekly report deferred because fewer than three recorded "
                "days are available for one or more shifts"
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
