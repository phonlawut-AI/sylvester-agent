import os
import hmac
import hashlib
import base64
import json
import requests
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

app = FastAPI()

LINE_CHANNEL_SECRET       = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_REPLY_URL            = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL             = "https://api.line.me/v2/bot/message/push"
LINE_CONTENT_URL          = "https://api-data.line.me/v2/bot/message/{}/content"
LINE_PROFILE_URL          = "https://api.line.me/v2/bot/profile/{}"

DRIVE_FOLDER_NAME = "Sylvester Stock Proofs"

# Manager LINE user ID — set in Railway env vars.
# Leave empty to allow anyone to manage items (useful for testing).
MANAGER_LINE_ID = os.environ.get("MANAGER_LINE_ID", "")


def _is_manager(user_id: str) -> bool:
    return not MANAGER_LINE_ID or user_id == MANAGER_LINE_ID

# ── In-memory state per LINE user ─────────────────────────────────────────────
# Possible actions: "stock_in" | "stock_out" | "add_item" | "awaiting_photo"
PENDING: dict[str, dict] = {}


# ── Credentials (shared for Sheets + Drive) ───────────────────────────────────

def _credentials() -> Credentials:
    creds_dict = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    return Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )


# ── Google Sheets ─────────────────────────────────────────────────────────────

def _gs_client() -> gspread.Client:
    return gspread.authorize(_credentials())

def _stock_ws() -> gspread.Worksheet:
    return _gs_client().open("Sylvester Inventory").worksheet("stock")

def _movements_ws() -> gspread.Worksheet:
    return _gs_client().open("Sylvester Inventory").worksheet("movements")

def _users_ws() -> gspread.Worksheet:
    return _gs_client().open("Sylvester Inventory").worksheet("users")

def _team_ws() -> gspread.Worksheet:
    return _gs_client().open("Sylvester Inventory").worksheet("team")


# ── Team helpers ──────────────────────────────────────────────────────────────

def _add_to_team(user_id: str, display_name: str, role: str):
    """Add member to team sheet on approval. Skips if already exists."""
    ws      = _team_ws()
    records = ws.get_all_records()
    for r in records:
        if r.get("user_id") == user_id:
            return  # already in team
    start_date = datetime.now().strftime("%Y-%m-%d")
    ws.append_row([user_id, display_name, role, start_date, "active"])

def _get_team_members() -> list[dict]:
    try:
        return _team_ws().get_all_records()
    except Exception:
        return []


# ── User registration helpers ─────────────────────────────────────────────────

def _get_user_record(user_id: str) -> dict | None:
    try:
        records = _users_ws().get_all_records()
        for r in records:
            if r.get("user_id") == user_id:
                return r
    except Exception:
        pass
    return None

