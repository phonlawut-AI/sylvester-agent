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

# ── In-memory state per LINE user ─────────────────────────────────────────────
# { user_id: {"action": "stock_in"|"stock_out"|"add_item", "item": str|None, "qty": int|None} }
PENDING: dict[str, dict] = {}

# ── Google Sheets ─────────────────────────────────────────────────────────────

def _client() -> gspread.Client:
    creds_dict = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    return gspread.service_account_from_dict(creds_dict)


def _stock_ws() -> gspread.Worksheet:
    return _client().open("Sylvester Inventory").worksheet("stock")


def _movements_ws() -> gspread.Worksheet:
    return _client().open("Sylvester Inventory").worksheet("movements")


# ── Command handlers (Google Sheets — unchanged) ──────────────────────────────

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
    for i, r in enumerate(records, start=2):
        if str(r.get("item", "")).lower() == item.lower():
            new_qty = int(r.get("quantity", 0)) + qty
            ws.update_cell(i, 2, new_qty)
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


# ── Fallback text command router (backup for full typed commands) ──────────────

def handle_message(text: str) -> str:
    t = text.strip().lower()

    if t == "stock balance":
        return cmd_stock_balance()
    if t == "items":
        return cmd_items()

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
            return "Quantity must be a number.\nUsage: stock in <item> <qty>"

    if t.startswith("stock out "):
        parts = text.strip().split()
        if len(parts) < 4:
            return "Usage: stock out <item> <qty>"
        try:
            return cmd_stock_out(" ".join(parts[2:-1]), int(parts[-1]))
        except ValueError:
            return "Quantity must be a number.\nUsage: stock out <item> <qty>"

    return "❓ Unknown command.\nSend 'menu' to see available commands."


# ── LINE reply helpers ────────────────────────────────────────────────────────

def _reply(reply_token: str, text: str) -> bool:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:5000]}],
    }
    response = requests.post(LINE_REPLY_URL, headers=headers, json=payload)
    print("LINE reply status:", response.status_code, response.text)
    if response.status_code >= 400:
        raise Exception(f"LINE API error: {response.status_code} {response.text}")
    return True


def _reply_flex(reply_token: str, bubble: dict, alt_text: str) -> bool:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{"type": "flex", "altText": alt_text, "contents": bubble}],
    }
    response = requests.post(LINE_REPLY_URL, headers=headers, json=payload)
    print("LINE flex reply status:", response.status_code, response.text)
    if response.status_code >= 400:
        raise Exception(f"LINE API error: {response.status_code} {response.text}")
    return True


# ── Flex bubbles ──────────────────────────────────────────────────────────────

MENU_TEXT = """📦 Sylvester Warehouse Menu
─────────────────────────
1. stock balance
2. add item <name>
3. stock in <item> <qty>
4. stock out <item> <qty>
5. items"""


def _menu_flex_bubble() -> dict:
    def _btn(label: str, text: str, color: str) -> dict:
        return {
            "type": "button",
            "style": "primary",
            "color": color,
            "height": "sm",
            "action": {"type": "message", "label": label, "text": text},
        }

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#1A237E",
            "paddingAll": "16px",
            "contents": [
                {"type": "text", "text": "📦 Sylvester", "color": "#FFFFFF",
                 "weight": "bold", "size": "xl"},
                {"type": "text", "text": "Warehouse Assistant", "color": "#9FA8DA",
                 "size": "sm", "margin": "xs"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "14px",
            "contents": [
                _btn("📊 Stock Balance", "stock balance", "#1565C0"),
                _btn("📋 Items",         "items",         "#00695C"),
                _btn("➕ Stock In",      "stock in",      "#2E7D32"),
                _btn("➖ Stock Out",     "stock out",     "#C62828"),
                _btn("🆕 Add Item",     "add item",      "#6A1B9A"),
            ],
        },
    }


def _confirm_flex_bubble(action: str, item: str, qty: int | None) -> dict:
    action_label = {"stock_in": "Stock In", "stock_out": "Stock Out",
                    "add_item": "Add Item"}[action]

    if action == "add_item":
        detail_lines = [
            {"type": "text", "text": f"Add item: {item}", "size": "md",
             "weight": "bold", "wrap": True},
        ]
    else:
        symbol = "+" if action == "stock_in" else "-"
        detail_lines = [
            {"type": "text", "text": action_label, "size": "md",
             "weight": "bold"},
            {"type": "text", "text": f"Item: {item}", "size": "sm",
             "margin": "sm"},
            {"type": "text", "text": f"Qty:  {symbol}{qty}", "size": "sm"},
        ]

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#37474F",
            "paddingAll": "14px",
            "contents": [
                {"type": "text", "text": "Confirm?", "color": "#FFFFFF",
                 "weight": "bold", "size": "lg"},
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "16px",
            "spacing": "sm",
            "contents": detail_lines,
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
                    "color": "#1B5E20",
                    "height": "sm",
                    "flex": 1,
                    "action": {"type": "postback", "label": "✅ Confirm",
                               "data": f"confirm_{action}"},
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#B71C1C",
                    "height": "sm",
                    "flex": 1,
                    "action": {"type": "postback", "label": "❌ Cancel",
                               "data": "cancel"},
                },
            ],
        },
    }


