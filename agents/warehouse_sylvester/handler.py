from inventory import get_inventory
from item_manager import add_item, remove_item, update_item

def handle_event(data: dict) -> dict:
    action = data.get("action")
    payload = data.get("payload", {})

    if action == "get_inventory":
        return {"status": "ok", "inventory": get_inventory()}
    elif action == "add_item":
        return add_item(payload)
    elif action == "remove_item":
        return remove_item(payload)
    elif action == "update_item":
        return update_item(payload)
    else:
        return {"status": "error", "message": "Unknown action"}