"""
LINE Messaging API wrapper.
Sends text messages and Flex messages to individual users or groups.
"""

import httpx
from config import LINE_CHANNEL_ACCESS_TOKEN
from parsers.plan_pdf import Plan
from agents.brief_generator import MaintenanceTask, FREQ_LABEL, MONTHS

_LINE_API       = "https://api.line.me/v2/bot/message/push"
_LINE_REPLY_API = "https://api.line.me/v2/bot/message/reply"
_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
}


# ── Basic send helpers ────────────────────────────────────────────────────────

def reply_text(reply_token: str, text: str) -> None:
    """Reply to a LINE event using a reply token (single use per event)."""
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}],
    }
    response = httpx.post(_LINE_REPLY_API, headers=_HEADERS, json=payload, timeout=15)
    response.raise_for_status()


def send_text(to: str, text: str) -> None:
    """Push a plain text message to a LINE user or group ID."""
    payload = {
        "to": to,
        "messages": [{"type": "text", "text": text}],
    }
    response = httpx.post(_LINE_API, headers=_HEADERS, json=payload, timeout=15)
    response.raise_for_status()


def send_texts(to: str, texts: list[str]) -> None:
    """Push up to 5 text messages per call; splits automatically if more."""
    chunks = [texts[i:i + 5] for i in range(0, len(texts), 5)]
    for chunk in chunks:
        payload = {
            "to": to,
            "messages": [{"type": "text", "text": t} for t in chunk],
        }
        response = httpx.post(_LINE_API, headers=_HEADERS, json=payload, timeout=15)
        response.raise_for_status()


# ── Flex primitives ───────────────────────────────────────────────────────────

def _short_date(iso_date: str) -> str:
    """YYYY-MM-DD → '10 Feb'"""
    try:
        parts = iso_date.split("-")
        return f"{int(parts[2])} {MONTHS[int(parts[1])]}"
    except Exception:
        return iso_date


def _chip(text: str, bg: str, color: str) -> dict:
    """Coloured status chip (not a button — display only)."""
    return {
        "type": "box",
        "layout": "horizontal",
        "backgroundColor": bg,
        "cornerRadius": "4px",
        "paddingAll": "8px",
        "flex": 1,
        "contents": [{
            "type": "text",
            "text": text,
            "color": color,
            "weight": "bold",
            "align": "center",
            "size": "sm",
        }],
    }


def _action_btn(label: str, data: str, color: str | None = None) -> dict:
    """Postback button.  color=None → LINE default primary green."""
    btn: dict = {
        "type": "button",
        "style": "primary",
        "height": "sm",
        "flex": 1,
        "action": {"type": "postback", "label": label, "data": data},
    }
    if color:
        btn["color"] = color
    return btn


# ── Status row builder ────────────────────────────────────────────────────────

def _status_row(visit_status: str, issue_status: str, plan_date: str,
                machine_id: str, location_name: str) -> dict:
    """Return the horizontal box containing the correct chips/buttons for a machine."""

    done_chip    = _chip("✓ Done",          "#E8F5E9", "#2E7D32")
    issue_chip   = _chip("⚠ Issue Reported","#FFF3E0", "#F57C00")
    not_done_chip= _chip("✖ Not Completed", "#FFEBEE", "#D32F2F")

    done_btn     = _action_btn("Mark Done",    f"done|{plan_date}|{machine_id}|{location_name}")
    issue_btn    = _action_btn("🟠 Issue",     f"issue|{plan_date}|{machine_id}|{location_name}", "#F57C00")
    not_done_btn = _action_btn("🔴 Not Done",  f"not_done|{plan_date}|{machine_id}|{location_name}", "#D32F2F")

    issue_active = issue_status in ("pending", "reported")

    if visit_status == "not_done":
        items = [not_done_chip]

    elif visit_status == "done" and issue_active:
        items = [done_chip, issue_chip]

    elif visit_status == "done":
        items = [done_chip]

    elif issue_active:
        items = [done_btn, issue_chip, not_done_btn]

    else:
        items = [done_btn, issue_btn, not_done_btn]

    return {
        "type": "box",
        "layout": "horizontal",
        "spacing": "sm",
        "margin": "sm",
        "contents": items,
    }


# ── Progress header line ──────────────────────────────────────────────────────

def _progress_line(machines: list[dict]) -> str:
    total     = len(machines)
    done      = sum(1 for m in machines if _visit(m) == "done")
    not_done  = sum(1 for m in machines if _visit(m) == "not_done")
    issues    = sum(1 for m in machines if _issue(m) in ("pending", "reported"))

    parts = [f"Visit completed: {done}/{total}"]
    if not_done:
        parts.append(f"Not done: {not_done}")
    if issues:
        parts.append(f"Issues: {issues}")
    return " · ".join(parts)


