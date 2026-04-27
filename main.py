import os
import hmac
import hashlib
import base64
import json
import requests
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
import gspread

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# ── Google Sheets ─────────────────────────────────────────────────────────────

def _client() -> gspread.Client:
    creds_dict = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    return gspread.service_account_from_dict(creds_dict)


def _stock_ws() -> gspread.Worksheet:
    return _client().open("Sylvester Inventory").worksheet("stock")


def _movements_ws() -> gspread.Worksheet:
    return _client().open("Sylvester Inventory").worksheet("movements")


# ── Menu ──────────────────────────────────────────────────────────────────────

MENU_TEXT = """📦 Sylvester Warehouse Menu
─────────────────────────
1. stock balance
2. add item <name>
3. stock in <item> <qty>
4. stock out <item> <qty>
5. items"""

# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_stock_balance() -> str:
    records = _stock_ws().get_all_records()
    if not records:
        return "No items yet.\nUse: add item <name>"
    lines = ["📦 Stock Balance\n─────────────────────────"]
    for r in records:
        lines.append(f"• {r['item']}: {r['quantity']}")
    return "\n".join(lines)


def cmd_add_item(name: str) -> str:
    ws = _stock_ws()
    records = ws.get_all_records()
    for r in records:
        if str(r.get("item", "")).lower() == name.lower():
            return f"'{name}' already exists.\nCurrent quantity: {r.get('quantity', 0)}"
    ws.append_row([name, 0])
    return f"✅ Item added: {name}\nQuantity: 0"


def cmd_stock_in(item: str, qty: int) -> str:
    ws = _stock_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):  # row 1 is header
        if str(r.get("item", "")).lower() == item.lower():
            new_qty = int(r.get("quantity", 0)) + qty
            ws.update_cell(i, 2, new_qty)  # col 2 = quantity
            _log(item, "in", qty)
            return f"✅ Stock IN\nItem: {item}\nAdded: +{qty}\nBalance: {new_qty}"
    return f"❌ '{item}' not found.\nUse: add item {item}"


def cmd_stock_out(item: str, qty: int) -> str:
    ws = _stock_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("item", "")).lower() == item.lower():
            current = int(r.get("quantity", 0))
            if qty > current:
                return f"❌ Insufficient stock.\n'{item}' only has {current}."
            new_qty = current - qty
            ws.update_cell(i, 2, new_qty)
            _log(item, "out", qty)
            return f"✅ Stock OUT\nItem: {item}\nRemoved: -{qty}\nBalance: {new_qty}"
    return f"❌ '{item}' not found.\nUse: add item {item}"


def cmd_items() -> str:
    records = _stock_ws().get_all_records()
    if not records:
        return "No items yet.\nUse: add item <name>"
    lines = ["📋 Items\n─────────────────────────"]
    for r in records:
        lines.append(f"• {r['item']}: {r.get('quantity', 0)}")
    return "\n".join(lines)


def _log(item: str, move_type: str, qty: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    _movements_ws().append_row([now, item, move_type, qty])


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


def _reply(reply_token: str, text: str) -> bool:
    url = LINE_REPLY_URL
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    response = requests.post(url, headers=headers, json=payload)
    print("LINE reply status:", response.status_code, response.text)
    if response.status_code >= 400:
        raise Exception(f"LINE API error: {response.status_code} {response.text}")
    return True


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
            message = handle_message(user_text)
            _reply(reply_token, message)
        except Exception as e:
            err = str(e)
            # Sanitize raw HTTP response objects (e.g. gspread APIError)
            if "<Response" in err:
                err = "Google Sheets error. Please try again."
            print("ERROR:", err)
            try:
                _reply(reply_token, f"Error: {err}")
            except Exception:
                pass

    return {"status": "ok"}
