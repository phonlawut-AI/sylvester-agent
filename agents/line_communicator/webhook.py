"""
FastAPI webhook server for LINE Messaging API events.
Handles postback and message events from the morning brief Flex card.

Run locally:
    uvicorn agents.line_communicator.webhook:app --reload --port 8000

Expose via ngrok for LINE to reach during development:
    ngrok http 8000
Then set the webhook URL in LINE Developer Console:
    https://<ngrok-id>.ngrok.io/webhook/line
"""

import hashlib
import hmac
import base64
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from config import LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, MANAGER_LINE_ID
from agents.line_communicator.sender import (
    reply_text,
    send_text,
    send_status_update_card,
    send_approval_request_card,
    send_welcome_card,
    send_role_selection_card,
    send_manager_menu_card,
)
from agents.registration import staff_registry
from storage import log_store

logger = logging.getLogger(__name__)

app = FastAPI(title="Flying Turtle LINE Webhook")

# Image storage root: morning-brief-agent/data/
_DATA_DIR = Path(__file__).parent.parent.parent / "data"

# ── In-memory pending-action registry ────────────────────────────────────────
# key: LINE userId
# value: {action, date, machine_id, location_name, push_target}
_PENDING: dict[str, dict] = {}


# ── Signature verification ────────────────────────────────────────────────────

def _verify_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        logger.warning("LINE_CHANNEL_SECRET not set — skipping signature check")
        return True
    mac = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode(), signature)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.post("/webhook/line")
async def line_webhook(request: Request) -> JSONResponse:
    body      = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not _verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = json.loads(body)

    for event in payload.get("events", []):
        event_type = event.get("type")
        if event_type == "postback":
            await _handle_postback(event)
        elif event_type == "message":
            await _handle_message_event(event)
        elif event_type == "follow":
            await _handle_follow_event(event)

    return JSONResponse({"status": "ok"})


# ── Shared helpers ────────────────────────────────────────────────────────────

def _get_push_target(event: dict) -> str:
    source = event.get("source", {})
    t = source.get("type", "user")
    if t == "group":
        return source.get("groupId", "")
    if t == "room":
        return source.get("roomId", "")
    return source.get("userId", "")


def _push_status_card(push_target: str, date: str) -> None:
    entry = log_store.get_latest_entry(date)
    if not entry or not push_target:
        return
    target = entry.get("target_line_id") or push_target
    try:
        send_status_update_card(target, entry)
    except Exception as exc:
        logger.error("Status card push failed: %s", exc)


def _reply(reply_token: str, text: str) -> None:
    if not reply_token:
        return
    try:
        reply_text(reply_token, text)
    except Exception as exc:
        logger.error("Reply failed: %s", exc)


def _find_machine(entry: dict | None, machine_id: str) -> dict | None:
    if not entry:
        return None
    for m in entry.get("machines", []):
        if m.get("machine_id") == machine_id:
            return m
    return None


async def _download_image(message_id: str) -> bytes:
    """Fetch image content from the LINE Content API."""
    url     = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.content


# ── Postback handlers ─────────────────────────────────────────────────────────

