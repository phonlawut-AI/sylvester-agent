"""
Morning brief orchestrator.
Coordinates the full workflow for one staff member's daily refill brief.

  PDF parse  →  task fetch  →  LINE send  →  JSON log

Entry point: run_morning_brief(plan_pdf_path, target_line_id)
"""

from parsers.plan_pdf import parse_plan_pdf
from agents.brief_generator import generate_brief
from sheets.task_schedule import get_due_tasks
from agents.line_communicator.sender import send_brief_with_done_buttons
from storage import log_store


def run_morning_brief(
    plan_pdf_path: str,
    target_line_id: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Run the full morning brief workflow for one staff member.

    Steps:
      1. Parse the Vendii refill plan PDF
      2. Fetch recurring maintenance tasks due today (sheets or mock)
      3. Generate plain-text brief for terminal preview and LINE altText
      4. Send LINE Flex message with per-machine Mark Done + Issue buttons
      5. Append a run entry to data/logs/daily_plans/{date}.json

    Args:
        plan_pdf_path:  path to the Vendii refill plan PDF
        target_line_id: LINE user or group ID to send the brief to
        dry_run:        when True, skip steps 4-5 (preview only, nothing sent)

    Returns a dict with keys:
        plan, due_tasks, brief, status, log (None when dry_run=True)
    """
    # 1. Parse
    plan = parse_plan_pdf(plan_pdf_path)

    # 2. Recurring tasks
    due_tasks = get_due_tasks(plan)

    # 3. Plain-text brief (terminal preview + Flex altText)
    brief = generate_brief(plan, due_tasks)

    if dry_run:
        return {
            "plan": plan,
            "due_tasks": due_tasks,
            "brief": brief,
            "status": "dry_run",
            "log": None,
        }

    # 4. Send — log failure before re-raising so the error is always recorded
    try:
        send_brief_with_done_buttons(target_line_id, brief, plan, due_tasks)
        status = "sent"
    except Exception as exc:
        log_store.save(plan, due_tasks, target_line_id, status=f"error: {exc}")
        raise

    # 5. Log success
    entry = log_store.save(plan, due_tasks, target_line_id, status=status)

    return {
        "plan": plan,
        "due_tasks": due_tasks,
        "brief": brief,
        "status": status,
        "log": entry,
    }
