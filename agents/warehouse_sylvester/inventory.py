import json
import os

FILE = "data/warehouse_inventory/inventory.json"

def _load():
    if not os.path.exists(FILE):
        return {}
    with open(FILE, "r") as f:
        return json.load(f)

def _save(data):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    with open(FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_inventory():
    return _load()

def set_inventory(data):
    _save(data)