async def _handle_postback(event: dict) -> None:
    data         = event["postback"]["data"]
    reply_token  = event.get("replyToken", "")
    user_id      = event.get("source", {}).get("userId", "unknown")
    push_target  = _get_push_target(event)
    now          = datetime.now(timezone.utc).isoformat()

    parts  = data.split("|")
    action = parts[0] if parts else ""

    if action == "done" and len(parts) == 4:
        _, date, machine_id, location_name = parts

        # Duplicate guard
        existing = log_store.get_latest_entry(date)
        m = _find_machine(existing, machine_id)
        if m and m.get("visit_status") == "done":
            logger.info("Duplicate done ignored: %s", machine_id)
            return

        log_store.update_machine_status(date, machine_id, {
            "visit_status": "done",
            "done_at": now,
            "done_by": user_id,
        })
        _push_status_card(push_target, date)
        # No text reply for Done

    elif action == "issue" and len(parts) == 4:
        _, date, machine_id, location_name = parts

        log_store.update_machine_status(date, machine_id, {
            "issue_status": "pending",
            "issue_at": now,
            "issue_by": user_id,
        })

        # Register pending so follow-up messages are routed correctly
        _PENDING[user_id] = {
            "action": "issue",
            "date": date,
            "machine_id": machine_id,
            "location_name": location_name,
            "push_target": push_target,
        }

        _reply(reply_token, f"Please describe the issue for #{machine_id} and upload a photo.")

    elif action == "not_done" and len(parts) == 4:
        _, date, machine_id, location_name = parts

        log_store.update_machine_status(date, machine_id, {
            "not_done_at": now,
            "not_done_by": user_id,
        })

        _PENDING[user_id] = {
            "action": "not_done",
            "date": date,
            "machine_id": machine_id,
            "location_name": location_name,
            "push_target": push_target,
        }

        _reply(reply_token, f"Please explain why #{machine_id} was not completed and upload a photo.")

    elif action == "register_start":
        status = staff_registry.get_registration_status(user_id)
        if status == "approved":
            _reply(reply_token, "You are already registered in the system.")
            return
        if status == "pending":
            _reply(reply_token, "Your registration is pending manager approval.")
            return

        _PENDING[user_id] = {"action": "register_name", "push_target": push_target}
        _reply(reply_token, "Please enter your English name only.\nExample: Tom")

    elif action == "register_role" and len(parts) == 2:
        _, role = parts
        pending = _PENDING.get(user_id, {})

        if pending.get("action") != "register_role":
            _reply(reply_token, "No registration in progress. Please tap Register to start.")
            return

        english_name = pending.get("english_name", "")
        if not english_name:
            _PENDING.pop(user_id, None)
            _reply(reply_token, "Session expired. Please tap Register to start again.")
            return

        valid, error = staff_registry.validate_registration(english_name, role)
        if not valid:
            _PENDING.pop(user_id, None)
            _reply(reply_token, f"Registration failed: {error}")
            return

        staff_registry.submit_registration(user_id, english_name, role)
        _PENDING.pop(user_id, None)
        _reply(reply_token, "Registration submitted. Waiting for manager approval.")

        if not MANAGER_LINE_ID:
            logger.warning("MANAGER_LINE_ID not set — manager not notified for %s", english_name)
            return
        try:
            send_approval_request_card(MANAGER_LINE_ID, user_id, english_name, role)
        except Exception as exc:
            logger.error("Could not send approval card: %s", exc)

    elif action == "manager_cmd":
        await _handle_manager_menu_postback(event, parts)

    elif action == "approve_staff" and len(parts) == 4:
        _, target_user_id, english_name, role = parts
        approver_id = event.get("source", {}).get("userId", "")

        if MANAGER_LINE_ID and approver_id != MANAGER_LINE_ID:
            _reply(reply_token, "Not authorised to approve staff registrations.")
            return

        staff_registry.approve_staff(target_user_id, english_name, role, approver_id)

        try:
            send_text(target_user_id, f"Registration approved. You are registered as {english_name} ({role}).")
        except Exception as exc:
            logger.error("Could not notify approved staff %s: %s", target_user_id, exc)

        _reply(reply_token, f"✓ {english_name} ({role}) approved.")

    elif action == "reject_staff" and len(parts) == 2:
        _, target_user_id = parts
        approver_id = event.get("source", {}).get("userId", "")

        if MANAGER_LINE_ID and approver_id != MANAGER_LINE_ID:
            _reply(reply_token, "Not authorised to reject staff registrations.")
            return

        entry = staff_registry.reject_staff(target_user_id)
        name  = entry["english_name"] if entry else target_user_id

        try:
            send_text(target_user_id, "Registration rejected. Please contact your manager.")
        except Exception as exc:
            logger.error("Could not notify rejected staff %s: %s", target_user_id, exc)

        _reply(reply_token, f"✗ {name} rejected.")

    else:
        logger.debug("Unhandled postback: %s", data)


# ── Follow (bot add) handler ──────────────────────────────────────────────────

async def _handle_follow_event(event: dict) -> None:
    """Triggered when a user adds the bot as a friend."""
    user_id = event.get("source", {}).get("userId", "")
    if not user_id:
        return
    try:
        send_welcome_card(user_id)
    except Exception as exc:
        logger.error("Welcome card failed for new follower %s: %s", user_id, exc)


