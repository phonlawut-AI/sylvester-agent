"""
End-to-end test: preview the morning brief, then send via the Orchestrator.
Usage: python test_send_brief.py <path-to-plan.pdf> [LINE_USER_ID]

LINE_USER_ID defaults to TEST_LINE_ID in .env.
"""

import sys
import os

# Force UTF-8 output on Windows (avoids cp1252 encoding errors with Unicode)
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

load_dotenv()

from orchestrator_agent.morning_brief import run_morning_brief


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_send_brief.py <plan.pdf> [LINE_USER_ID]")
        sys.exit(1)

    pdf_path = sys.argv[1]
    target_id = sys.argv[2] if len(sys.argv) >= 3 else os.getenv("TEST_LINE_ID", "")

    if not target_id:
        print("Error: provide a LINE user ID as argument or set TEST_LINE_ID in .env")
        sys.exit(1)

    # Parse + generate brief without sending (dry run for preview)
    result = run_morning_brief(pdf_path, target_id, dry_run=True)
    plan = result["plan"]

    print(f"\nParsed:   {plan.plan_name} | Staff: {plan.staff_name} | Machines: {len(plan.machines)}")
    print(f"Tasks:    {result['due_tasks'] and sum(len(v) for v in result['due_tasks'].values())} recurring task(s) due")
    print("\n── Preview ─────────────────────────────────────────")
    print(result["brief"])
    print("────────────────────────────────────────────────────\n")

    confirm = input(f"Send to {target_id}? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)

    result = run_morning_brief(pdf_path, target_id)
    print(f"Sent!   Status : {result['status']}")
    print(f"        Log    : {result['log']['log_path']}")


if __name__ == "__main__":
    main()
