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
PENDING: dict[str, dict] = {}

# ── Google Sheets ─────────────────────────────────────────────────────────────

def _client() -> gspread.Client:
    creds_dict = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    return gspread.service_account_from_dict(creds_dict)

def _stock_ws() -> gspread.Worksheet:
    return _client().open("Sylvester Inventory").worksheet("stock")

def _movements_ws() -> gspread.Worksheet:
    return _client().open("Sylvester Inventory").worksheet("movements")


# ── Google Sheets command handlers ────────────────────────────────────────────

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
            return f"✅ Stock IN recorded\nItem: {item}\nAdded: +{qty}\nNew balance: {new_qty}"
    return f"❌ '{item}' not found.\nAdd it first with: add item {item}"

def cmd_stock_out(item: str, qty: int) -> str:
    ws = _stock_ws()
    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
        if str(r.get("item", "")).lower() == item.lower():
            current = int(r.get("quantity", 0))
            if qty > current:
                return f"❌ Insufficient stock.\n'{item}' only has {current} remaining."
            new_qty = current - qty
            ws.update_cell(i, 2, new_qty)
            _log(item, "out", qty)
            return f"✅ Stock OUT recorded\nItem: {item}\nRemoved: -{qty}\nNew balance: {new_qty}"
    return f"❌ '{item}' not found.\nAdd it first with: add item {item}"

def _log(item: str, move_type: str, qty: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    _movements_ws().append_row([now, item, move_type, qty])

def _fetch_items() -> list[dict]:
    try:
        return _stock_ws().get_all_records()
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
    return None  # Not a typed command — caller handles


# ── LINE send helpers ─────────────────────────────────────────────────────────

def _send(reply_token: str, messages: list[dict]) -> bool:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {"replyToken": reply_token, "messages": messages}
    response = requests.post(LINE_REPLY_URL, headers=headers, json=payload)
    print("LINE reply status:", response.status_code, response.text)
    if response.status_code >= 400:
        raise Exception(f"LINE API error: {response.status_code} {response.text}")
    return True

def _reply(reply_token: str, text: str) -> bool:
    return _send(reply_token, [{"type": "text", "text": text[:5000]}])

def _reply_flex(reply_token: str, bubble: dict, alt_text: str) -> bool:
    return _send(reply_token, [{"type": "flex", "altText": alt_text, "contents": bubble}])

def _reply_with_quick_reply(reply_token: str, text: str, chips: list[str]) -> bool:
    """Text prompt with tappable Quick Reply chips (max 13, label max 20 chars)."""
    items = [
        {"type": "action", "action": {"type": "message",
                                      "label": c[:20], "text": c}}
        for c in chips[:13]
    ]
    return _send(reply_token, [{
        "type": "text",
        "text": text,
        "quickReply": {"items": items},
    }])


# ── Flex bubble builders ──────────────────────────────────────────────────────

MENU_TEXT = "Send 'menu' to see available commands."

def _menu_flex_bubble() -> dict:
    def _btn(label: str, text: str, color: str) -> dict:
        return {
            "type": "button", "style": "primary", "color": color, "height": "sm",
            "action": {"type": "message", "label": label, "text": text},
        }
    return {
        "type": "bubble",
        "size": "kilo",
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
                _btn("📊 Stock Balance", "stock balance", "#1565C0"),
                _btn("📋 Items List",    "items",         "#00695C"),
                _btn("➕ Stock In",      "stock in",      "#2E7D32"),
                _btn("➖ Stock Out",     "stock out",     "#C62828"),
                _btn("🆕 Add New Item", "add item",      "#6A1B9A"),
            ],
        },
    }


def _balance_flex_bubble(records: list[dict]) -> dict:
    if not records:
        rows = [{"type": "text", "text": "No items yet.", "color": "#888888",
                 "size": "sm", "align": "center"}]
    else:
        rows = []
        for r in records:
            qty = int(r.get("quantity", 0))
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
            rows.pop()  # remove trailing separator

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
        "body": {
            "type": "box", "layout": "vertical", "spacing": "none",
            "paddingAll": "14px", "contents": rows,
        },
    }


def _items_flex_bubble(records: list[dict]) -> dict:
    if not records:
        rows = [{"type": "text", "text": "No items yet.", "color": "#888888",
                 "size": "sm", "align": "center"}]
    else:
        rows = []
        for r in records:
            qty = int(r.get("quantity", 0))
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
                                "size": "xs", "color": "#FFFFFF",
                                "align": "center",
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
        "body": {
            "type": "box", "layout": "vertical", "spacing": "none",
            "paddingAll": "14px", "contents": rows,
        },
    }