# ── Registration name-input handler ──────────────────────────────────────────

async def _handle_register_name_input(event: dict, text: str) -> None:
    """Called when _PENDING[user_id]['action'] == 'register_name'."""
    user_id     = event.get("source", {}).get("userId", "unknown")
    reply_token = event.get("replyToken", "")

    valid, error = staff_registry.validate_name(text)
    if not valid:
        _reply(
            reply_token,
            f"Invalid name: {error}\n"
            "Please try again. Example: Tom",
        )
        return

    pending = _PENDING.setdefault(user_id, {})
    pending["english_name"] = text
    pending["action"]       = "register_role"

    try:
        send_role_selection_card(user_id, text)
    except Exception as exc:
        logger.error("Role selection card failed for %s: %s", user_id, exc)
        _reply(reply_token, "Something went wrong. Please tap Register to try again.")
        _PENDING.pop(user_id, None)


# ── Registration command ───────────────────────────────────────────────────────

async def _handle_register_command(event: dict, text: str) -> None:
    """Handle: register <english_name> <role>"""
    user_id     = event.get("source", {}).get("userId", "unknown")
    reply_token = event.get("replyToken", "")

    parts = text.strip().split()
    if len(parts) != 3:
        _reply(
            reply_token,
            "Usage: register <name> <role>\n"
            "Example: register Tom refiller\n"
            f"Valid roles: {', '.join(sorted(staff_registry.VALID_ROLES))}",
        )
        return

    _, english_name, role = parts
    valid, error = staff_registry.validate_registration(english_name, role)
    if not valid:
        _reply(reply_token, f"Registration failed: {error}")
        return

    staff_registry.submit_registration(user_id, english_name, role)
    _reply(reply_token, "Registration submitted. Waiting for manager approval.")

    if not MANAGER_LINE_ID:
        logger.warning("MANAGER_LINE_ID not set — manager not notified of registration for %s", english_name)
        return

    try:
        send_approval_request_card(MANAGER_LINE_ID, user_id, english_name, role)
    except Exception as exc:
        logger.error("Could not send approval card to manager: %s", exc)


# ── Message event handler (follow-up text / images) ──────────────────────────

async def _handle_message_event(event: dict) -> None:
    user_id     = event.get("source", {}).get("userId", "unknown")
    reply_token = event.get("replyToken", "")
    msg         = event.get("message", {})
    msg_type    = msg.get("type", "")
    pending     = _PENDING.get(user_id)

    # ── Text messages ──────────────────────────────────────────────────────────
    if msg_type == "text":
        text = msg.get("text", "").strip()
        if not text:
            return

        # 1. Explicit CLI registration command (always available)
        if text.lower().startswith("register "):
            await _handle_register_command(event, text)
            return

        # 1b. Manager staff-management commands
        if text.lower().startswith("staff"):
            await _handle_staff_command(event, text)
            return

        # 1c. Manager daily report commands
        if text.lower().startswith("daily"):
            await _handle_daily_command(event, text)
            return

        # 1d. Manager menu command
        if text.lower().startswith("manager"):
            await _handle_manager_command(event, text)
            return

        # 2. Button-based registration flow states
        if pending and pending.get("action") == "register_name":
            await _handle_register_name_input(event, text)
            return

        if pending and pending.get("action") == "register_role":
            _reply(reply_token, "Please tap Refiller or Tech to complete your registration.")
            return

        # 3. Issue / not_done follow-up — only for approved staff with an active pending action
        if pending and pending.get("action") in ("issue", "not_done"):
            action      = pending["action"]
            date        = pending["date"]
            machine_id  = pending["machine_id"]
            push_target = pending.get("push_target", user_id)
            await _handle_follow_up_text(user_id, reply_token, action, date, machine_id, push_target, text)
            return

        # 4. Any other message → respond based on registration status
        status = staff_registry.get_registration_status(user_id)
        if status == "unregistered":
            try:
                send_welcome_card(user_id)
            except Exception as exc:
                logger.error("Welcome card failed: %s", exc)
        elif status == "pending":
            _reply(reply_token, "Your registration is pending manager approval.")
        elif status == "inactive":
            _reply(reply_token, "Your account has been deactivated. Please contact your manager.")

    # ── Image messages ─────────────────────────────────────────────────────────
    elif msg_type == "image":
        if not pending or pending.get("action") not in ("issue", "not_done"):
            return

        message_id  = msg.get("id", "")
        action      = pending["action"]
        date        = pending["date"]
        machine_id  = pending["machine_id"]
        push_target = pending.get("push_target", user_id)

        if message_id:
            await _handle_follow_up_image(user_id, reply_token, action, date, machine_id, push_target, message_id)