# ── Step-by-step state machine ────────────────────────────────────────────────

def _start_flow(user_id: str, action: str, reply_token: str):
    PENDING[user_id] = {"action": action, "item": None, "qty": None}
    prompts = {
        "stock_in":  "➕ Stock In\nEnter item name:",
        "stock_out": "➖ Stock Out\nEnter item name:",
        "add_item":  "🆕 Add Item\nEnter item name:",
    }
    _reply(reply_token, prompts[action])


def _handle_pending(user_id: str, reply_token: str, user_text: str):
    pending = PENDING[user_id]
    action  = pending["action"]
    t       = user_text.strip().lower()

    # Allow user to escape at any step
    if t in ("cancel", "menu", "hi", "start"):
        PENDING.pop(user_id, None)
        if t in ("menu", "hi", "start"):
            try:
                _reply_flex(reply_token, _menu_flex_bubble(), "📦 Sylvester Warehouse Menu")
            except Exception:
                _reply(reply_token, MENU_TEXT)
        else:
            _reply(reply_token, "Cancelled. Send 'menu' to start over.")
        return

    # Step 1 — waiting for item name
    if pending["item"] is None:
        item = user_text.strip()
        if not item:
            _reply(reply_token, "Please enter a valid item name:")
            return
        PENDING[user_id]["item"] = item
        if action == "add_item":
            # No qty needed — go straight to confirmation
            _reply_flex(reply_token,
                        _confirm_flex_bubble(action, item, None),
                        f"Confirm add item: {item}?")
        else:
            _reply(reply_token, "Enter quantity:")
        return

    # Step 2 — waiting for quantity (stock_in / stock_out only)
    if pending["qty"] is None:
        try:
            qty = int(user_text.strip())
            if qty <= 0:
                raise ValueError
        except ValueError:
            _reply(reply_token, "Please enter a valid quantity (number > 0):")
            return
        PENDING[user_id]["qty"] = qty
        item = pending["item"]
        _reply_flex(reply_token,
                    _confirm_flex_bubble(action, item, qty),
                    f"Confirm {action.replace('_', ' ')}: {item} {qty}?")


def _handle_postback(event: dict):
    data        = event["postback"]["data"]
    reply_token = event.get("replyToken", "")
    user_id     = event.get("source", {}).get("userId", "unknown")
    pending     = PENDING.get(user_id, {})

    if data == "cancel":
        PENDING.pop(user_id, None)
        _reply(reply_token, "Cancelled. Send 'menu' to start over.")
        return

    if data in ("confirm_stock_in", "confirm_stock_out", "confirm_add_item"):
        item = pending.get("item")
        qty  = pending.get("qty")
        PENDING.pop(user_id, None)

        if not item:
            _reply(reply_token, "Error: session expired. Please start over.")
            return

        try:
            if data == "confirm_stock_in":
                msg = cmd_stock_in(item, qty)
            elif data == "confirm_stock_out":
                msg = cmd_stock_out(item, qty)
            else:
                msg = cmd_add_item(item)
            _reply(reply_token, msg)
        except Exception as e:
            print("CONFIRM ERROR TYPE:", type(e).__name__)
            print("CONFIRM ERROR DETAIL:", repr(e))
            err = str(e)
            if "<Response" in err:
                err = "Google Sheets error. Please try again."
            _reply(reply_token, f"Error: {err}")


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

        # ── Postback (Confirm / Cancel buttons) ───────────────────────────────
        if event_type == "postback":
            try:
                _handle_postback(event)
            except Exception as e:
                print("POSTBACK ERROR:", repr(e))
            continue

        # ── Text messages ─────────────────────────────────────────────────────
        if event_type != "message" or event["message"].get("type") != "text":
            continue

        user_text = event["message"]["text"].strip()
        t         = user_text.lower()

        try:
            # 1. If user has an active flow, route through state machine
            if user_id in PENDING:
                _handle_pending(user_id, reply_token, user_text)
                continue

            # 2. Menu trigger → Flex menu
            if t in ("menu", "hi", "start"):
                try:
                    _reply_flex(reply_token, _menu_flex_bubble(),
                                "📦 Sylvester Warehouse Menu")
                except Exception as e:
                    print("FLEX MENU ERROR:", repr(e))
                    _reply(reply_token, MENU_TEXT)
                continue

            # 3. Bare action buttons → start guided flow
            if t == "stock in":
                _start_flow(user_id, "stock_in", reply_token)
                continue

            if t == "stock out":
                _start_flow(user_id, "stock_out", reply_token)
                continue

            if t == "add item":
                _start_flow(user_id, "add_item", reply_token)
                continue

            # 4. Full typed commands (backup)
            message = handle_message(user_text)
            _reply(reply_token, message)

        except Exception as e:
            print("GOOGLE SHEETS ERROR TYPE:", type(e).__name__)
            print("GOOGLE SHEETS ERROR DETAIL:", repr(e))
            err = str(e)
            if "<Response" in err:
                err = "Google Sheets error. Please try again."
            _reply(reply_token, f"Error: {err}")

    return {"status": "ok"}
