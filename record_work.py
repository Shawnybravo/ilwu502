import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


HISTORY_FILE = Path("history.json")
STATE_FILE = Path("state.json")
VANCOUVER = ZoneInfo("America/Vancouver")


def read_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_atomic(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def normalized_shift(value):
    choices = {
        "430": "4:30",
        "4:30": "4:30",
        "8": "8 AM",
        "8am": "8 AM",
        "8 AM": "8 AM",
        "g": "Graveyard",
        "graveyard": "Graveyard",
        "Graveyard": "Graveyard",
    }
    received = str(value).strip()
    shift = choices.get(received)
    if shift is None:
        raise ValueError(
            f"Shift must be 430, 8, or g; received {received!r}."
        )
    return shift


def report_date(shift, reported_local):
    if shift == "Graveyard" and (
        reported_local.hour * 60 + reported_local.minute >= 16 * 60 + 15
    ):
        next_day = reported_local.date().fromordinal(
            reported_local.date().toordinal() + 1
        )
        return next_day.isoformat()
    return reported_local.date().isoformat()


def state_breakdown(state, shift):
    prefixes = {
        "4:30": "board_430",
        "8 AM": "board_8am",
        "Graveyard": "board_1am",
    }
    prefix = prefixes[shift]
    return {
        "total": state.get(f"{prefix}_total"),
        "auto_dr": state.get(f"{prefix}_auto_dr_total"),
        "containers": state.get(f"{prefix}_container_total"),
        "container_ht": state.get(f"{prefix}_container_ht_total"),
        "container_lashers": state.get(
            f"{prefix}_container_lashers_total"
        ),
        "rated": state.get(f"{prefix}_rated_total"),
        "fsd": state.get(f"{prefix}_fsd_total"),
        "deltaport": state.get(f"{prefix}_dp_total"),
        "board_time": state.get(f"{prefix}_modified"),
    }


def matching_outcome(history, shift, date_value):
    for outcome in reversed(history.get("shift_outcomes", [])):
        if outcome.get("shift") == shift and outcome.get("date") == date_value:
            return outcome
    return None


def undo_latest(history):
    reports = history.setdefault("worked_reports", [])
    for index in range(len(reports) - 1, -1, -1):
        if reports[index].get("person") == "me":
            removed = reports.pop(index)
            return removed
    return None


def main():
    action = os.environ.get("REPORT_ACTION", "add").strip().lower()
    history = read_json(HISTORY_FILE, {})
    history.setdefault("worked_reports", [])

    if action == "undo":
        removed = undo_latest(history)
        if removed is None:
            print("No self-reported work entry exists to undo.")
            return
        write_json_atomic(HISTORY_FILE, history)
        print(
            f"Removed worked report: {removed.get('date')} "
            f"{removed.get('shift')}"
        )
        return

    shift = normalized_shift(os.environ["REPORTED_SHIFT"])
    reported_at = datetime.fromisoformat(os.environ["REPORTED_AT"])
    if reported_at.tzinfo is None:
        raise ValueError("REPORTED_AT must include a timezone.")
    reported_local = reported_at.astimezone(VANCOUVER)
    date_value = report_date(shift, reported_local)
    report_key = f"me|{date_value}|{shift}"

    if any(
        report.get("key") == report_key
        for report in history["worked_reports"]
    ):
        print("This worked shift has already been reported.")
        return

    state = read_json(STATE_FILE, {})
    outcome = matching_outcome(history, shift, date_value)
    breakdown = (
        outcome.get("shift_breakdown")
        if outcome and outcome.get("shift_breakdown")
        else state_breakdown(state, shift)
    )

    history["worked_reports"].append({
        "key": report_key,
        "person": "me",
        "position": "H-16",
        "worked": True,
        "source": "self_report",
        "reported_at": reported_at.isoformat(),
        "reported_at_vancouver": reported_local.isoformat(),
        "date": date_value,
        "shift": shift,
        "h_board": state.get("h_board"),
        "t_board": state.get("t_board"),
        "matched_shift_outcome_key": outcome.get("key") if outcome else None,
        "h_moved": outcome.get("h_moved") if outcome else None,
        "t_moved": outcome.get("t_moved") if outcome else None,
        "outcome_coverage_confident": (
            outcome.get("coverage_confident") if outcome else None
        ),
        "shift_breakdown": breakdown,
    })

    write_json_atomic(HISTORY_FILE, history)
    print(f"Recorded worked shift: {date_value} {shift}")


if __name__ == "__main__":
    main()