async def _handle_follow_up_text(
    user_id: str, reply_token: str, action: str,
    date: str, machine_id: str, push_target: str, text: str,
) -> None:
    if action == "issue":
        log_store.update_machine_status(date, machine_id, {"issue_note": text})

        entry = log_store.get_latest_entry(date)
        m     = _find_machine(entry, machine_id)

        if m and m.get("issue_images"):
            _finalise_issue(user_id, date, machine_id, push_target)
            _reply(reply_token, f"Issue recorded successfully for #{machine_id}.")
        else:
            _reply(reply_token, "Thanks. Please upload a photo of the issue.")

    elif action == "not_done":
        log_store.update_machine_status(date, machine_id, {"not_done_reason": text})

        entry = log_store.get_latest_entry(date)
        m     = _find_machine(entry, machine_id)

        if m and m.get("not_done_images"):
            _finalise_not_done(user_id, date, machine_id, push_target)
            _reply(reply_token, f"Not completed case recorded for #{machine_id}.")
        else:
            _reply(reply_token, "Thanks. Please upload a photo for verification.")


async def _handle_follow_up_image(
    user_id: str, reply_token: str, action: str,
    date: str, machine_id: str, push_target: str, message_id: str,
) -> None:
    try:
        image_data = await _download_image(message_id)
    except Exception as exc:
        logger.error("Image download failed: %s", exc)
        _reply(reply_token, "Sorry, could not save the image. Please try again.")
        return

    if action == "issue":
        save_dir  = _DATA_DIR / "issues" / date / machine_id
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f"{message_id}.jpg").write_bytes(image_data)
        log_store.append_to_machine_field(date, machine_id, "issue_images", str(save_dir / f"{message_id}.jpg"))

        entry = log_store.get_latest_entry(date)
        m     = _find_machine(entry, machine_id)

        if m and m.get("issue_note"):
            _finalise_issue(user_id, date, machine_id, push_target)
            _reply(reply_token, f"Issue recorded successfully for #{machine_id}.")
        else:
            _reply(reply_token, "Image received. Please provide issue details.")

    elif action == "not_done":
        save_dir  = _DATA_DIR / "not_done" / date / machine_id
        save_dir.mkdir(parents=True, exist_ok=True)
        (save_dir / f"{message_id}.jpg").write_bytes(image_data)
        log_store.append_to_machine_field(date, machine_id, "not_done_images", str(save_dir / f"{message_id}.jpg"))

        entry = log_store.get_latest_entry(date)
        m     = _find_machine(entry, machine_id)

        if m and m.get("not_done_reason"):
            _finalise_not_done(user_id, date, machine_id, push_target)
            _reply(reply_token, f"Not completed case recorded for #{machine_id}.")
        else:
            _reply(reply_token, "Image received. Please provide a reason.")