def _register_user(user_id: str, display_name: str, role: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    _users_ws().append_row([user_id, display_name, role, "pending", now])

def _update_user_status(user_id: str, new_status: str) -> dict | None:
    ws      = _users_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if r.get("user_id") == user_id:
            ws.update_cell(i, 4, new_status)   # col 4 = status
            return r
    return None


# ── Google Drive ──────────────────────────────────────────────────────────────

def _drive_service():
    return build("drive", "v3", credentials=_credentials())

def _get_or_create_folder(service, name: str) -> str:
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    results = service.files().list(q=q, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder"},
        fields="id",
    ).execute()
    return folder["id"]

def _upload_to_drive(image_bytes: bytes, filename: str) -> str:
    """Upload image to Google Drive and return a shareable link."""
    service   = _drive_service()
    folder_id = _get_or_create_folder(service, DRIVE_FOLDER_NAME)

    media = MediaInMemoryUpload(image_bytes, mimetype="image/jpeg", resumable=False)
    file  = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media,
        fields="id,webViewLink",
    ).execute()

    # Make file viewable by anyone with the link
    service.permissions().create(
        fileId=file["id"],
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return file.get("webViewLink", "")


# ── LINE helpers ──────────────────────────────────────────────────────────────

def _get_display_name(user_id: str) -> str:
    """Fetch LINE display name for a user ID."""
    try:
        resp = requests.get(
            LINE_PROFILE_URL.format(user_id),
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json().get("displayName", user_id)
    except Exception as e:
        print("GET PROFILE ERROR:", repr(e))
    return user_id  # fallback to ID if profile fetch fails

def _push_message(to: str, messages: list[dict]):
    """Push message to a user without a reply token (proactive send)."""
    if not to:
        return
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    resp = requests.post(LINE_PUSH_URL, headers=headers,
                         json={"to": to, "messages": messages})
    print("LINE push status:", resp.status_code, resp.text)


def _download_line_image(message_id: str) -> bytes:
    """Download image content from LINE Content API."""
    resp = requests.get(
        LINE_CONTENT_URL.format(message_id),
        headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise Exception(f"LINE content API error {resp.status_code}")
    return resp.content


# ── Google Sheets command handlers ────────────────────────────────────────────

def _log(item: str, move_type: str, qty: int,
         user_id: str = "", user_name: str = "", photo_link: str = ""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    _movements_ws().append_row([now, item, move_type, qty, user_id, user_name, photo_link])

def cmd_add_item(name: str, user_id: str = "", user_name: str = "") -> str:
    ws      = _stock_ws()
    records = ws.get_all_records()
    for r in records:
        if str(r.get("item", "")).lower() == name.lower():
            return f"'{name}' already exists.\nCurrent quantity: {r.get('quantity', 0)}"
    ws.append_row([name, 0, "active"])
    _log(name, "ADD", 0, user_id, user_name)
    return f"✅ Item added: {name}\nQuantity: 0\nStatus: active"

def cmd_stock_in(item: str, qty: int,
                 user_id: str = "", user_name: str = "") -> str:
    ws      = _stock_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("item", "")).lower() == item.lower():
            if r.get("status", "active") != "active":
                return f"❌ '{item}' is inactive.\nAsk the manager to activate it first."
            new_qty = int(r.get("quantity", 0)) + qty
            ws.update_cell(i, 2, new_qty)
            _log(item, "IN", qty, user_id, user_name)
            return (f"✅ Stock IN recorded\n"
                    f"Item: {item}\nAdded: +{qty}\nNew balance: {new_qty}\n"
                    f"By: {user_name}")
    return f"❌ '{item}' not found.\nAdd it first with: add item {item}"

def cmd_stock_out(item: str, qty: int,
                  user_id: str = "", user_name: str = "") -> str:
    ws      = _stock_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("item", "")).lower() == item.lower():
            if r.get("status", "active") != "active":
                return f"❌ '{item}' is inactive.\nAsk the manager to activate it first."
            current = int(r.get("quantity", 0))
            if qty > current:
                return f"❌ Insufficient stock.\n'{item}' only has {current} remaining."
            new_qty = current - qty
            ws.update_cell(i, 2, new_qty)
            _log(item, "OUT", qty, user_id, user_name)
            return (f"✅ Stock OUT recorded\n"
                    f"Item: {item}\nRemoved: -{qty}\nNew balance: {new_qty}\n"
                    f"By: {user_name}")
    return f"❌ '{item}' not found.\nAdd it first with: add item {item}"

def _set_item_status(item_name: str, new_status: str) -> str:
    ws      = _stock_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("item", "")).lower() == item_name.lower():
            ws.update_cell(i, 3, new_status)   # col 3 = status
            icon = "✅" if new_status == "active" else "🔴"
            return f"{icon} '{item_name}' is now {new_status}."
    return f"❌ Item '{item_name}' not found."

def _fetch_items(active_only: bool = False) -> list[dict]:
    try:
        records = _stock_ws().get_all_records()
        if active_only:
            return [r for r in records if r.get("status", "active") == "active"]
        return records
    except Exception:
        return []


# ── Backup typed command router ───────────────────────────────────────────────

def handle_message(text: str) -> str | None:
    t = text.strip().lower()
    if t.startswith("add item "):
        name = text.strip()[9:].strip()
        return cmd_add_item(name) if name else "Usage: add item <name>"
    if t.startswith("stock in "):
        parts = text.strip().split()
        if len(parts) < 4:
            return "Usage: stock in <item> <qty>"
        try:
            return cmd_stock_in(" ".join(parts[2:-1]), int(parts[-1]))
        except ValueError:
            return "Quantity must be a number."
    if t.startswith("stock out "):
        parts = text.strip().split()
        if len(parts) < 4:
            return "Usage: stock out <item> <qty>"
        try:
            return cmd_stock_out(" ".join(parts[2:-1]), int(parts[-1]))
        except ValueError:
            return "Quantity must be a number."
    return None


# ── LINE send helpers ─────────────────────────────────────────────────────────

def _send(reply_token: str, messages: list[dict]) -> bool:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    response = requests.post(LINE_REPLY_URL, headers=headers,
                             json={"replyToken": reply_token, "messages": messages})
    print("LINE reply status:", response.status_code, response.text)
    if response.status_code >= 400:
        raise Exception(f"LINE API error: {response.status_code} {response.text}")
    return True

def _reply(reply_token: str, text: str) -> bool:
    return _send(reply_token, [{"type": "text", "text": text[:5000]}])

def _reply_flex(reply_token: str, bubble: dict, alt_text: str) -> bool:
    return _send(reply_token, [{"type": "flex", "altText": alt_text, "contents": bubble}])

def _reply_with_quick_reply(reply_token: str, text: str, chips: list[str]) -> bool:
    items = [
        {"type": "action", "action": {"type": "message", "label": c[:20], "text": c}}
        for c in chips[:13]
    ]
    return _send(reply_token, [{"type": "text", "text": text,
                                "quickReply": {"items": items}}])


# ── Flex bubble builders ──────────────────────────────────────────────────────

MENU_TEXT = "Send 'menu' to see available commands."

def _menu_flex_bubble() -> dict:
    def _btn(label: str, text: str, color: str) -> dict:
        return {
            "type": "button", "style": "primary", "color": color, "height": "sm",
            "action": {"type": "message", "label": label, "text": text},
        }
    return {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A237E", "paddingAll": "18px",
            "contents": [
                {"type": "text", "text": "📦 Sylvester", "color": "#FFFFFF",
                 "weight": "bold", "size": "xxl"},
                {"type": "text", "text": "Warehouse Assistant", "color": "#9FA8DA",
                 "size": "sm", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "sm", "paddingAll": "14px",
            "contents": [
                _btn("📊 Stock Balance",  "stock balance",  "#1565C0"),
                _btn("📋 Items List",     "items",          "#00695C"),
                _btn("➕ Stock In",       "stock in",       "#2E7D32"),
                _btn("➖ Stock Out",      "stock out",      "#C62828"),
                _btn("🆕 Add New Item",  "add item",       "#6A1B9A"),
                _btn("👥 Team",          "team",           "#263238"),
                _btn("🔧 Manage Items",  "manage items",   "#37474F"),
            ],
        },
    }

def _balance_flex_bubble(records: list[dict]) -> dict:
    rows = []
    if not records:
        rows = [{"type": "text", "text": "No items yet.", "color": "#888888",
                 "size": "sm", "align": "center"}]
    else:
        for r in records:
            qty       = int(r.get("quantity", 0))
            qty_color = "#C62828" if qty == 0 else "#F57C00" if qty < 5 else "#2E7D32"
            rows.append({
                "type": "box", "layout": "horizontal", "paddingAll": "6px",
                "contents": [
                    {"type": "text", "text": r["item"], "size": "sm",
                     "flex": 3, "color": "#333333"},
                    {"type": "text", "text": str(qty), "size": "sm", "flex": 1,
                     "align": "end", "weight": "bold", "color": qty_color},
                ],
            })
            rows.append({"type": "separator"})
        if rows:
            rows.pop()
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1565C0", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "📦 Stock Balance", "color": "#FFFFFF",
                 "weight": "bold", "size": "lg"},
                {"type": "text",
                 "text": datetime.now().strftime("Updated %d %b %Y %H:%M"),
                 "color": "#BBDEFB", "size": "xs", "margin": "xs"},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "none",
                 "paddingAll": "14px", "contents": rows},
    }