def _visit(m: dict) -> str:
    """Read visit_status with backward-compat fallback to old 'status' field."""
    if "visit_status" in m:
        return m["visit_status"]
    old = m.get("status", "pending")
    return "done" if old == "done" else "pending"


def _issue(m: dict) -> str:
    """Read issue_status with backward-compat fallback to old 'status' field."""
    if "issue_status" in m:
        return m["issue_status"]
    return "reported" if m.get("status") == "issue" else "none"


# ── Initial brief card (all machines pending) ─────────────────────────────────

def _machine_block(m: object, plan_date: str, tasks: list) -> dict:
    """Build one machine block for the initial morning brief (all pending)."""
    contents: list[dict] = [
        {
            "type": "text",
            "text": f"{m.machine_id} – {m.location_name}",
            "weight": "bold",
            "size": "sm",
            "wrap": True,
        },
    ]

    meta_parts: list[str] = []
    if m.arrival_time:
        meta_parts.append(f"⏰ {m.arrival_time}")
    if m.route:
        meta_parts.append(m.route)
    if meta_parts:
        contents.append({
            "type": "text",
            "text": " · ".join(meta_parts),
            "size": "xs",
            "color": "#666666",
            "wrap": True,
        })

    if m.notes:
        contents.append({
            "type": "text",
            "text": f"⚠️ {m.notes}",
            "size": "xs",
            "color": "#cc4400",
            "wrap": True,
        })

    for task in tasks:
        freq = FREQ_LABEL.get(task.frequency, task.frequency)
        freq_line = freq + (f" · Last done: {task.last_done_date}" if task.last_done_date else "")
        contents.append({
            "type": "box",
            "layout": "vertical",
            "margin": "sm",
            "spacing": "none",
            "contents": [
                {"type": "text", "text": f"🔁 {task.task_name}", "size": "xs", "color": "#0055aa", "wrap": True},
                {"type": "text", "text": freq_line, "size": "xs", "color": "#888888", "wrap": True},
            ],
        })

    # All pending on initial brief → 3 action buttons
    contents.append(_status_row("pending", "none", plan_date, m.machine_id, m.location_name))

    return {"type": "box", "layout": "vertical", "spacing": "xs", "margin": "md", "contents": contents}


def _build_flex_brief(plan: Plan, due_tasks: dict[str, list[MaintenanceTask]]) -> dict:
    """Build the initial morning brief Flex bubble (all machines pending)."""
    machines   = plan.machines
    total      = len(machines)
    task_count = sum(len(v) for v in due_tasks.values())
    date_str   = _short_date(plan.date)

    header_line2 = f"Today {date_str} · {total} machine{'s' if total != 1 else ''}"
    if task_count:
        header_line2 += f" · {task_count} recurring task{'s' if task_count != 1 else ''} due"

    body: list[dict] = [
        {"type": "text", "text": "📦 Today's Plan", "weight": "bold", "size": "md"},
        {"type": "separator", "margin": "sm"},
    ]
    for idx, m in enumerate(machines):
        body.append(_machine_block(m, plan.date, due_tasks.get(m.machine_id, [])))
        if idx < total - 1:
            body.append({"type": "separator", "margin": "md"})
    body.append({
        "type": "text",
        "text": "Tap Mark Done after visiting each machine.",
        "size": "xs", "color": "#888888", "wrap": True, "margin": "xl",
    })

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#FF8F00", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"🌅 Good morning {plan.staff_name}!", "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": header_line2, "color": "#FFE0B2", "size": "sm", "wrap": True},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px", "contents": body},
    }


def send_brief_with_done_buttons(
    target_id: str,
    brief: str,
    plan: Plan,
    due_tasks: dict | None = None,
) -> None:
    """Send the morning brief as a single Flex bubble with per-machine action buttons."""
    if due_tasks is None:
        due_tasks = {}

    bubble   = _build_flex_brief(plan, due_tasks)
    alt_lines = [l for l in brief.splitlines() if l.strip()]
    alt_text  = "\n".join(alt_lines[:2])[:400]

    payload = {
        "to": target_id,
        "messages": [{"type": "flex", "altText": alt_text, "contents": bubble}],
    }
    response = httpx.post(_LINE_API, headers=_HEADERS, json=payload, timeout=15)
    response.raise_for_status()


# ── Status-update card (pushed after any action tap) ─────────────────────────

