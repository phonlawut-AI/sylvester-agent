import os
import hmac
import hashlib
import base64
import json
import httpx
from pathlib import Path
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"

# ── JSON data files ──────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data" / "warehouse_inventory"
CATALOG_FILE = DATA_DIR / "item_catalog.json"
BALANCE_FILE = DATA_DIR / "stock_balance.json"
MOVEMENTS_FILE = DATA_DIR / "stock_movements.json"


def _init_data():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for f, default in [
        (CATALOG_FILE, {}),
        (BALANCE_FILE, {}),
        (MOVEMENTS_FILE, []),
    ]:
        if not f.exists():
            f.write_text(json.dumps(default, indent=2))


def _load(path: Path):
    return json.loads(path.read_text())


def _save(path: Path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


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
    _init_data()
    catalog = _load(CATALOG_FILE)
    if name in catalog:
        return f"'{name}' already exists (status: {catalog[name]['status']})."
    catalog[name] = {"status": "active", "added": datetime.now().strftime("%Y-%m-%d %H:%M")}
    _save(CATALOG_FILE, catalog)
    balance = _load(BALANCE_FILE)
    if name not in balance:
        balance[name] = 0
        _save(BALANCE_FILE, balance)
    return f"✅ Item added: {name}\nStatus: active\nBalance: 0"


def cmd_items() -> str:
    _init_data()
    catalog = _load(CATALOG_FILE)
    if not catalog:
        return "No items yet.\nUse: add item <name>"
    lines = ["📋 Item List\n─────────────────────────"]
    for name, info in catalog.items():
        icon = "✅" if info["status"] == "active" else "🔴"
        lines.append(f"{icon} {name} ({info['status']})")
    return "\n".join(lines)


def cmd_stock_in(item: str, qty: int) -> str:
    _init_data()
    catalog = _load(CATALOG_FILE)
    if item not in catalog:
        return f"❌ Item '{item}' not found.\nUse: add item {item}"
    if catalog[item]["status"] != "active":
        return f"❌ Item '{item}' is inactive."
    balance = _load(BALANCE_FILE)
    balance[item] = balance.get(item, 0) + qty
    _save(BALANCE_FILE, balance)
    movements = _load(MOVEMENTS_FILE)
    movements.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "item": item, "type": "IN", "qty": qty, "balance_after": balance[item]
    })
    _save(MOVEMENTS_FILE, movements)
    return f"✅ Stock IN recorded\nItem: {item}\nQty: +{qty}\nBalance: {balance[item]}"


def cmd_stock_out(item: str, qty: int) -> str:
    _init_data()
    catalog = _load(CATALOG_FILE)
    if item not in catalog:
        return f"❌ Item '{item}' not found.\nUse: add item {item}"
    if catalog[item]["status"] != "active":
        return f"❌ Item '{item}' is inactive."
    balance = _load(BALANCE_FILE)
    current = balance.get(item, 0)
    if qty > current:
        return f"❌ Insufficient stock.\n'{item}' has only {current} in stock."
    balance[item] = current - qty
    _save(BALANCE_FILE, balance)
    movements = _load(MOVEMENTS_FILE)
    movements.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "item": item, "type": "OUT", "qty": qty, "balance_after": balance[item]
    })
    _save(MOVEMENTS_FILE, movements)
    return f"✅ Stock OUT recorded\nItem: {item}\nQty: -{qty}\nBalance: {balance[item]}"


def cmd_stock_balance() -> str:
    _init_data()
    balance = _load(BALANCE_FILE)
    if not balance:
        return "No items yet.\nUse: add item <name>"
    lines = ["📦 Stock Balance\n─────────────────────────"]
    for item, qty in balance.items():
        lines.append(f"• {item}: {qty}")
    return "\n".join(lines)


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
