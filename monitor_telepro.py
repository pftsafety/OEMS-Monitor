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


def check_secrets_status():
    print("--- Secret Configuration Check ---")
    print(f"TELEPRO_USER set: {'YES' if USERNAME else 'NO (Missing!)'}")
    print(f"TELEPRO_PASS set: {'YES' if PASSWORD else 'NO (Missing!)'}")
    print(f"TELEPRO_COOKIE set: {'YES' if SESSION_COOKIE else 'NO (Optional)'}")
    print(f"ALERT_EMAIL set: {'YES' if SENDER_EMAIL else 'NO (Missing!)'}")
    print(f"ALERT_EMAIL_PASS set: {'YES' if SENDER_APP_PASSWORD else 'NO (Missing!)'}")
    print(f"RECEIVER_EMAIL set: {'YES' if RECEIVER_EMAIL else 'NO (Missing!)'}")
    print("----------------------------------")


def send_alert(subject, body):
    if not SENDER_EMAIL or not SENDER_APP_PASSWORD or not RECEIVER_EMAIL:
        print("WARNING: Cannot send email because ALERT_EMAIL, ALERT_EMAIL_PASS, or RECEIVER_EMAIL secret is empty!")
        return

    msg = EmailMessage()
    msg.set_content(body)
    msg["Subject"] = f"[URGENT] HLL TelePro ETP Alert: {subject}"
    msg["From"] = SENDER_EMAIL
    msg["To"] = [email.strip() for email in RECEIVER_EMAIL.split(",") if email.strip()]

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_APP_PASSWORD.replace(" ", ""))
            server.send_message(msg)
        print(f"Alert email sent successfully: {subject}")
    except Exception as e:
        print(f"Failed to send email alert: {e}")


def solve_captcha_and_login(page):
    ocr = ddddocr.DdddOcr(show_ad=False)

    for attempt in range(1, 6):
        print(f"\n--- Captcha Login Attempt {attempt}/5 ---")
        try:
            page.goto(LOGIN_URL, timeout=30000)
            page.wait_for_timeout(2500)

            # Fill Username and Password
            page.locator("input[type='text']:visible, input[type='email']:visible").first.fill(USERNAME)
            page.locator("input[type='password']:visible").first.fill(PASSWORD)

            # Screenshot the Django Simple Captcha image
            captcha_el = page.locator("img.captcha, img[src*='captcha' i], form img").first
            img_bytes = captcha_el.screenshot(timeout=5000)
            raw_text = ocr.classification(img_bytes).strip().upper()
            captcha_text = re.sub(r"[^A-Z0-9]", "", raw_text)
            print(f"OCR read Captcha as: '{captcha_text}'")

            # Target ONLY the visible text input (captcha_1), skipping hidden captcha_0
            captcha_input = page.locator(
                "input[name='captcha_1'], input[placeholder*='captcha' i]:visible, input[type='text']:visible"
            ).last
            captcha_input.fill(captcha_text, timeout=5000)

            # Click Login button
            page.locator("button:has-text('Login'), input[value*='Login' i], button[type='submit']").first.click()
            page.wait_for_timeout(5000)

            body_now = page.locator("body").inner_text()
            if "dashboard" in page.url.lower() or "IoT:" in body_now or "OD23KL" in body_now:
                print("Successfully logged in via Captcha solver!")
                return True
            print(f"Captcha rejected or still on login page ({page.url}), retrying...")
        except Exception as e:
            print(f"Attempt {attempt} error: {e}")

    return False


def authenticate(context, page):
    # 1. Try 32-character Django sessionid cookie if available
    if SESSION_COOKIE:
        try:
            # Extract clean 32-char lowercase alphanumeric sessionid even if whole row was pasted
            match_32 = re.search(r"\b([a-z0-9]{32})\b", SESSION_COOKIE)
            cookie_val = match_32.group(1) if match_32 else re.sub(r"[^a-zA-Z0-9]", "", SESSION_COOKIE.split("=")[-1])[:32]

            if cookie_val:
                context.add_cookies([{
                    "name": "sessionid",
                    "value": cookie_val,
                    "url": "https://tpro.telsys.in"
                }])
                print(f"Injected sessionid cookie (length {len(cookie_val)}), opening dashboard...")
                page.goto(DASHBOARD_URL, timeout=30000)
                page.wait_for_timeout(5000)

                body_preview = page.locator("body").inner_text()
                if "IoT:" in body_preview or "OD23KL" in body_preview:
                    print("Successfully logged in via sessionid cookie!")
                    return True
                print("Session cookie expired or logged out. Falling back to Captcha login...")
        except Exception as e:
            print(f"Cookie injection skipped ({e}). Falling back to Captcha login...")

    # 2. Fallback to Automated Captcha Login
    return solve_captcha_and_login(page)