def _items_flex_bubble(records: list[dict]) -> dict:
    rows = []
    if not records:
        rows = [{"type": "text", "text": "No items yet.", "color": "#888888",
                 "size": "sm", "align": "center"}]
    else:
        for r in records:
            qty         = int(r.get("quantity", 0))
            badge_color = "#C62828" if qty == 0 else "#2E7D32"
            rows.append({
                "type": "box", "layout": "horizontal",
                "paddingTop": "6px", "paddingBottom": "6px",
                "contents": [
                    {"type": "text", "text": r["item"], "size": "sm",
                     "flex": 4, "color": "#333333"},
                    {
                        "type": "box", "layout": "vertical", "flex": 1,
                        "contents": [{
                            "type": "box", "layout": "vertical",
                            "backgroundColor": badge_color,
                            "cornerRadius": "8px", "paddingAll": "4px",
                            "contents": [{
                                "type": "text", "text": str(qty),
                                "size": "xs", "color": "#FFFFFF", "align": "center",
                            }],
                        }],
                    },
                ],
            })
            rows.append({"type": "separator"})
        if rows:
            rows.pop()
    return {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#00695C", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "📋 Item List", "color": "#FFFFFF",
                 "weight": "bold", "size": "lg"},
                {"type": "text", "text": f"{len(records)} items",
                 "color": "#B2DFDB", "size": "xs", "margin": "xs"},
            ],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "none",
                 "paddingAll": "14px", "contents": rows},
    }

