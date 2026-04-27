from fastapi import FastAPI, Request, HTTPException
import os
import hmac
import hashlib
import base64
import json
import requests

app = FastAPI()

CHANNEL_SECRET = os.getenv("SYLVESTER_CHANNEL_SECRET")
CHANNEL_ACCESS_TOKEN = os.getenv("SYLVESTER_CHANNEL_ACCESS_TOKEN")

# ✅ health check
@app.get("/")
async def health():
    return {"status": "Sylvester is running 🐱"}

# ✅ verify LINE signature
def verify_signature(body, signature):
    hash = hmac.new(
        CHANNEL_SECRET.encode('utf-8'),
        body,
        hashlib.sha256
    ).digest()
    expected = base64.b64encode(hash).decode()
    return hmac.compare_digest(expected, signature)

# ✅ webhook
@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = json.loads(body)

    for event in data.get("events", []):
        if event["type"] != "message":
            continue
        if event["message"]["type"] != "text":
            continue

        reply_token = event["replyToken"]

        reply(reply_token, "hi from Sylvester 🐱")

    return {"status": "ok"}

# ✅ reply function
def reply(reply_token, text):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text}]
    }
    requests.post(url, headers=headers, json=body)
