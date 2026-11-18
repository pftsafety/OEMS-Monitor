import os
import re
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import PIL.Image

if not hasattr(PIL.Image, "ANTIALIAS"):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS

import ddddocr
from playwright.sync_api import sync_playwright

# --- URLS & CREDENTIALS FROM GITHUB SECRETS ---
LOGIN_URL = "https://tpro.telsys.in/"
DASHBOARD_URL = "https://tpro.telsys.in/dashboard#"

USERNAME = os.environ.get("TELEPRO_USER", "").strip()
PASSWORD = os.environ.get("TELEPRO_PASS", "").strip()
SESSION_COOKIE = os.environ.get("TELEPRO_COOKIE", "").strip()

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.environ.get("ALERT_EMAIL", "").strip()
SENDER_APP_PASSWORD = os.environ.get("ALERT_EMAIL_PASS", "").strip()
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "").strip()

# --- EFFLUENT THRESHOLD LIMITS ---
LIMITS = {
    "BOD": {"max": 30.0},
    "COD": {"max": 250.0},
    "pH":  {"min": 5.5, "max": 9.0},
    "TSS": {"max": 100.0}
}

MAX_DATA_DELAY_MINUTES = 10
IST = timezone(timedelta(hours=5, minutes=30))


def check_secrets():
    print("--- Checking GitHub Secrets Configuration ---")
    print(f"TELEPRO_USER configured: {'YES' if USERNAME else 'NO (Empty!)'}")
    print(f
