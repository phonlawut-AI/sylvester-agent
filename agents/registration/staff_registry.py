"""
Staff Registration Agent.

Handles self-registration, manager approval/rejection, and approved-staff lookups.

Data files (relative to project root):
  config/staff_registry.json           – approved staff keyed by english_name
  config/pending_staff_approvals.json  – requests awaiting manager approval, keyed by LINE userId
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
_REGISTRY   = _CONFIG_DIR / "staff_registry.json"
_PENDING    = _CONFIG_DIR / "pending_staff_approvals.json"

VALID_ROLES            = frozenset({"refiller", "tech"})
VALID_EMPLOYMENT_TYPES = frozenset({"training", "fulltime", "parttime", "contractor"})
_NAME_RE               = re.compile(r"^[A-Za-z]+$")


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Validation ────────────────────────────────────────────────────────────────

def validate_name(english_name: str) -> tuple[bool, str]:
    """Validate the name field before the role is known.
    Checks format, uniqueness in registry, and pending queue.
    """
    if not english_name or not _NAME_RE.match(english_name):
        return False, (
            "Name must contain English letters only (A–Z, a–z). "
            "No spaces, numbers, or special characters."
        )
    registry = _read(_REGISTRY)
    if english_name in registry:
        return False, f"'{english_name}' is already registered."
    pending = _read(_PENDING)
    for entry in pending.values():
        if entry.get("english_name") == english_name:
            return False, f"A registration for '{english_name}' is already pending approval."
    return True, ""


def validate_registration(english_name: str, role: str) -> tuple[bool, str]:
    """Return (True, "") if valid, otherwise (False, error_message)."""
    valid, error = validate_name(english_name)
    if not valid:
        return False, error
    if role not in VALID_ROLES:
        return False, f"Role must be one of: {', '.join(sorted(VALID_ROLES))}."
    return True, ""


def get_registration_status(user_id: str) -> str:
    """Return 'approved', 'inactive', 'pending', or 'unregistered' for a LINE userId."""
    registry = _read(_REGISTRY)
    for info in registry.values():
        if info.get("line_id") == user_id:
            return info.get("status", "approved")   # 'approved' or 'inactive'
    pending = _read(_PENDING)
    if user_id in pending:
        return "pending"
    return "unregistered"


# ── Registration ──────────────────────────────────────────────────────────────

def submit_registration(user_id: str, english_name: str, role: str) -> None:
    """Save a pending registration request (keyed by LINE userId)."""
    pending = _read(_PENDING)
    pending[user_id] = {
        "english_name": english_name,
        "role": role,
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }
    _write(_PENDING, pending)


def get_pending(user_id: str) -> dict | None:
    """Return the pending registration entry for user_id, or None."""
    return _read(_PENDING).get(user_id)


def get_all_pending() -> dict[str, dict]:
    """Return all pending registration requests keyed by LINE userId."""
    return _read(_PENDING)


# ── Approval / rejection ──────────────────────────────────────────────────────

def approve_staff(
    user_id: str,
    english_name: str,
    role: str,
    approved_by: str,
) -> None:
    """Move the registration from pending to the approved registry."""
    now      = datetime.now(timezone.utc).isoformat()
    registry = _read(_REGISTRY)
    registry[english_name] = {
        "line_id":         user_id,
        "role":            role,
        "employment_type": "training",   # default; manager can update later
        "status":          "approved",
        "approved_at":     now,
        "approved_by":     approved_by,
        "history":         [],
    }
    _write(_REGISTRY, registry)

    pending = _read(_PENDING)
    pending.pop(user_id, None)
    _write(_PENDING, pending)


def reject_staff(user_id: str) -> dict | None:
    """Remove user_id from pending and return the removed entry (or None)."""
    pending = _read(_PENDING)
    entry   = pending.pop(user_id, None)
    _write(_PENDING, pending)
    return entry


# ── Lookups ───────────────────────────────────────────────────────────────────

def lookup_refiller(staff_name: str) -> str | None:
    """Return the LINE ID for an approved refiller matching staff_name, or None."""
    entry = _read(_REGISTRY).get(staff_name, {})
    if entry.get("role") == "refiller" and entry.get("status") == "approved":
        return entry["line_id"]
    return None


def lookup_all_tech() -> list[dict]:
    """Return all approved tech staff as [{"name": ..., "line_id": ...}, ...]."""
    registry = _read(_REGISTRY)
    return [
        {"name": name, "line_id": info["line_id"]}
        for name, info in registry.items()
        if info.get("role") == "tech" and info.get("status") == "approved"
    ]


# ── Manager operations ────────────────────────────────────────────────────────

def list_staff() -> list[dict]:
    """Return all registry entries as a list, sorted by name."""
    registry = _read(_REGISTRY)
    return sorted(
        [
            {
                "name":            name,
                "role":            info.get("role", ""),
                "employment_type": info.get("employment_type", ""),
                "status":          info.get("status", ""),
            }
            for name, info in registry.items()
        ],
        key=lambda x: x["name"].lower(),
    )


def update_staff_field(
    english_name: str,
    field: str,
    new_value: str,
    changed_by: str,
) -> tuple[bool, str]:
    """Update a single field on an approved staff entry with full history tracking.

    Returns (True, "") on success or (False, error_message) on failure.
    """
    registry = _read(_REGISTRY)
    if english_name not in registry:
        return False, f"'{english_name}' not found in the registry."

    entry     = registry[english_name]
    old_value = entry.get(field)

    entry[field] = new_value

    if "history" not in entry or not isinstance(entry["history"], list):
        entry["history"] = []

    entry["history"].append({
        "changed_at": datetime.now(timezone.utc).isoformat(),
        "changed_by": changed_by,
        "field":      field,
        "old_value":  old_value,
        "new_value":  new_value,
    })

    _write(_REGISTRY, registry)
    return True, ""


def deactivate_staff(english_name: str, changed_by: str) -> tuple[bool, str]:
    """Set status = 'inactive' for the named staff member."""
    registry = _read(_REGISTRY)
    if english_name not in registry:
        return False, f"'{english_name}' not found in the registry."
    if registry[english_name].get("status") == "inactive":
        return False, f"'{english_name}' is already inactive."
    return update_staff_field(english_name, "status", "inactive", changed_by)


def rename_staff(old_name: str, new_name: str, changed_by: str) -> tuple[bool, str]:
    """Rename a registry entry from old_name to new_name.

    Preserves all existing fields (line_id, role, employment_type, etc.)
    and appends a history record for the rename.
    """
    if not new_name or not _NAME_RE.match(new_name):
        return False, "New name must contain English letters only (A–Z, a–z). No spaces or special characters."

    registry = _read(_REGISTRY)

    if old_name not in registry:
        return False, f"'{old_name}' not found in the registry."
    if new_name in registry:
        return False, f"'{new_name}' already exists in the registry."

    entry = registry.pop(old_name)

    if "history" not in entry or not isinstance(entry["history"], list):
        entry["history"] = []
    entry["history"].append({
        "changed_at": datetime.now(timezone.utc).isoformat(),
        "changed_by": changed_by,
        "field":      "name",
        "old_value":  old_name,
        "new_value":  new_name,
    })

    registry[new_name] = entry
    _write(_REGISTRY, registry)
    return True, ""