def _team_flex(members: list[dict]) -> tuple[dict, str]:
    role_colors = {
        "manager":              "#4A148C",
        "warehouse_supervisor": "#00695C",
        "warehouse_staff":      "#1565C0",
    }
    rows = []
    if not members:
        rows = [{"type": "text", "text": "No team members yet.",
                 "color": "#888888", "size": "sm", "align": "center"}]
    else:
        for r in members:
            role      = r.get("role", "")
            color     = role_colors.get(role, "#455A64")
            role_label = role.replace("_", " ").title()
            is_active  = r.get("status", "active") == "active"
            rows.append({
                "type": "box", "layout": "vertical",
                "paddingTop": "8px", "paddingBottom": "8px",
                "contents": [
                    {"type": "box", "layout": "horizontal", "contents": [
                        {"type": "text", "text": r.get("display_name", ""),
                         "size": "sm", "weight": "bold", "flex": 4,
                         "color": "#333333" if is_active else "#AAAAAA"},
                        {
                            "type": "box", "layout": "vertical", "flex": 3,
                            "contents": [{
                                "type": "box", "layout": "vertical",
                                "backgroundColor": color if is_active else "#9E9E9E",
                                "cornerRadius": "8px", "paddingAll": "3px",
                                "contents": [{
                                    "type": "text", "text": role_label,
                                    "size": "xxs", "color": "#FFFFFF",
                                    "align": "center",
                                }],
                            }],
                        },
                    ]},
                    {"type": "text",
                     "text": f"Since {r.get('start_date', '')}",
                     "size": "xxs", "color": "#AAAAAA", "margin": "xs"},
                ],
            })
            rows.append({"type": "separator"})
        if rows:
            rows.pop()

    active_count = sum(1 for r in members if r.get("status", "active") == "active")
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#263238", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "👥 Team Members", "color": "#FFFFFF",
                 "weight": "bold", "size": "lg"},
                {"type": "text",
                 "text": f"{active_count} active · {len(members)} total",
                 "color": "#B0BEC5", "size": "xs", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "none",
            "paddingAll": "12px", "contents": rows,
        },
    }
    return bubble, "👥 Team Members"


def _welcome_flex(display_name: str) -> tuple[dict, str]:
    def _role_btn(label: str, role: str, color: str) -> dict:
        return {
            "type": "button", "style": "primary", "color": color, "height": "sm",
            "action": {"type": "postback", "label": label,
                       "data": f"register_role|{role}"},
        }
    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#1A237E", "paddingAll": "18px",
            "contents": [
                {"type": "text", "text": "📦 Sylvester", "color": "#FFFFFF",
                 "weight": "bold", "size": "xxl"},
                {"type": "text", "text": "Warehouse Assistant",
                 "color": "#9FA8DA", "size": "sm", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "16px", "spacing": "sm",
            "contents": [
                {"type": "text", "text": f"Hello, {display_name}! 👋",
                 "weight": "bold", "size": "lg"},
                {"type": "text",
                 "text": "Please select your position to request access.",
                 "size": "sm", "color": "#555555", "wrap": True, "margin": "sm"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "spacing": "sm", "paddingAll": "12px",
            "contents": [
                _role_btn("🏭 Warehouse Staff",       "warehouse_staff",       "#1565C0"),
                _role_btn("📋 Warehouse Supervisor",  "warehouse_supervisor",  "#00695C"),
                _role_btn("👔 Manager",               "manager",               "#4A148C"),
            ],
        },
    }
    return bubble, "Welcome to Sylvester Warehouse!"


def _approval_flex(target_user_id: str, display_name: str, role: str) -> tuple[dict, str]:
    role_label = role.replace("_", " ").title()
    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#E65100", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "🆕 Registration Request",
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "Approval required",
                 "color": "#FFE0B2", "size": "xs", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "16px", "spacing": "sm",
            "contents": [
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "Name", "size": "xs",
                     "color": "#888888", "flex": 2},
                    {"type": "text", "text": display_name, "size": "sm",
                     "weight": "bold", "flex": 4, "wrap": True},
                ]},
                {"type": "separator"},
                {"type": "box", "layout": "horizontal", "contents": [
                    {"type": "text", "text": "Role", "size": "xs",
                     "color": "#888888", "flex": 2},
                    {"type": "text", "text": role_label, "size": "sm",
                     "weight": "bold", "flex": 4},
                ]},
            ],
        },
        "footer": {
            "type": "box", "layout": "horizontal",
            "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {
                    "type": "button", "style": "primary",
                    "color": "#1B5E20", "height": "sm", "flex": 1,
                    "action": {"type": "postback", "label": "✅ Approve",
                               "data": f"approve_user|{target_user_id}"},
                },
                {
                    "type": "button", "style": "primary",
                    "color": "#B71C1C", "height": "sm", "flex": 1,
                    "action": {"type": "postback", "label": "❌ Reject",
                               "data": f"reject_user|{target_user_id}"},
                },
            ],
        },
    }
    return bubble, f"New registration: {display_name} ({role_label})"


