import os
import hmac
import hashlib
import base64
import json
import httpx
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
import gspread
from google.oauth2.service_account import Credentials

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── Google Sheets helpers ────────────────────────────────────────────────────

def _gs_client() -> gspread.Client:
    creds_dict = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def _spreadsheet() -> gspread.Spreadsheet:
    return _gs_client().open("Sylvester Inventory")


def _stock_ws() -> gspread.Worksheet:
    return _spreadsheet().worksheet("stock")


def _movements_ws() -> gspread.Worksheet:
    return _spreadsheet().worksheet("movements")


# ── Menu ─────────────────────────────────────────────────────────────────────

MENU_TEXT = """📦 Sylvester Warehouse Menu
─────────────────────────
1. stock balance
2. add item <name>
3. stock in <item> <qty>
4. stock out <item> <qty>
5. items"""


# ── Command handlers ─────────────────────────────────────────────────────────

def cmd_add_item(name: str) -> str:
    ws = _stock_ws()
    records = ws.get_all_records()
    for r in records:
        if str(r.get("name", "")).lower() == name.lower():
            return f"'{name}' already exists (status: {r.get('status', 'active')})."
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws.append_row([name, 0, "active", now])
    return f"✅ Item added: {name}\nStatus: active\nBalance: 0"


def cmd_items() -> str:
    records = _stock_ws().get_all_records()
    if not records:
        return "No items yet.\nUse: add item <name>"
    lines = ["📋 Item List\n─────────────────────────"]
    for r in records:
        icon = "✅" if r.get("status") == "active" else "🔴"
        lines.append(f"{icon} {r['name']} ({r.get('status', 'active')})")
    return "\n".join(lines)


def cmd_stock_in(item: str, qty: int) -> str:
    ws = _stock_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):  # row 1 is header
        if str(r.get("name", "")).lower() == item.lower():
            if r.get("status") != "active":
                return f"❌ Item '{item}' is inactive."
            new_qty = int(r.get("qty", 0)) + qty
            ws.update_cell(i, 2, new_qty)  # col 2 = qty
            _log_movement(item, "IN", qty, new_qty)
            return f"✅ Stock IN recorded\nItem: {item}\nQty: +{qty}\nBalance: {new_qty}"
    return f"❌ Item '{item}' not found.\nUse: add item {item}"


def cmd_stock_out(item: str, qty: int) -> str:
    ws = _stock_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("name", "")).lower() == item.lower():
            if r.get("status") != "active":
                return f"❌ Item '{item}' is inactive."
            current = int(r.get("qty", 0))
            if qty > current:
                return f"❌ Insufficient stock.\n'{item}' has only {current} in stock."
            new_qty = current - qty
            ws.update_cell(i, 2, new_qty)
            _log_movement(item, "OUT", qty, new_qty)
            return f"✅ Stock OUT recorded\nItem: {item}\nQty: -{qty}\nBalance: {new_qty}"
    return f"❌ Item '{item}' not found.\nUse: add item {item}"


def cmd_stock_balance() -> str:
    records = _stock_ws().get_all_records()
    if not records:
        return "No items yet.\nUse: add item <name>"
    lines = ["📦 Stock Balance\n─────────────────────────"]
    for r in records:
        lines.append(f"• {r['name']}: {r.get('qty', 0)}")
    return "\n".join(lines)


def _log_movement(item: str, move_type: str, qty: int, balance_after: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    _movements_ws().append_row([now, item, move_type, qty, balance_after])


# ── Command router ────────────────────────────────────────────────────────────

def handle_message(text: str) -> str:
    t = text.strip().lower()

    if t in ("hi", "menu", "start"):
        return MENU_TEXT

    if t == "stock balance":
        return cmd_stock_balance()

    if t == "items":
        return cmd_items()

    if t.startswith("add item "):
        name = text.strip()[9:].strip()
        if not name:
            return "Usage: add item <name>"
        return cmd_add_item(name)

    if t.startswith("stock in "):
        parts = text.strip().split()
        if len(parts) < 4:
            return "Usage: stock in <item> <qty>"
        try:
            qty = int(parts[-1])
            item = " ".join(parts[2:-1])
            return cmd_stock_in(item, qty)
        except ValueError:
            return "Quantity must be a number.\nUsage: stock in <item> <qty>"

    if t.startswith("stock out "):
        parts = text.strip().split()
        if len(parts) < 4:
            return "Usage: stock out <item> <qty>"
        try:
            qty = int(parts[-1])
            item = " ".join(parts[2:-1])
            return cmd_stock_out(item, qty)
        except ValueError:
            return "Quantity must be a number.\nUsage: stock out <item> <qty>"

    return "❓ Unknown command.\nSend 'menu' to see available commands."


# ── LINE webhook ──────────────────────────────────────────────────────────────

def _verify_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def _reply(reply_token: str, text: str):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    async with httpx.AsyncClient() as client:
        await client.post(LINE_REPLY_URL, json=payload, headers=headers)


@app.get("/")
async def health():
    return {"status": "Sylvester is running 🐱"}


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not _verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = await request.json()

    for event in payload.get("events", []):
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_text = event["message"]["text"].strip()
        reply_token = event["replyToken"]

        try:
            reply_text = handle_message(user_text)
        except Exception as e:
            reply_text = f"Sorry, something went wrong.\nError: {e}"

        await _reply(reply_token, reply_text)

    return {"status": "ok"}
