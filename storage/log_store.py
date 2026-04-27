"""
JSON-based run log for the morning brief orchestrator.

One file per plan date: data/logs/daily_plans/{YYYY-MM-DD}.json
Each file is a JSON array so multiple runs on the same date are appended.

Machine status fields
---------------------
visit_status  : "pending" | "done" | "not_done"
issue_status  : "none" | "pending" | "reported"

A machine can be visit_status="done" AND issue_status="reported" simultaneously.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from parsers.plan_pdf import Plan
from agents.brief_generator import MaintenanceTask

_LOG_DIR = Path(__file__).parent.parent / "data" / "logs" / "daily_plans"


def save(
    plan: Plan,
    due_tasks: dict[str, list[MaintenanceTask]],
    target_line_id: str,
    status: str = "sent",
) -> dict:
    """Append one run entry to data/logs/daily_plans/{plan.date}.json.

    Returns the saved entry dict, which includes a 'log_path' key.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _LOG_DIR / f"{plan.date}.json"

    entries: list[dict] = []
    if log_path.exists():
        with log_path.open(encoding="utf-8") as f:
            entries = json.load(f)

    entry: dict = {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "plan_id": plan.plan_id,
        "plan_name": plan.plan_name,
        "staff_name": plan.staff_name,
        "date": plan.date,
        "target_line_id": target_line_id,
        "status": status,
        "machine_count": len(plan.machines),
        "task_count": sum(len(v) for v in due_tasks.values()),
        "machines": [
            {
                "machine_id": m.machine_id,
                "location_name": m.location_name,
                "arrival_time": m.arrival_time,
                "route": m.route,
                "notes": m.notes,
                # visit fields
                "visit_status": "pending",
                "done_at": None,
                "done_by": None,
                # issue fields
                "issue_status": "none",
                "issue_note": "",
                "issue_images": [],
                "issue_at": None,
                "issue_by": None,
                # not-done fields
                "not_done_reason": "",
                "not_done_images": [],
                "not_done_at": None,
                "not_done_by": None,
                # recurring tasks
                "recurring_tasks": [
                    {
                        "task_name": t.task_name,
                        "frequency": t.frequency,
                        "last_done_date": t.last_done_date,
                    }
                    for t in due_tasks.get(m.machine_id, [])
                ],
            }
            for m in plan.machines
        ],
        "log_path": str(log_path),
    }

    entries.append(entry)

    with log_path.open("w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

    return entry


def update_machine_status(date: str, machine_id: str, update: dict) -> bool:
    """Merge update dict into the matching machine entry for the given date.

    Searches from most recent log entry backwards so reruns don't shadow
    the latest send. Returns True if the machine was found and updated.
    """
    log_path = _LOG_DIR / f"{date}.json"
    if not log_path.exists():
        return False

    with log_path.open(encoding="utf-8") as f:
        entries = json.load(f)

    for entry in reversed(entries):
        for machine in entry.get("machines", []):
            if machine["machine_id"] == machine_id:
                machine.update(update)
                with log_path.open("w", encoding="utf-8") as f:
                    json.dump(entries, f, indent=2, ensure_ascii=False)
                return True

    return False


def append_to_machine_field(date: str, machine_id: str, field: str, value: object) -> bool:
    """Append value to a list field on the matching machine entry.

    Creates the list if the field is missing or not a list.
    Returns True if the machine was found and updated.
    """
    log_path = _LOG_DIR / f"{date}.json"
    if not log_path.exists():
        return False

    with log_path.open(encoding="utf-8") as f:
        entries = json.load(f)

    for entry in reversed(entries):
        for machine in entry.get("machines", []):
            if machine["machine_id"] == machine_id:
                if not isinstance(machine.get(field), list):
                    machine[field] = []
                machine[field].append(value)
                with log_path.open("w", encoding="utf-8") as f:
                    json.dump(entries, f, indent=2, ensure_ascii=False)
                return True

    return False


def is_already_logged(date: str, plan_id: str) -> bool:
    """Return True if plan_id was already successfully sent on the given date.

    Checks every entry in the file so reruns of the batch script are safe.
    """
    log_path = _LOG_DIR / f"{date}.json"
    if not log_path.exists():
        return False
    with log_path.open(encoding="utf-8") as f:
        entries = json.load(f)
    return any(
        e.get("plan_id") == plan_id and e.get("status") == "sent"
        for e in entries
    )


def get_all_entries(date: str) -> list[dict]:
    """Return all log entries for the given date (empty list if file not found)."""
    log_path = _LOG_DIR / f"{date}.json"
    if not log_path.exists():
        return []
    with log_path.open(encoding="utf-8") as f:
        return json.load(f)


def get_latest_entry(date: str) -> dict | None:
    """Return the most recently saved log entry for the given date, or None."""
    log_path = _LOG_DIR / f"{date}.json"
    if not log_path.exists():
        return None
    with log_path.open(encoding="utf-8") as f:
        entries = json.load(f)
    return entries[-1] if entries else None