def _build_status_machine_block(m: dict, plan_date: str) -> dict:
    """Build a machine block from a log-entry dict reflecting current status."""
    machine_id    = m["machine_id"]
    location_name = m["location_name"]
    visit_status  = _visit(m)
    issue_status  = _issue(m)

    contents: list[dict] = [
        {"type": "text", "text": f"{machine_id} – {location_name}", "weight": "bold", "size": "sm", "wrap": True}
    ]

    meta_parts: list[str] = []
    if m.get("arrival_time"):
        meta_parts.append(f"⏰ {m['arrival_time']}")
    if m.get("route"):
        meta_parts.append(m["route"])
    if meta_parts:
        contents.append({"type": "text", "text": " · ".join(meta_parts), "size": "xs", "color": "#666666", "wrap": True})

    if m.get("notes"):
        contents.append({"type": "text", "text": f"⚠️ {m['notes']}", "size": "xs", "color": "#cc4400", "wrap": True})

    for task in m.get("recurring_tasks", []):
        contents.append({"type": "text", "text": f"🔁 {task['task_name']}", "size": "xs", "color": "#0055aa", "wrap": True})

    contents.append(_status_row(visit_status, issue_status, plan_date, machine_id, location_name))

    return {"type": "box", "layout": "vertical", "spacing": "xs", "margin": "md", "contents": contents}


def _build_status_card(entry: dict) -> dict:
    """Build a full Flex bubble from a log-entry dict reflecting current machine statuses."""
    machines   = entry.get("machines", [])
    total      = len(machines)
    plan_date  = entry.get("date", "")
    staff_name = entry.get("staff_name", "")

    body: list[dict] = [
        {"type": "text", "text": "📦 Today's Plan", "weight": "bold", "size": "md"},
        {"type": "separator", "margin": "sm"},
    ]
    for idx, m in enumerate(machines):
        body.append(_build_status_machine_block(m, plan_date))
        if idx < total - 1:
            body.append({"type": "separator", "margin": "md"})

    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#FF8F00", "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": f"🌅 Good morning {staff_name}!", "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": _progress_line(machines), "color": "#FFE0B2", "size": "sm", "wrap": True},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "16px", "contents": body},
    }


def send_status_update_card(target_id: str, entry: dict) -> None:
    """Push a new Flex card showing the current visit/issue state of all machines."""
    machines = entry.get("machines", [])
    done     = sum(1 for m in machines if _visit(m) == "done")
    total    = len(machines)
    alt_text = f"Update: {done}/{total} done — {entry.get('staff_name', '')}"

    payload = {
        "to": target_id,
        "messages": [{"type": "flex", "altText": alt_text[:400], "contents": _build_status_card(entry)}],
    }
    response = httpx.post(_LINE_API, headers=_HEADERS, json=payload, timeout=15)
    response.raise_for_status()


# ── Registration onboarding cards ────────────────────────────────────────────

def send_welcome_card(target_id: str) -> None:
    """Push the welcome Flex card to a new / unregistered user."""
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1B5E20",
            "paddingAll": "14px",
            "contents": [{
                "type": "text",
                "text": "🐢 Flying Turtle Ops Bot",
                "color": "#FFFFFF",
                "weight": "bold",
                "size": "md",
            }],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "text",
                    "text": "Welcome to Flying Turtle Ops Bot 🐢",
                    "weight": "bold",
                    "size": "sm",
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": "Please register before using the system.",
                    "size": "sm",
                    "color": "#555555",
                    "wrap": True,
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "12px",
            "contents": [{
                "type": "button",
                "style": "primary",
                "height": "sm",
                "action": {
                    "type": "postback",
                    "label": "Register",
                    "data": "register_start",
                },
            }],
        },
    }

    payload = {
        "to": target_id,
        "messages": [{"type": "flex", "altText": "Welcome — please register to get started.", "contents": bubble}],
    }
    response = httpx.post(_LINE_API, headers=_HEADERS, json=payload, timeout=15)
    response.raise_for_status()


def send_role_selection_card(target_id: str, english_name: str) -> None:
    """Push a role-selection Flex card after the user's name is validated."""
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1565C0",
            "paddingAll": "14px",
            "contents": [{
                "type": "text",
                "text": "📋 Select Your Role",
                "color": "#FFFFFF",
                "weight": "bold",
                "size": "md",
            }],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "14px",
            "contents": [
                {
                    "type": "text",
                    "text": f"Hi {english_name}!",
                    "weight": "bold",
                    "size": "sm",
                },
                {
                    "type": "text",
                    "text": "Please select your role:",
                    "size": "sm",
                    "color": "#555555",
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#1565C0",
                    "height": "sm",
                    "flex": 1,
                    "action": {
                        "type": "postback",
                        "label": "Refiller",
                        "data": "register_role|refiller",
                    },
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#6A1B9A",
                    "height": "sm",
                    "flex": 1,
                    "action": {
                        "type": "postback",
                        "label": "Tech",
                        "data": "register_role|tech",
                    },
                },
            ],
        },
    }

    payload = {
        "to": target_id,
        "messages": [{"type": "flex", "altText": f"Hi {english_name}! Please select your role.", "contents": bubble}],
    }
    response = httpx.post(_LINE_API, headers=_HEADERS, json=payload, timeout=15)
    response.raise_for_status()