def _manage_items_flex(records: list[dict]) -> tuple[dict, str]:
    rows = []
    for r in records:
        item      = r.get("item", "")
        status    = r.get("status", "active")
        is_active = status == "active"

        rows.append({
            "type": "box", "layout": "horizontal",
            "paddingTop": "8px", "paddingBottom": "8px",
            "contents": [
                {"type": "text", "text": item, "size": "sm", "flex": 4,
                 "color": "#333333" if is_active else "#AAAAAA",
                 "gravity": "center"},
                {
                    "type": "box", "layout": "vertical", "flex": 2,
                    "justifyContent": "center",
                    "contents": [{
                        "type": "box", "layout": "vertical",
                        "backgroundColor": "#2E7D32" if is_active else "#9E9E9E",
                        "cornerRadius": "10px", "paddingAll": "4px",
                        "contents": [{
                            "type": "text",
                            "text": "Active" if is_active else "Inactive",
                            "size": "xxs", "color": "#FFFFFF", "align": "center",
                        }],
                    }],
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#B71C1C" if is_active else "#1B5E20",
                    "height": "sm", "flex": 2,
                    "action": {
                        "type": "postback",
                        "label": "Deactivate" if is_active else "Activate",
                        "data": f"deactivate|{item}" if is_active else f"activate|{item}",
                    },
                },
            ],
        })
        rows.append({"type": "separator"})

    if rows:
        rows.pop()  # remove trailing separator

    if not rows:
        rows = [{"type": "text", "text": "No items yet.",
                 "size": "sm", "color": "#888888", "align": "center"}]

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": "#37474F", "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "🔧 Manage Items", "color": "#FFFFFF",
                 "weight": "bold", "size": "lg"},
                {"type": "text", "text": "Manager: activate or deactivate items",
                 "color": "#CFD8DC", "size": "xs", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "none",
            "paddingAll": "12px", "contents": rows,
        },
    }
    return bubble, "🔧 Manage Items"


def _confirm_flex_bubble(action: str, item: str, qty: int | None) -> tuple[dict, str]:
    titles  = {"stock_in": "➕ Stock In", "stock_out": "➖ Stock Out",
               "add_item": "🆕 Add Item"}
    colors  = {"stock_in": "#1B5E20", "stock_out": "#B71C1C", "add_item": "#4A148C"}
    hdr_col = colors[action]

    if action == "add_item":
        body_rows = [
            {"type": "text", "text": "New item to add:", "size": "xs", "color": "#888888"},
            {"type": "text", "text": item, "size": "xl",
             "weight": "bold", "color": "#333333", "margin": "sm"},
        ]
        alt = f"Confirm add item: {item}?"
    else:
        symbol = "+" if action == "stock_in" else "−"
        body_rows = [
            {"type": "box", "layout": "horizontal", "contents": [
                {"type": "text", "text": "Item", "size": "xs", "color": "#888888", "flex": 2},
                {"type": "text", "text": item, "size": "sm",
                 "weight": "bold", "color": "#333333", "flex": 3},
            ]},
            {"type": "separator", "margin": "sm"},
            {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
                {"type": "text", "text": "Qty", "size": "xs", "color": "#888888", "flex": 2},
                {"type": "text", "text": f"{symbol}{qty}",
                 "size": "sm", "weight": "bold", "color": hdr_col, "flex": 3},
            ]},
        ]
        alt = f"Confirm {action.replace('_', ' ')}: {item} {symbol}{qty}?"

    bubble = {
        "type": "bubble", "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical",
            "backgroundColor": hdr_col, "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": titles[action],
                 "color": "#FFFFFF", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "Review before confirming",
                 "color": "#FFFFFF", "size": "xs", "margin": "xs"},
            ],
        },
        "body": {"type": "box", "layout": "vertical",
                 "paddingAll": "16px", "spacing": "sm", "contents": body_rows},
        "footer": {
            "type": "box", "layout": "horizontal",
            "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {"type": "button", "style": "primary", "color": "#1B5E20",
                 "height": "sm", "flex": 1,
                 "action": {"type": "postback", "label": "✅ Confirm",
                            "data": f"confirm_{action}"}},
                {"type": "button", "style": "primary", "color": "#B71C1C",
                 "height": "sm", "flex": 1,
                 "action": {"type": "postback", "label": "❌ Cancel", "data": "cancel"}},
            ],
        },
    }
    return bubble, alt


# ── Step-by-step state machine ────────────────────────────────────────────────

def _start_flow(user_id: str, action: str, reply_token: str):
    PENDING[user_id] = {"action": action, "item": None, "qty": None}
    if action in ("stock_in", "stock_out"):
        records    = _fetch_items(active_only=True)
        item_names = [r["item"] for r in records if r.get("item")]
        prompt     = ("➕ Select item to add stock:" if action == "stock_in"
                      else "➖ Select item to remove stock:")
        if item_names:
            _reply_with_quick_reply(reply_token, prompt, item_names)
        else:
            _reply(reply_token, f"{prompt}\n\n(No items yet — add items first)")
    else:
        _reply(reply_token, "🆕 Add Item\n\nEnter the new item name:")


