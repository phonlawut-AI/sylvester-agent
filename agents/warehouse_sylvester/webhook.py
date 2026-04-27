from flask import Flask, request, jsonify
from handler import handle_event

app = Flask(__name__)

@app.route("/webhook/sylvester", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    return jsonify(handle_event(data))

if __name__ == "__main__":
    app.run(port=5000)