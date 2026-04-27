"""
Issue Aggregator Agent.
Reads daily plan logs and produces a ranked technician repair priority summary.

Entry point: generate_repair_priority_summary(days=30) -> str
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Three parents up: agents/issue_aggregator/ → agents/ → morning-brief-agent/
_LOG_DIR = Path(__file__).parent.parent.parent / "data" / "logs" / "daily_plans"

CRITICAL_KEYWORDS: list[tuple[str, int]] = [
    ("not dispensing", 3),
    ("no coffee",      3),
    ("leak",           3),
    ("payment",        3),
    ("broken",         2),
    ("blocked",        2),
    ("brew head",      2),
    ("air filter",     1),
    ("calibrate",      1),
    ("canister",       1),
]

_PRIORITY_HIGH   = "High"
_PRIORITY_MEDIUM = "Medium"
_PRIORITY_LOW    = "Low"
_PRIORITY_ORDER  = {_PRIORITY_HIGH: 0, _PRIORITY_MEDIUM: 1, _PRIORITY_LOW: 2}


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class IssueRecord:
    machine_id:    str
    location_name: str
    issue_note:    str
    issue_at:      str | None
    log_date:      str
    issue_images:  list[str] = field(default_factory=list)


@dataclass
class MachineSummary:
    machine_id:      str
    latest_location: str
    issue_count:     int
    unique_notes:    list[str] = field(default_factory=list)
    all_images:      list[str] = field(default_factory=list)
    latest_issue_at: str | None = None
    score:           int = 0
    priority:        str = _PRIORITY_LOW


# ── Collection ────────────────────────────────────────────────────────────────

def _log_files_since(cutoff: datetime) -> list[Path]:
    if not _LOG_DIR.exists():
        return []
    results: list[Path] = []
    for path in _LOG_DIR.glob("*.json"):
        try:
            file_date = datetime.fromisoformat(path.stem).replace(tzinfo=timezone.utc)
            if file_date >= cutoff:
                results.append(path)
        except ValueError:
            continue
    return sorted(results)


def _resolve_issue_status(m: dict) -> str:
    """Read issue_status with backward-compat fallback to old 'status' field."""
    if "issue_status" in m:
        return m["issue_status"]
    return "reported" if m.get("status") == "issue" else "none"


def _collect_issues(files: list[Path]) -> list[IssueRecord]:
    records: list[IssueRecord] = []
    for path in files:
        try:
            with path.open(encoding="utf-8") as f:
                entries = json.load(f)
        except Exception:
            continue

        log_date = path.stem
        for entry in entries:
            for m in entry.get("machines", []):
                issue_status = _resolve_issue_status(m)
                has_issue    = (
                    issue_status in ("pending", "reported")
                    or m.get("issue") is True
                    or bool(m.get("issue_note", "").strip())
                )
                if not has_issue:
                    continue

                records.append(IssueRecord(
                    machine_id=m.get("machine_id", ""),
                    location_name=m.get("location_name", ""),
                    issue_note=m.get("issue_note", "").strip(),
                    issue_at=m.get("issue_at"),
                    log_date=log_date,
                    issue_images=list(m.get("issue_images", [])),
                ))

    return records


# ── Scoring ───────────────────────────────────────────────────────────────────

def _keyword_score(note: str) -> int:
    note_lower = note.lower()
    return sum(w for kw, w in CRITICAL_KEYWORDS if kw in note_lower)


def _recency_bonus(issue_at: str | None, log_date: str, now: datetime) -> int:
    raw = issue_at or f"{log_date}T00:00:00+00:00"
    try:
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return 1 if (now - ts).days <= 7 else 0
    except Exception:
        return 0


def _compute_score(records: list[IssueRecord], now: datetime) -> int:
    score = len(records) * 2
    for rec in records:
        score += _keyword_score(rec.issue_note)
        score += _recency_bonus(rec.issue_at, rec.log_date, now)
    return score


def _priority_label(score: int) -> str:
    if score >= 5:
        return _PRIORITY_HIGH
    if score >= 3:
        return _PRIORITY_MEDIUM
    return _PRIORITY_LOW


# ── Aggregation ───────────────────────────────────────────────────────────────

def _build_summaries(records: list[IssueRecord]) -> list[MachineSummary]:
    groups: dict[str, list[IssueRecord]] = {}
    for rec in records:
        if rec.machine_id:
            groups.setdefault(rec.machine_id, []).append(rec)

    now = datetime.now(timezone.utc)
    summaries: list[MachineSummary] = []

    for machine_id, recs in groups.items():
        recs_sorted = sorted(
            recs,
            key=lambda r: r.issue_at or f"{r.log_date}T00:00:00",
            reverse=True,
        )
        latest = recs_sorted[0]

        # Deduplicated notes
        seen: set[str] = set()
        unique_notes: list[str] = []
        for r in recs_sorted:
            key = r.issue_note.lower()
            if r.issue_note and key not in seen:
                seen.add(key)
                unique_notes.append(r.issue_note)

        # Collect all image paths
        all_images: list[str] = []
        seen_imgs: set[str] = set()
        for r in recs_sorted:
            for img in r.issue_images:
                if img not in seen_imgs:
                    seen_imgs.add(img)
                    all_images.append(img)

        score    = _compute_score(recs, now)
        priority = _priority_label(score)

        summaries.append(MachineSummary(
            machine_id=machine_id,
            latest_location=latest.location_name,
            issue_count=len(recs),
            unique_notes=unique_notes,
            all_images=all_images,
            latest_issue_at=latest.issue_at or latest.log_date,
            score=score,
            priority=priority,
        ))

    summaries.sort(key=lambda s: (_PRIORITY_ORDER[s.priority], -s.score, -s.issue_count))
    return summaries


# ── Formatting ────────────────────────────────────────────────────────────────

def _format_summary(summaries: list[MachineSummary], days: int) -> str:
    if not summaries:
        return f"No issues found in the last {days} day(s)."

    lines = [
        "🔧 Technician Repair Priority",
        f"(last {days} day(s))\n",
    ]

    for rank, s in enumerate(summaries, start=1):
        issue_word = "issue" if s.issue_count == 1 else "issues"
        lines.append(f"{rank}. #{s.machine_id} · {s.issue_count} {issue_word}")
        lines.append(f"Location: {s.latest_location or 'Unknown'}")
        lines.append(f"Priority: {s.priority}")

        if s.unique_notes:
            lines.append("Issues:")
            for note in s.unique_notes:
                lines.append(f"- {note}")
        else:
            lines.append("Issues: (no note provided)")

        if s.all_images:
            lines.append(f"Photos: {len(s.all_images)} file(s)")
            for img in s.all_images:
                lines.append(f"  {img}")

        lines.append("")

    return "\n".join(lines).rstrip()


# ── Public API ────────────────────────────────────────────────────────────────

def generate_repair_priority_summary(days: int = 30) -> str:
    """Read issues from the last N days and return a ranked technician repair summary.

    Args:
        days: look-back window in calendar days (default 30). Pass 0 for all time.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
        if days > 0
        else datetime.min.replace(tzinfo=timezone.utc)
    )
    files     = _log_files_since(cutoff)
    records   = _collect_issues(files)
    summaries = _build_summaries(records)
    return _format_summary(summaries, days)


# ── CLI runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    days_arg = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    print(generate_repair_priority_summary(days=days_arg))