def _handle_pending(user_id: str, reply_token: str, user_text: str):
    pending = PENDING[user_id]
    action  = pending["action"]
    t       = user_text.strip().lower()

    # ── Awaiting photo after stock in ────────────────────────────────────────
    if action == "awaiting_photo":
        if t == "skip":
            PENDING.pop(user_id, None)
            _reply(reply_token, "✅ Done.\nStock IN saved without photo.")
        else:
            _reply_with_quick_reply(
                reply_token,
                "📸 Please send a photo as proof,\nor tap Skip to finish.",
                ["Skip 📷"],
            )
        return

    # ── Escape hatch ─────────────────────────────────────────────────────────
    if t in ("cancel", "menu", "hi", "start"):
        PENDING.pop(user_id, None)
        if t in ("menu", "hi", "start"):
            try:
                _reply_flex(reply_token, _menu_flex_bubble(), "📦 Sylvester Warehouse Menu")
            except Exception:
                _reply(reply_token, MENU_TEXT)
        else:
            _reply(reply_token, "Cancelled ✖\nSend 'menu' to start over.")
        return

    # ── Step 1: item name ────────────────────────────────────────────────────
    if pending["item"] is None:
        item = user_text.strip()
        if not item:
            _reply(reply_token, "Please enter a valid item name:")
            return
        PENDING[user_id]["item"] = item
        if action == "add_item":
            bubble, alt = _confirm_flex_bubble(action, item, None)
            _reply_flex(reply_token, bubble, alt)
        else:
            _reply(reply_token, f"Item: {item}\n\nEnter quantity:")
        return

    # ── Step 2: quantity (stock_in / stock_out only) ─────────────────────────
    if pending["qty"] is None and action in ("stock_in", "stock_out"):
        try:
            qty = int(user_text.strip())
            if qty <= 0:
                raise ValueError
        except ValueError:
            _reply(reply_token, "⚠️ Please enter a valid number greater than 0:")
            return
        PENDING[user_id]["qty"] = qty
        bubble, alt = _confirm_flex_bubble(action, pending["item"], qty)
        _reply_flex(reply_token, bubble, alt)