def check_dashboard():
    check_secrets_status()
    alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        if not authenticate(context, page):
            browser.close()
            send_alert(
                "Login Failed",
                "Automated monitor could not log into TelePro after 5 Captcha attempts."
            )
            return

        page.wait_for_timeout(5000)
        body_text = page.locator("body").inner_text()
        print("\n--- Dashboard Loaded Successfully ---")

        # 1. Check BOD, COD, pH, TSS Limits
        for param, bounds in LIMITS.items():
            match = re.search(rf"{param}\s*\n+\s*([\d.]+)", body_text)
            if match:
                val = float(match.group(1))
                print(f"{param}: {val}")
                if "min" in bounds and val < bounds["min"]:
                    alerts.append(f"{param} BELOW limit: {val} (Min allowed: {bounds['min']})")
                if "max" in bounds and val > bounds["max"]:
                    alerts.append(f"{param} EXCEEDED limit: {val} (Max allowed: {bounds['max']})")
            else:
                print(f"Warning: Could not parse {param} from text.")

        # 2. Check IoT Online Status
        if "IoT: Online" not in body_text:
            alerts.append("IoT Device Status is OFFLINE (Not showing 'IoT: Online')!")
        else:
            print("IoT Status: Online")

        # 3. Check CPCB & KSPCB Green Checkmarks
        cpcb_ok = page.locator("text=CPCB: >> .. >> .fa-check, .fa-check-circle, .text-success, svg, img, i").count() > 0
        kspcb_ok = page.locator("text=KSPCB: >> .. >> .fa-check, .fa-check-circle, .text-success, svg, img, i").count() > 0
        print(f"CPCB Checkmark OK: {cpcb_ok} | KSPCB Checkmark OK: {kspcb_ok}")

        if not cpcb_ok:
            alerts.append("Data NOT sending to CPCB (Checkmark missing)!")
        if not kspcb_ok:
            alerts.append("Data NOT sending to KSPCB (Checkmark missing)!")

        # 4. Check Timestamp Freshness
        ts_match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", body_text)
        if ts_match:
            timestamp_str = ts_match.group(1)
            last_update = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            now_ist = datetime.now(IST)
            delay_mins = (now_ist - last_update).total_seconds() / 60
            print(f"Latest Timestamp: {timestamp_str} ({delay_mins:.1f} mins old)")

            if delay_mins > MAX_DATA_DELAY_MINUTES:
                alerts.append(
                    f"Data Not Sending (Stale Timestamp): Last data received at {timestamp_str} "
                    f"({int(delay_mins)} minutes ago, exceeding {MAX_DATA_DELAY_MINUTES}-minute limit)."
                )
        else:
            alerts.append("Could not find a valid data timestamp on the dashboard.")

        browser.close()

    if alerts:
        print("\nIssues Found:")
        for a in alerts:
            print(f" - {a}")
        report = (
            "URGENT: Automated ETP check detected the following issues on "
            "Site OD23KL03233_Effluent_HLLONLINE (HLL Lifecare Limited):\n\n"
            + "\n".join(f"• {a}" for a in alerts)
            + f"\n\nChecked at: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
            + f"\nDashboard: {DASHBOARD_URL}"
        )
        send_alert("Limit Exceeded / KSPCB-CPCB Data Delay", report)
    else:
        print("\nAll parameters normal and transmitting on schedule to CPCB & KSPCB.")


if __name__ == "__main__":
    try:
        check_dashboard()
    except Exception:
        print("CRITICAL ERROR DURING RUN:")
        traceback.print_exc()
        exit(1)