# ── Manager command menu card ────────────────────────────────────────────────

def send_manager_menu_card(manager_id: str) -> None:
    """Push the Manager Command Menu Flex card to the manager."""

    def _btn(label: str, data: str, color: str) -> dict:
        return {
            "type": "button",
            "style": "primary",
            "color": color,
            "height": "sm",
            "flex": 1,
            "action": {"type": "postback", "label": label, "data": data},
        }

    def _section_label(text: str) -> dict:
        return {
            "type": "text",
            "text": text,
            "size": "xxs",
            "color": "#9E9E9E",
            "weight": "bold",
            "margin": "md",
        }

    def _btn_row(*buttons: dict) -> dict:
        return {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "margin": "sm",
            "contents": list(buttons),
        }

    _BLUE   = "#1565C0"
    _TEAL   = "#00695C"
    _PURPLE = "#6A1B9A"
    _SLATE  = "#455A64"

    body_contents = [
        # ── Staff ────────────────────────────────────────────────────
        _section_label("STAFF"),
        _btn_row(
            _btn("📋 Staff List",  "manager_cmd|staff_list",  _BLUE),
            _btn("👥 Staff Count", "manager_cmd|staff_count", _BLUE),
        ),
        # ── Daily Ops ────────────────────────────────────────────────
        {"type": "separator", "margin": "md"},
        _section_label("DAILY OPS"),
        _btn_row(
            _btn("📊 Summary", "manager_cmd|daily_summary|today", _TEAL),
            _btn("🔍 Issues",  "manager_cmd|daily_issues|today",  _TEAL),
        ),
        # ── Admin ────────────────────────────────────────────────────
        {"type": "separator", "margin": "md"},
        _section_label("ADMIN"),
        _btn_row(
            _btn("⏳ Approvals",  "manager_cmd|pending_approvals", _PURPLE),
            _btn("❓ Help",       "manager_cmd|help",              _SLATE),
        ),
    ]

    bubble = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#0D47A1",
            "paddingAll": "16px",
            "contents": [
                {
                    "type": "text",
                    "text": "🔧 Manager Menu",
                    "color": "#FFFFFF",
                    "weight": "bold",
                    "size": "xl",
                },
                {
                    "type": "text",
                    "text": "Flying Turtle Operations",
                    "color": "#90CAF9",
                    "size": "xs",
                    "margin": "xs",
                },
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "14px",
            "paddingBottom": "18px",
            "contents": body_contents,
        },
    }

    payload = {
        "to": manager_id,
        "messages": [{"type": "flex", "altText": "🔧 Manager Menu — Flying Turtle Ops", "contents": bubble}],
    }
    response = httpx.post(_LINE_API, headers=_HEADERS, json=payload, timeout=15)
    response.raise_for_status()


# ── Staff registration card (sent to manager for approval) ───────────────────

def send_approval_request_card(
    manager_id: str,
    user_id: str,
    english_name: str,
    role: str,
) -> None:
    """Push a registration approval Flex card to the manager LINE ID."""
    bubble = {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1565C0",
            "paddingAll": "12px",
            "contents": [{
                "type": "text",
                "text": "🆕 Staff Registration Request",
                "color": "#FFFFFF",
                "weight": "bold",
                "size": "md",
            }],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": f"Name:    {english_name}", "size": "sm"},
                {"type": "text", "text": f"Role:    {role}",         "size": "sm"},
                {
                    "type": "text",
                    "text": f"LINE ID: {user_id}",
                    "size": "xs",
                    "color": "#888888",
                    "wrap": True,
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "paddingAll": "12px",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "height": "sm",
                    "flex": 1,
                    "action": {
                        "type": "postback",
                        "label": "Approve",
                        "data": f"approve_staff|{user_id}|{english_name}|{role}",
                    },
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#D32F2F",
                    "height": "sm",
                    "flex": 1,
                    "action": {
                        "type": "postback",
                        "label": "Reject",
                        "data": f"reject_staff|{user_id}",
                    },
                },
            ],
        },
    }

    payload = {
        "to": manager_id,
        "messages": [{
            "type": "flex",
            "altText": f"New registration: {english_name} ({role}) — approval needed",
            "contents": bubble,
        }],
    }
    response = httpx.post(_LINE_API, headers=_HEADERS, json=payload, timeout=15)
    response.raise_for_status()


# ── CLI test runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python -m agents.line_communicator.sender <LINE_USER_ID> <message>")
        sys.exit(1)

    target_id = sys.argv[1]
    message = " ".join(sys.argv[2:])
    send_text(target_id, message)
    print(f"Sent to {target_id}: {message[:60]}...")