def _finalise_issue(user_id: str, date: str, machine_id: str, push_target: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    log_store.update_machine_status(date, machine_id, {
        "issue_status": "reported",
        "issue_at": now,
        "issue_by": user_id,
    })
    _PENDING.pop(user_id, None)
    _push_status_card(push_target, date)


def _finalise_not_done(user_id: str, date: str, machine_id: str, push_target: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    log_store.update_machine_status(date, machine_id, {
        "visit_status": "not_done",
        "not_done_at": now,
        "not_done_by": user_id,
    })
    _PENDING.pop(user_id, None)
    _push_status_card(push_target, date)


# ── Staff management commands (manager only) ─────────────────────────────────

def _staff_command_usage() -> str:
    return (
        "Staff management commands:\n"
        "  staff list\n"
        "  staff rename <old_name> <new_name>\n"
        "  staff update <name> employment <type>\n"
        f"    Types: {', '.join(sorted(staff_registry.VALID_EMPLOYMENT_TYPES))}\n"
        "  staff update <name> role <role>\n"
        f"    Roles: {', '.join(sorted(staff_registry.VALID_ROLES))}\n"
        "  staff deactivate <name>"
    )


async def _handle_staff_command(event: dict, text: str) -> None:
    user_id     = event.get("source", {}).get("userId", "unknown")
    reply_token = event.get("replyToken", "")

    if MANAGER_LINE_ID and user_id != MANAGER_LINE_ID:
        _reply(reply_token, "Staff management commands are only available to the manager.")
        return

    tokens = text.strip().split()
    # tokens[0] = "staff" (or just "staff" with no sub-command)
    if len(tokens) < 2:
        _reply(reply_token, _staff_command_usage())
        return

    sub = tokens[1].lower()

    # ── staff list ────────────────────────────────────────────────────────────
    if sub == "count":
        _reply(reply_token, _staff_count_text())

    elif sub == "list":
        _reply(reply_token, _staff_list_text())

    # ── staff update <name> employment <type>  /  staff update <name> role <role> ──
    elif sub == "update":
        if len(tokens) < 5:
            _reply(reply_token, _staff_command_usage())
            return

        name      = tokens[2]
        field_key = tokens[3].lower()
        new_value = tokens[4].lower()

        if field_key == "employment":
            if new_value not in staff_registry.VALID_EMPLOYMENT_TYPES:
                _reply(reply_token,
                       f"Invalid employment type '{new_value}'.\n"
                       f"Valid: {', '.join(sorted(staff_registry.VALID_EMPLOYMENT_TYPES))}")
                return
            ok, error = staff_registry.update_staff_field(name, "employment_type", new_value, user_id)
            _reply(reply_token,
                   f"✓ {name}'s employment type updated to '{new_value}'." if ok else f"Error: {error}")

        elif field_key == "role":
            if new_value not in staff_registry.VALID_ROLES:
                _reply(reply_token,
                       f"Invalid role '{new_value}'.\n"
                       f"Valid: {', '.join(sorted(staff_registry.VALID_ROLES))}")
                return
            ok, error = staff_registry.update_staff_field(name, "role", new_value, user_id)
            _reply(reply_token,
                   f"✓ {name}'s role updated to '{new_value}'." if ok else f"Error: {error}")

        else:
            _reply(reply_token, _staff_command_usage())

    # ── staff rename <old_name> <new_name> ────────────────────────────────────
    elif sub == "rename":
        if len(tokens) < 4:
            _reply(reply_token, _staff_command_usage())
            return
        old_name, new_name = tokens[2], tokens[3]
        ok, error = staff_registry.rename_staff(old_name, new_name, user_id)
        _reply(reply_token, f"Updated name: {old_name} → {new_name}" if ok else f"Error: {error}")

    # ── staff deactivate <name> ───────────────────────────────────────────────
    elif sub == "deactivate":
        if len(tokens) < 3:
            _reply(reply_token, _staff_command_usage())
            return
        name = tokens[2]
        ok, error = staff_registry.deactivate_staff(name, user_id)
        _reply(reply_token, f"✓ {name} deactivated." if ok else f"Error: {error}")

    else:
        _reply(reply_token, _staff_command_usage())


# ── Daily report commands (manager only) ─────────────────────────────────────

def _v(m: dict) -> str:
    """visit_status with backward-compat fallback."""
    if "visit_status" in m:
        return m["visit_status"]
    return "done" if m.get("status") == "done" else "pending"


def _i(m: dict) -> str:
    """issue_status with backward-compat fallback."""
    if "issue_status" in m:
        return m["issue_status"]
    return "reported" if m.get("status") == "issue" else "none"


def _latest_by_staff(entries: list[dict]) -> dict[str, dict]:
    """Return the most recent log entry per staff_name."""
    result: dict[str, dict] = {}
    for entry in entries:
        result[entry.get("staff_name", "Unknown")] = entry
    return result


def _build_daily_summary(date_str: str) -> str:
    entries = log_store.get_all_entries(date_str)
    if not entries:
        return f"No data found for {date_str}."

    by_staff = _latest_by_staff(entries)

    total = completed = not_done_total = pending_total = issues_total = 0
    staff_lines: list[str] = []

    for staff_name, entry in sorted(by_staff.items()):
        machines   = entry.get("machines", [])
        s_total    = len(machines)
        s_done     = sum(1 for m in machines if _v(m) == "done")
        s_not_done = sum(1 for m in machines if _v(m) == "not_done")
        s_pending  = sum(1 for m in machines if _v(m) == "pending")
        s_issues   = sum(1 for m in machines if _i(m) in ("pending", "reported"))

        total          += s_total
        completed      += s_done
        not_done_total += s_not_done
        pending_total  += s_pending
        issues_total   += s_issues

        parts = [f"{s_done}/{s_total} done"]
        if s_not_done:
            parts.append(f"{s_not_done} not done")
        if s_issues:
            parts.append(f"{s_issues} issue{'s' if s_issues != 1 else ''}")
        if s_pending:
            parts.append(f"{s_pending} pending")
        staff_lines.append(f"- {staff_name}: {' · '.join(parts)}")

    lines = [
        f"📊 Daily Ops Summary · {date_str}",
        "",
        "Overall",
        f"Visit completed: {completed}/{total}",
    ]
    if not_done_total:
        lines.append(f"Not done: {not_done_total}")
    if issues_total:
        lines.append(f"Issues: {issues_total}")
    if pending_total:
        lines.append(f"Pending: {pending_total}")
    lines += ["", "By staff"] + staff_lines

    return "\n".join(lines)


def _build_daily_issues(date_str: str) -> str:
    entries = log_store.get_all_entries(date_str)
    if not entries:
        return f"No data found for {date_str}."

    by_staff = _latest_by_staff(entries)
    blocks: list[str] = []

    for staff_name, entry in sorted(by_staff.items()):
        for m in entry.get("machines", []):
            if _i(m) not in ("pending", "reported"):
                continue

            machine_id   = m.get("machine_id", "")
            location     = m.get("location_name", "")
            note         = m.get("issue_note", "").strip() or "(no note)"
            image_count  = len(m.get("issue_images", []))
            reported_by  = m.get("issue_by", "")

            lines = [
                f"#{machine_id} · {location} ({staff_name})",
                f"Issue: {note}",
                f"Photos: {image_count}",
            ]
            if reported_by:
                lines.append(f"Reported by: {reported_by[:16]}…")
            blocks.append("\n".join(lines))

    if not blocks:
        return f"No issues reported on {date_str}."

    header = f"🔍 Issues · {date_str}\n"
    return header + "\n\n".join(blocks)


async def _handle_daily_command(event: dict, text: str) -> None:
    user_id     = event.get("source", {}).get("userId", "unknown")
    reply_token = event.get("replyToken", "")

    if MANAGER_LINE_ID and user_id != MANAGER_LINE_ID:
        _reply(reply_token, "Daily reports are only available to the manager.")
        return

    tokens   = text.strip().split()
    sub      = tokens[1].lower() if len(tokens) > 1 else ""
    date_arg = tokens[2] if len(tokens) > 2 else datetime.now(timezone.utc).date().isoformat()

    if sub == "summary":
        _reply(reply_token, _build_daily_summary(date_arg))
    elif sub == "issues":
        _reply(reply_token, _build_daily_issues(date_arg))
    else:
        _reply(reply_token, "Usage:\n  daily summary [YYYY-MM-DD]\n  daily issues [YYYY-MM-DD]")


# ── Manager menu text helpers ─────────────────────────────────────────────────

def _staff_list_text() -> str:
    entries = staff_registry.list_staff()
    if not entries:
        return "No staff registered yet."
    lines = [f"📋 Staff ({len(entries)} total)\n"]
    for s in entries:
        icon   = "✓" if s["status"] == "approved" else "✗"
        suffix = "  (inactive)" if s["status"] == "inactive" else ""
        lines.append(f"{icon} {s['name']} · {s['role']} · {s['employment_type']}{suffix}")
    return "\n".join(lines)


def _staff_count_text() -> str:
    all_staff = staff_registry.list_staff()
    approved  = [s for s in all_staff if s["status"] == "approved"]
    inactive  = [s for s in all_staff if s["status"] == "inactive"]
    pending   = staff_registry.get_all_pending()
    refillers = sum(1 for s in approved if s["role"] == "refiller")
    techs     = sum(1 for s in approved if s["role"] == "tech")
    return "\n".join([
        "👥 Staff Count",
        f"Approved: {len(approved)}",
        f"  Refiller: {refillers}",
        f"  Tech: {techs}",
        f"Inactive: {len(inactive)}",
        f"Pending approval: {len(pending)}",
    ])


def _pending_approvals_text() -> str:
    pending = staff_registry.get_all_pending()
    if not pending:
        return "No pending approvals."
    lines = [f"⏳ Pending Approvals ({len(pending)})\n"]
    for entry in pending.values():
        name   = entry.get("english_name", "")
        role   = entry.get("role", "")
        req_at = entry.get("requested_at", "")[:10]
        lines.append(f"• {name} ({role}) — {req_at}")
    return "\n".join(lines)


def _manager_help_text() -> str:
    return (
        "📋 Manager Commands\n\n"
        "Staff:\n"
        "  staff list\n"
        "  staff count\n"
        "  staff update <name> employment\n"
        "    <training|fulltime|parttime|contractor>\n"
        "  staff update <name> role <refiller|tech>\n"
        "  staff rename <old_name> <new_name>\n"
        "  staff deactivate <name>\n\n"
        "Daily Ops:\n"
        "  daily summary\n"
        "  daily summary YYYY-MM-DD\n"
        "  daily issues\n"
        "  daily issues YYYY-MM-DD\n\n"
        "Manager:\n"
        "  manager menu"
    )


# ── Manager menu handlers ─────────────────────────────────────────────────────

async def _handle_manager_command(event: dict, text: str) -> None:
    """Handle: manager menu"""
    user_id     = event.get("source", {}).get("userId", "unknown")
    reply_token = event.get("replyToken", "")

    if MANAGER_LINE_ID and user_id != MANAGER_LINE_ID:
        _reply(reply_token, "Not authorised.")
        return

    tokens = text.strip().split()
    sub    = tokens[1].lower() if len(tokens) > 1 else ""

    if sub == "menu":
        try:
            send_manager_menu_card(user_id)
        except Exception as exc:
            logger.error("Could not send manager menu: %s", exc)
            _reply(reply_token, "Could not load menu. Please try again.")
    else:
        _reply(reply_token, "Usage: manager menu")


async def _handle_manager_menu_postback(event: dict, parts: list[str]) -> None:
    """Route manager_cmd|* postbacks from the manager menu card."""
    user_id     = event.get("source", {}).get("userId", "unknown")
    reply_token = event.get("replyToken", "")

    if MANAGER_LINE_ID and user_id != MANAGER_LINE_ID:
        _reply(reply_token, "Not authorised.")
        return

    sub = parts[1] if len(parts) > 1 else ""

    if sub == "staff_list":
        _reply(reply_token, _staff_list_text())

    elif sub == "staff_count":
        _reply(reply_token, _staff_count_text())

    elif sub == "daily_summary":
        date_arg = (
            parts[2] if len(parts) > 2 and parts[2] != "today"
            else datetime.now(timezone.utc).date().isoformat()
        )
        _reply(reply_token, _build_daily_summary(date_arg))

    elif sub == "daily_issues":
        date_arg = (
            parts[2] if len(parts) > 2 and parts[2] != "today"
            else datetime.now(timezone.utc).date().isoformat()
        )
        _reply(reply_token, _build_daily_issues(date_arg))

    elif sub == "pending_approvals":
        _reply(reply_token, _pending_approvals_text())

    elif sub == "help":
        _reply(reply_token, _manager_help_text())

    else:
        _reply(reply_token, "Unknown command.")


# ── Local dev entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("agents.line_communicator.webhook:app", host="0.0.0.0", port=8000, reload=True)
