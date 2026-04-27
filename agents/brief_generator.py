"""
Builds the LINE morning brief message for a single staff member.
Input: Plan (from parsers/plan_pdf.py) + list of due maintenance tasks per machine.
Output: formatted string ready to send via LINE.
"""

from dataclasses import dataclass
from parsers.plan_pdf import Plan, Machine


@dataclass
class MaintenanceTask:
    machine_id: str
    task_name: str
    frequency: str          # 'fortnightly' or 'monthly'
    photo_instruction: str
    last_done_date: str | None = None   # English short date, e.g. '12 Apr'


FREQ_LABEL = {
    "fortnightly": "Fortnightly",
    "monthly":     "Monthly",
}

MONTHS = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _short_date(iso_date: str) -> str:
    """YYYY-MM-DD → '10 Feb'"""
    try:
        parts = iso_date.split("-")
        return f"{int(parts[2])} {MONTHS[int(parts[1])]}"
    except Exception:
        return iso_date


def _route_names(machines: list[Machine]) -> str:
    """Unique route names joined with ' · '"""
    seen: list[str] = []
    for m in machines:
        if m.route and m.route not in seen:
            seen.append(m.route)
    return " · ".join(seen) if seen else ""


def generate_brief(
    plan: Plan,
    due_tasks: dict[str, list[MaintenanceTask]],
) -> str:
    """
    Build the morning brief LINE message for one staff member.

    Args:
        plan: parsed Plan object for this staff member
        due_tasks: mapping of machine_id -> list of MaintenanceTask due today
    """
    machines = plan.machines
    total = len(machines)
    routes = _route_names(machines)
    date_str = _short_date(plan.date)
    task_count = sum(len(v) for v in due_tasks.values())

    lines: list[str] = [
        f"🌅 Good morning {plan.staff_name}!",
        f"{date_str} · {total} machines"
        + (f" · {routes}" if routes else "")
        + (f" · {task_count} recurring task(s) due" if task_count else ""),
        "",
    ]

    for m in machines:
        lines.append(f"── {m.order}/{total} · ⏰ {m.arrival_time} ──────────")
        lines.append(f"📍 {m.location_name}")
        id_line = f"#{m.machine_id}"
        if m.route:
            id_line += f" · {m.route}"
        lines.append(id_line)

        if m.notes:
            lines.append(f"⚠️  {m.notes}")

        for task in due_tasks.get(m.machine_id, []):
            freq = FREQ_LABEL.get(task.frequency, task.frequency)
            lines.append(f"🔁 {task.task_name} ({freq} · due)")

        lines.append("")

    lines.append("────────────────────────")
    lines.append("Tap Done after each machine 💪")

    return "\n".join(lines)


def mock_due_tasks(plan: Plan) -> dict[str, list[MaintenanceTask]]:
    """Return synthetic recurring tasks for testing before Google Sheets is connected."""
    _MOCK = [
        ("Clean milk frother",              "fortnightly", "12 Apr", "Photo before and after cleaning"),
        ("Check CO2 fitting + clean drip tray", "monthly", "26 Mar", "Photo of CO2 fitting and drip tray"),
    ]
    result: dict[str, list[MaintenanceTask]] = {}
    for i, m in enumerate(plan.machines):
        if i < len(_MOCK):
            name, freq, last, photo = _MOCK[i]
            result[m.machine_id] = [
                MaintenanceTask(
                    machine_id=m.machine_id,
                    task_name=name,
                    frequency=freq,
                    photo_instruction=photo,
                    last_done_date=last,
                )
            ]
    return result


# ── CLI test runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from parsers.plan_pdf import parse_plan_pdf

    if len(sys.argv) < 2:
        print("Usage: python -m agents.brief_generator <path-to-plan.pdf>")
        sys.exit(1)

    plan = parse_plan_pdf(sys.argv[1])
    brief = generate_brief(plan, due_tasks=mock_due_tasks(plan))
    print(brief)