def _confirm_flex_bubble(action: str, item: str, qty: int | None) -> dict:
    titles  = {"stock_in": "➕ Stock In", "stock_out": "➖ Stock Out",
               "add_item": "🆕 Add Item"}
    colors  = {"stock_in": "#1B5E20", "stock_out": "#B71C1C",
               "add_item": "#4A148C"}
    hdr_col = colors[action]

    if action == "add_item":
        body_rows = [
            {"type": "text", "text": "New item to add:", "size": "xs",
             "color": "#888888"},
            {"type": "text", "text": item, "size": "xl",
             "weight": "bold", "color": "#333333", "margin": "sm"},
        ]
        alt = f"Confirm add item: {item}?"
    else:
        symbol = "+" if action == "stock_in" else "−"
        body_rows = [
            {"type": "box", "layout": "horizontal",
             "contents": [
                 {"type": "text", "text": "Item", "size": "xs",
                  "color": "#888888", "flex": 2},
                 {"type": "text", "text": item, "size": "sm",
                  "weight": "bold", "color": "#333333", "flex": 3},
             ]},
            {"type": "separator", "margin": "sm"},
            {"type": "box", "layout": "horizontal", "margin": "sm",
             "contents": [
                 {"type": "text", "text": "Qty", "size": "xs",
                  "color": "#888888", "flex": 2},
                 {"type": "text", "text": f"{symbol}{qty}",
                  "size": "sm", "weight": "bold", "color": hdr_col, "flex": 3},
             ]},
        ]
        alt = f"Confirm {action.replace('_',' ')}: {item} {symbol}{qty}?"

    return {
        "type": "bubble",
        "size": "kilo",
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
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "16px", "spacing": "sm",
            "contents": body_rows,
        },
        "footer": {
            "type": "box", "layout": "horizontal",
            "spacing": "sm", "paddingAll": "12px",
            "contents": [
                {
                    "type": "button", "style": "primary",
                    "color": "#1B5E20", "height": "sm", "flex": 1,
                    "action": {"type": "postback", "label": "✅ Confirm",
                               "data": f"confirm_{action}"},
                },
                {
                    "type": "button", "style": "primary",
                    "color": "#B71C1C", "height": "sm", "flex": 1,
                    "action": {"type": "postback", "label": "❌ Cancel",
                               "data": "cancel"},
                },
            ],
        },
    }, alt


# ── Step-by-step state machine ────────────────────────────────────────────────

def _start_flow(user_id: str, action: str, reply_token: str):
    PENDING[user_id] = {"action": action, "item": None, "qty": None}

    if action in ("stock_in", "stock_out"):
        records = _fetch_items()
        item_names = [r["item"] for r in records if r.get("item")]
        prompt = ("➕ Select item to add stock:" if action == "stock_in"
                  else "➖ Select item to remove stock:")
        if item_names:
            _reply_with_quick_reply(reply_token, prompt, item_names)
        else:
            _reply(reply_token, f"{prompt}\n\n(No items yet — type a name or add items first)")
    else:
        _reply(reply_token, "🆕 Add Item\n\nEnter the new item name:")


def _handle_pending(user_id: str, reply_token: str, user_text: str):
    pending = PENDING[user_id]
    action  = pending["action"]
    t       = user_text.strip().lower()

    # Escape hatch at any step
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

    # Step 1 — waiting for item name
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

    # Step 2 — waiting for quantity (stock_in / stock_out only)
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

    if data in ("confirm_stock_in", "confirm_stock_out", "confirm_add_item"):
        item = pending.get("item")
        qty  = pending.get("qty")
        PENDING.pop(user_id, None)

        if not item:
            _reply(reply_token, "⚠️ Session expired. Please start over.")
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

        # ── Postback: Confirm / Cancel ────────────────────────────────────────
        if event_type == "postback":
            try:
                _handle_postback(event)
            except Exception as e:
                print("POSTBACK ERROR:", repr(e))
            continue

        if event_type != "message" or event["message"].get("type") != "text":
            continue

        user_text = event["message"]["text"].strip()
        t         = user_text.lower()

        try:
            # 1. Active guided flow
            if user_id in PENDING:
                _handle_pending(user_id, reply_token, user_text)
                continue

            # 2. Menu
            if t in ("menu", "hi", "start"):
                try:
                    _reply_flex(reply_token, _menu_flex_bubble(),
                                "📦 Sylvester Warehouse Menu")
                except Exception as e:
                    print("FLEX MENU ERROR:", repr(e))
                    _reply(reply_token, MENU_TEXT)
                continue

            # 3. Stock Balance → Flex card
            if t == "stock balance":
                records = _fetch_items()
                _reply_flex(reply_token, _balance_flex_bubble(records),
                            "📦 Stock Balance")
                continue

            # 4. Items list → Flex card
            if t == "items":
                records = _fetch_items()
                _reply_flex(reply_token, _items_flex_bubble(records),
                            "📋 Item List")
                continue

            # 5. Bare action buttons → start guided flow
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
            if msg:
                _reply(reply_token, msg)
            else:
                _reply(reply_token,
                       "❓ Unknown command.\nSend 'menu' to see options.")

        except Exception as e:
            print("ERROR TYPE:", type(e).__name__)
            print("ERROR DETAIL:", repr(e))
            err = str(e)
            if "<Response" in err:
                err = "Google Sheets error. Please try again."
            _reply(reply_token, f"Error: {err}")

    return {"status": "ok"}
