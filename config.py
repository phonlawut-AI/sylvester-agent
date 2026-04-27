import os
from dotenv import load_dotenv

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

LINE_WAREHOUSE_TEAM = os.getenv("LINE_WAREHOUSE_TEAM")
LINE_SUPERVISOR_ID = os.getenv("LINE_SUPERVISOR_ID")
LINE_MGMT_GROUP_ID = os.getenv("LINE_MGMT_GROUP_ID")

STAFF_LINE_IDS: dict[str, str] = {
    key.replace("LINE_STAFF_", "").lower(): val
    for key, val in os.environ.items()
    if key.startswith("LINE_STAFF_") and val
}

GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_SHEETS_CREDENTIALS = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "./credentials.json")
GOOGLE_DRIVE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_FOLDER_ID")

PLAN_PDF_FOLDER = os.getenv("PLAN_PDF_FOLDER", "./data/plans/")
TIMEZONE = os.getenv("TIMEZONE", "Australia/Melbourne")

MANAGER_LINE_ID = os.getenv("MANAGER_LINE_ID", "")

MAINTENANCE_PHOTO_TIMEOUT_MINUTES = int(os.getenv("MAINTENANCE_PHOTO_TIMEOUT_MINUTES", "30"))
LATE_CONFIRMATION_ALERT_TIME = os.getenv("LATE_CONFIRMATION_ALERT_TIME", "12:00")
