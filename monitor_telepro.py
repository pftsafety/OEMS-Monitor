import os
import re
import smtplib
import traceback
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import PIL.Image

# Fix Pillow 10+ compatibility for ddddocr just in case
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

LIMITS = {
    "BOD": {"max": 30.0},
    "COD": {"max": 250.0},
    "pH":  {"min": 5.5, "max": 9.0},
    "TSS": {"max": 100.0}
}

MAX_DATA_DELAY_MINUTES = 10
IST = timezone(timedelta(hours=5, minutes=30))


def send_alert(subject, body):
    if not SENDER_EMAIL or not SENDER_APP_PASSWORD or not RECEIVER_EMAIL:
        print("WARNING: Email secrets (ALERT_EMAIL / ALERT_EMAIL_PASS / RECEIVER_EMAIL) are missing or empty!")
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

    for attempt in range(1, 5):
        print(f"\n--- Captcha Login Attempt {attempt}/4 ---")
        try:
            page.goto(LOGIN_URL, timeout=30000)
            page.wait_for_timeout(3000)

            # Fill Email & Password
            page.locator("input[type='text'], input[type='email']").first.fill(USERNAME)
            page.locator("input[type='password']").first.fill(PASSWORD)

            # Find Captcha image reliably
            captcha_el = None
            for selector in [
                "img[src*='captcha' i]",
                "img.captcha",
                "form img",
                "canvas"
            ]:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible():
                    captcha_el = loc
                    break

            if not captcha_el:
                # Fallback: pick any visible image inside the login box that isn't the logo
                imgs = page.locator("img").all()
                for img in imgs:
                    box = img.bounding_box()
                    if box and box["width"] > 60 and box["height"] < 120:
                        captcha_el = img
                        break

            img_bytes = captcha_el.screenshot(timeout=5000)
            raw_text = ocr.classification(img_bytes).strip().upper()
            captcha_text = re.sub(r"[^A-Z0-9]", "", raw_text)
            print(f"OCR read Captcha as: '{captcha_text}'")

            # Fill Captcha input box
            captcha_input = page.locator("input[placeholder*='captcha' i], input[name*='captcha' i]").first
            if captcha_input.count() == 0:
                captcha_input = page.locator("input[type='text']").nth(1)
            captcha_input.fill(captcha_text)

            # Click Login button
            page.locator("button:has-text('Login'), input[value*='Login' i], button[type='submit']").first.click()
            page.wait_for_timeout(5000)

            if "dashboard" in page.url.lower() or "IoT:" in page.locator("body").inner_text():
                print("Successfully logged in via Captcha solver!")
                return True
            else:
                print(f"Still on {page.url}, retrying...")
        except Exception as e:
            print(f"Attempt {attempt} encountered an error: {e}")

    return False


def authenticate(context, page):
    if SESSION_COOKIE:
        cookie_val = SESSION_COOKIE.split("=", 1)[-1].strip()
        context.add_cookies([{
            "name": "sessionid",
            "value": cookie_val,
            "domain": "tpro.telsys.in",
            "path": "/"
        }])
        print("Trying sessionid cookie...")
        page.goto(DASHBOARD_URL, timeout=30000)
        page.wait_for_timeout(5000)

        body_preview = page.locator("body").inner_text()
        if "IoT:" in body_preview or "BOD" in body_preview:
            print("Logged in directly via sessionid cookie!")
            return True
        print("Session cookie did not open dashboard. Falling back to Captcha login...")

    return solve_captcha_and_login(page)


def check_dashboard():
    alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        if not authenticate(context, page):
            browser.close()
            send_alert(
                "Login Failed",
                "Automated monitor could not log into TelePro. Please log into TelePro on Chrome, copy the new 'sessionid' cookie from F12 -> Application -> Cookies, and update TELEPRO_COOKIE in GitHub Secrets."
            )
            return

        page.wait_for_timeout(5000)
        body_text = page.locator("body").inner_text()
        print("\n--- Dashboard Loaded Successfully ---")

        # 1. Check BOD, COD, pH, TSS Limits
        for param, bounds in LIMITS.items():
            match = re.search(rf"{param}\s*\n\s*([\d.]+)", body_text)
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

        # 3. Check CPCB & KSPCB Green Checkmarks
        cpcb_ok = page.locator("text=CPCB: >> .. >> .fa-check, .fa-check-circle, .text-success, svg, img, i").count() > 0
        kspcb_ok = page.locator("text=KSPCB: >> .. >> .fa-check, .fa-check-circle, .text-success, svg, img, i").count() > 0

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
        report = (
            "URGENT: Automated ETP check detected the following issues on "
            "Site OD23KL03233_Effluent_HLLONLINE (HLL Lifecare Limited):\n\n"
            + "\n".join(f"• {a}" for a in alerts)
            + f"\n\nChecked at: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
            + f"\nDashboard: {DASHBOARD_URL}"
        )
        send_alert("Limit Exceeded / KSPCB-CPCB Data Delay", report)
    else:
        print("All parameters normal and transmitting on schedule to CPCB & KSPCB.")


if __name__ == "__main__":
    try:
        check_dashboard()
    except Exception:
        print("CRITICAL ERROR DURING RUN:")
        traceback.print_exc()
        exit(1)
