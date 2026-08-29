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
        return {"board_430": [], "board_8am": [], "board_1am": []}

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    for key in ["board_430", "board_8am", "board_1am"]:
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


def main():
    state = load_state()
    history = load_history()
    history_changed = False

    # Each source starts unavailable. A source is assigned a value only after
    # its fetch AND parsing/calculation both succeed.
    h = None
    b = None
    b8 = None
    b_1 = None
    nw = None

    print("Fetching H board...")
    try:
        pins_gb = extract_gbdata(fetch(PINS_URL))
        h = find_h_board(pins_gb)
    except Exception as e:
        print(
            f"WARNING: H board failed after retries: {e} "
            "-- keeping previous H board state"
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

    # H BOARD: compare, alert, and update state only when fresh data succeeded.
    if h is not None:
        old_h = state.get("h_board")
        if old_h is not None and h["value"] != old_h:
            telegram(
                "🚢 H BOARD UPDATED\n"
                f"{old_h} → {h['value']}\n"
                f"Board time: {h['modified'] or 'unknown'}\n"
                f'<a href="{PINS_PAGE}">View work pins</a>'
            )

        state["h_board"] = h["value"]
        state["h_modified"] = h["modified"]
        state["h_last_success"] = datetime.now(timezone.utc).isoformat()

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
        state["bcmea_last_success"] = datetime.now(timezone.utc).isoformat()

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

    # Successful sources have already updated their own keys. Failed sources
    # never touched their keys, so their previous state remains unchanged.
    save_state(state)
    if history_changed or not HISTORY_FILE.exists():
        save_history(history)

    if h is not None:
        print(f"H BOARD: {h['value']} ({h['modified']})")
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