def _handle_postback(event: dict):
    data        = event["postback"]["data"]
    reply_token = event.get("replyToken", "")
    user_id     = event.get("source", {}).get("userId", "unknown")
    pending     = PENDING.get(user_id, {})

    if data == "cancel":
        PENDING.pop(user_id, None)
        _reply(reply_token, "Cancelled ✖\nSend 'menu' to start over.")
        return

    # ── Registration: role selection ──────────────────────────────────────────
    if data.startswith("register_role|"):
        role         = data.split("|", 1)[1]
        display_name = _get_display_name(user_id)
        existing     = _get_user_record(user_id)

        if existing:
            status = existing.get("status", "pending")
            if status == "approved":
                _reply(reply_token,
                       f"✅ You're already registered as "
                       f"{existing.get('role','').replace('_',' ').title()}.")
            else:
                _reply(reply_token,
                       f"⏳ Your registration is {status}. Please wait.")
            return

        # Manager auto-approves themselves
        if _is_manager(user_id):
            _register_user(user_id, display_name, role)
            _update_user_status(user_id, "approved")
            _add_to_team(user_id, display_name, role)
            _reply(reply_token,
                   f"✅ Welcome, {display_name}!\n"
                   f"Auto-approved as Manager.\nSend 'menu' to get started.")
            return

        _register_user(user_id, display_name, role)

        # Notify manager
        if MANAGER_LINE_ID:
            bubble, alt = _approval_flex(user_id, display_name, role)
            _push_message(MANAGER_LINE_ID,
                          [{"type": "flex", "altText": alt, "contents": bubble}])

        role_label = role.replace("_", " ").title()
        _reply(reply_token,
               f"✅ Registration submitted!\n\n"
               f"Name: {display_name}\n"
               f"Role: {role_label}\n\n"
               f"⏳ Waiting for manager approval.\n"
               f"You'll be notified once approved.")
        return

    # ── Registration: approve / reject ────────────────────────────────────────
    if data.startswith("approve_user|") or data.startswith("reject_user|"):
        if not _is_manager(user_id):
            _reply(reply_token, "🔒 Only the manager can approve registrations.")
            return

        action_str, target_user_id = data.split("|", 1)
        new_status = "approved" if action_str == "approve_user" else "rejected"
        user_data  = _update_user_status(target_user_id, new_status)

        if not user_data:
            _reply(reply_token, "❌ User not found.")
            return

        name       = user_data.get("display_name", target_user_id)
        role_label = user_data.get("role", "").replace("_", " ").title()

        if new_status == "approved":
            _add_to_team(target_user_id, name,
                         user_data.get("role", ""))
            _push_message(target_user_id, [{
                "type": "text",
                "text": (f"✅ Your registration has been approved!\n\n"
                         f"Welcome, {name}!\n"
                         f"Role: {role_label}\n\n"
                         f"Send 'menu' to get started."),
            }])
            _reply(reply_token, f"✅ {name} ({role_label}) approved.")
        else:
            _push_message(target_user_id, [{
                "type": "text",
                "text": ("❌ Your registration was rejected.\n"
                         "Please contact the manager for more information."),
            }])
            _reply(reply_token, f"❌ {name} ({role_label}) rejected.")
        return

    if data.startswith("activate|") or data.startswith("deactivate|"):
        if not _is_manager(user_id):
            _reply(reply_token, "🔒 Only the manager can activate/deactivate items.")
            return
        action_str, item_name = data.split("|", 1)
        new_status = "active" if action_str == "activate" else "inactive"
        user_name  = _get_display_name(user_id)
        try:
            msg = _set_item_status(item_name, new_status)
            _log(item_name, new_status.upper(), 0, user_id, user_name)
            # Refresh the manage items card
            records = _fetch_items()
            bubble, alt = _manage_items_flex(records)
            _send(reply_token, [
                {"type": "text", "text": msg},
                {"type": "flex", "altText": alt, "contents": bubble},
            ])
        except Exception as e:
            print("TOGGLE STATUS ERROR:", repr(e))
            _reply(reply_token, f"Error updating status: {e}")
        return

    if data in ("confirm_stock_in", "confirm_stock_out", "confirm_add_item"):
        item = pending.get("item")
        qty  = pending.get("qty")
        PENDING.pop(user_id, None)

        if not item:
            _reply(reply_token, "⚠️ Session expired. Please start over.")
            return

        # Fetch display name for audit trail
        user_name = _get_display_name(user_id)

        try:
            if data == "confirm_stock_in":
                msg = cmd_stock_in(item, qty, user_id, user_name)
                # Set awaiting_photo state — stock is already saved
                PENDING[user_id] = {
                    "action":    "awaiting_photo",
                    "item":      item,
                    "qty":       qty,
                    "user_id":   user_id,
                    "user_name": user_name,
                }
                # Reply with success + photo prompt in one shot
                _send(reply_token, [
                    {"type": "text", "text": msg},
                    {
                        "type": "text",
                        "text": "📸 Upload a photo as proof\n(or tap Skip to finish)",
                        "quickReply": {"items": [
                            {"type": "action", "action": {
                                "type": "message", "label": "Skip 📷", "text": "skip"}}
                        ]},
                    },
                ])

            elif data == "confirm_stock_out":
                msg = cmd_stock_out(item, qty, user_id, user_name)
                _reply(reply_token, msg)

            else:  # confirm_add_item
                msg = cmd_add_item(item, user_id, user_name)
                _reply(reply_token, msg)

        except Exception as e:
            print("CONFIRM ERROR TYPE:", type(e).__name__)
            print("CONFIRM ERROR DETAIL:", repr(e))
            err = str(e)
            if "<Response" in err:
                err = "Google Sheets error. Please try again."
            _reply(reply_token, f"Error: {err}")


def _handle_photo(user_id: str, reply_token: str, message_id: str):
    """Called when user sends an image while in awaiting_photo state."""
    pending   = PENDING.pop(user_id, {})
    item      = pending.get("item", "unknown")
    user_name = pending.get("user_name", user_id)

    try:
        image_bytes = _download_line_image(message_id)
        filename    = f"stock_in_{item}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        photo_link  = _upload_to_drive(image_bytes, filename)

        # Log photo reference separately
        _log(item, "PHOTO", 0, user_id, user_name, photo_link)

        _reply(reply_token,
               f"📸 Photo saved!\n\n✅ Stock IN for '{item}' is complete.\n{photo_link}")

    except Exception as e:
        print("PHOTO UPLOAD ERROR TYPE:", type(e).__name__)
        print("PHOTO UPLOAD ERROR DETAIL:", repr(e))
        _reply(reply_token,
               "⚠️ Photo could not be saved, but stock IN was already recorded.\n✅ Done.")


# ── Signature verification ────────────────────────────────────────────────────

def _verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


# ── FastAPI routes ────────────────────────────────────────────────────────────

@app.get("/")
async def health():
    return {"status": "Sylvester is running 🐱"}


