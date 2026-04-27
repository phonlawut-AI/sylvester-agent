from inventory import get_inventory, set_inventory

def add_item(payload):
    item_id = payload.get("id")
    if not item_id:
        return {"status": "error", "message": "Missing id"}

    data = get_inventory()
    if item_id in data:
        return {"status": "error", "message": "Already exists"}

    data[item_id] = payload
    set_inventory(data)
    return {"status": "ok"}

def remove_item(payload):
    item_id = payload.get("id")
    data = get_inventory()

    if item_id not in data:
        return {"status": "error", "message": "Not found"}

    data.pop(item_id)
    set_inventory(data)
    return {"status": "ok"}

def update_item(payload):
    item_id = payload.get("id")
    data = get_inventory()

    if item_id not in data:
        return {"status": "error", "message": "Not found"}

    data[item_id].update(payload)
    set_inventory(data)
    return {"status": "ok"}