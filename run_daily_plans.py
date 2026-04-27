"""
Batch Morning Brief runner.

Drop any number of Vendii refill plan PDFs into a folder, then run:

    python run_daily_plans.py [folder_path]

If no folder is given, defaults to:  data/inbox/refill_plans/

Each PDF is processed independently:
  - Plan is parsed to identify the staff member and plan date
  - staff_name from the PDF is looked up in config/staff_registry.json
    (only approved refillers receive a Morning Brief)
  - Morning Brief is generated and sent via LINE
  - JSON log is saved in data/logs/daily_plans/{date}.json
  - File is moved to data/processed/refill_plans/ on success
  - File is moved to data/failed/refill_plans/ on any error

Safety rules:
  - A 0.3–0.5 s delay is inserted between sends (LINE rate-limiting)
  - Plans already in the log (same plan_id + date, status=sent) are skipped
  - An error in one file never stops the remaining files from being processed
"""

import sys
import random
import shutil
import time
from pathlib import Path
from datetime import datetime

# Force UTF-8 on Windows
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from parsers.plan_pdf import parse_plan_pdf
from orchestrator_agent.morning_brief import run_morning_brief
from agents.registration.staff_registry import lookup_refiller
from storage import log_store

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT          = Path(__file__).parent
_DEFAULT_INBOX = _ROOT / "data" / "inbox"    / "refill_plans"
_PROCESSED_DIR = _ROOT / "data" / "processed" / "refill_plans"
_FAILED_DIR    = _ROOT / "data" / "failed"   / "refill_plans"


# ── File helpers ──────────────────────────────────────────────────────────────

def _move(src: Path, dest_dir: Path) -> Path:
    """Move src to dest_dir, appending a timestamp to the filename on collision."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = dest_dir / f"{src.stem}_{ts}{src.suffix}"
    shutil.move(str(src), str(dest))
    return dest


# ── Per-file processor ────────────────────────────────────────────────────────

def _process_one(pdf_path: Path) -> str:
    """
    Process a single PDF.  Returns a short status string.
    Raises on any unrecoverable error so the caller can move the file to failed/.
    """
    plan = parse_plan_pdf(str(pdf_path))

    target_id = lookup_refiller(plan.staff_name)
    if not target_id:
        raise ValueError(
            f"No approved refiller found for staff_name '{plan.staff_name}'. "
            "Staff must register and be approved before receiving a Morning Brief."
        )

    if log_store.is_already_logged(plan.date, plan.plan_id):
        return "skipped:duplicate"

    run_morning_brief(str(pdf_path), target_id)
    return f"sent:{plan.staff_name}:{target_id[:12]}"


# ── Batch runner ──────────────────────────────────────────────────────────────

def process_folder(folder: Path) -> None:
    pdfs = sorted(folder.glob("*.pdf"))

    if not pdfs:
        print(f"No PDF files found in {folder}")
        return

    total   = len(pdfs)
    sent    = 0
    skipped = 0
    failed  = 0
    failures: list[tuple[str, str]] = []

    print(f"\n📂  {total} PDF{'s' if total != 1 else ''} found in {folder}")
    print(f"{'─' * 52}")

    for idx, pdf_path in enumerate(pdfs, start=1):
        label = pdf_path.name
        print(f"[{idx}/{total}]  {label}")

        try:
            status = _process_one(pdf_path)

            if status.startswith("skipped"):
                skipped += 1
                print(f"        ⏭   Already sent — skipping")
                _move(pdf_path, _PROCESSED_DIR)
            else:
                _, staff, line_id_prefix = status.split(":", 2)
                sent += 1
                print(f"        ✓   Sent  →  {staff}  ({line_id_prefix}…)")
                _move(pdf_path, _PROCESSED_DIR)

        except Exception as exc:
            failed += 1
            reason = str(exc)
            failures.append((label, reason))
            print(f"        ✗   FAILED: {reason}")
            try:
                _move(pdf_path, _FAILED_DIR)
            except Exception as move_err:
                print(f"             (could not move to failed/: {move_err})")

        finally:
            if idx < total:
                time.sleep(random.uniform(0.3, 0.5))

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"{'─' * 52}")
    print(f"Total   : {total}")
    print(f"Sent    : {sent}")
    if skipped:
        print(f"Skipped : {skipped}  (already processed)")
    print(f"Failed  : {failed}")

    if failures:
        print("\nFailed files:")
        for name, reason in failures:
            print(f"  •  {name}")
            print(f"     {reason}")

    print(f"{'─' * 52}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_INBOX

    if not folder.exists():
        if folder == _DEFAULT_INBOX:
            folder.mkdir(parents=True, exist_ok=True)
            print(f"Created inbox: {folder}")
            print("Drop PDF files there and re-run.")
            return
        print(f"Error: folder not found: {folder}")
        sys.exit(1)

    if not folder.is_dir():
        print(f"Error: not a directory: {folder}")
        sys.exit(1)

    process_folder(folder)


if __name__ == "__main__":
    main()