@app.post("/webhook")
async def webhook(request: Request):
    body      = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not _verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()

    for event in payload.get("events", []):
        event_type  = event.get("type")
        reply_token = event.get("replyToken", "")
        user_id     = event.get("source", {}).get("userId", "unknown")

        # ── Postback (Confirm / Cancel / Register / Approve) ─────────────────
        if event_type == "postback":
            try:
                _handle_postback(event)
            except Exception as e:
                print("POSTBACK ERROR:", repr(e))
            continue

        # ── Follow (user adds bot) ────────────────────────────────────────────
        if event_type == "follow":
            try:
                display_name = _get_display_name(user_id)
                existing     = _get_user_record(user_id)
                if not existing:
                    if _is_manager(user_id):
                        _register_user(user_id, display_name, "manager")
                        _update_user_status(user_id, "approved")
                        _add_to_team(user_id, display_name, "manager")
                        _reply(reply_token,
                               f"✅ Welcome, {display_name}!\n"
                               f"Auto-approved as Manager.\nSend 'menu' to get started.")
                    else:
                        bubble, alt = _welcome_flex(display_name)
                        _reply_flex(reply_token, bubble, alt)
                elif existing.get("status") == "approved":
                    _reply_flex(reply_token, _menu_flex_bubble(),
                                "📦 Sylvester Warehouse Menu")
                else:
                    _reply(reply_token,
                           f"⏳ Welcome back, {display_name}!\n"
                           f"Your registration is {existing.get('status')}.")
            except Exception as e:
                print("FOLLOW ERROR:", repr(e))
            continue

        if event_type != "message":
            continue

        msg_type = event["message"].get("type")

        # ── Image (photo proof for stock in) ─────────────────────────────────
        if msg_type == "image":
            if PENDING.get(user_id, {}).get("action") == "awaiting_photo":
                try:
                    _handle_photo(user_id, reply_token, event["message"]["id"])
                except Exception as e:
                    print("PHOTO ERROR:", repr(e))
            continue

        if msg_type != "text":
            continue

        user_text = event["message"]["text"].strip()
        t         = user_text.lower()

        try:
            # 0. Registration gate — skip for manager
            if not _is_manager(user_id):
                user_record = _get_user_record(user_id)
                if not user_record:
                    display_name = _get_display_name(user_id)
                    bubble, alt  = _welcome_flex(display_name)
                    _reply_flex(reply_token, bubble, alt)
                    continue
                status = user_record.get("status", "pending")
                if status == "pending":
                    _reply(reply_token,
                           "⏳ Your registration is pending manager approval.\n"
                           "You'll be notified once approved.")
                    continue
                if status == "rejected":
                    _reply(reply_token,
                           "❌ Your registration was rejected.\n"
                           "Please contact the manager.")
                    continue
                # status == "approved" → fall through

            # 1. Active guided flow (includes awaiting_photo "skip" handling)
            if user_id in PENDING:
                _handle_pending(user_id, reply_token, user_text)
                continue

            # 2. Menu
            if t in ("menu", "hi", "start"):
                try:
                    _reply_flex(reply_token, _menu_flex_bubble(), "📦 Sylvester Warehouse Menu")
                except Exception as e:
                    print("FLEX MENU ERROR:", repr(e))
                    _reply(reply_token, MENU_TEXT)
                continue

            # 3. Stock Balance
            if t == "stock balance":
                records = _fetch_items()
                _reply_flex(reply_token, _balance_flex_bubble(records), "📦 Stock Balance")
                continue

            # 4. Items list
            if t == "items":
                records = _fetch_items()
                _reply_flex(reply_token, _items_flex_bubble(records), "📋 Item List")
                continue

            # 5. Team list
            if t == "team":
                members = _get_team_members()
                bubble, alt = _team_flex(members)
                _reply_flex(reply_token, bubble, alt)
                continue

            # 6. Manage items (manager only)
            if t == "manage items":
                if not _is_manager(user_id):
                    _reply(reply_token, "🔒 Only the manager can manage items.")
                    continue
                records = _fetch_items()
                bubble, alt = _manage_items_flex(records)
                _reply_flex(reply_token, bubble, alt)
                continue

            # 6. Bare button taps → start guided flow
            if t == "stock in":
                _start_flow(user_id, "stock_in", reply_token)
                continue
            if t == "stock out":
                _start_flow(user_id, "stock_out", reply_token)
                continue
            if t == "add item":
                _start_flow(user_id, "add_item", reply_token)
                continue

            # 6. Full typed commands (backup)
            msg = handle_message(user_text)
            _reply(reply_token, msg if msg else "❓ Unknown command.\nSend 'menu' to see options.")

        except Exception as e:
            print("ERROR TYPE:", type(e).__name__)
            print("ERROR DETAIL:", repr(e))
            err = str(e)
            if "<Response" in err:
                err = "Google Sheets error. Please try again."
            _reply(reply_token, f"Error: {err}")

    return {"status": "ok"}
