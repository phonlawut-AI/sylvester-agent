"""
Recurring maintenance task scheduler.
Fetches tasks due today for the machines in a given plan.

Currently returns mock data.
Replace the body of get_due_tasks() with a real Google Sheets fetch
when the maintenance schedule sheet is connected (GOOGLE_SHEET_ID in .env).
"""

from parsers.plan_pdf import Plan
from agents.brief_generator import MaintenanceTask, mock_due_tasks


def get_due_tasks(plan: Plan) -> dict[str, list[MaintenanceTask]]:
    """Return recurring maintenance tasks due today for machines in this plan.

    TODO: replace with Google Sheets fetch using config.GOOGLE_SHEET_ID.
    """
    return mock_due_tasks(plan)
