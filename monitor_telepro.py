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
    "pH":  {"min": 6.5, "max": 8.5},
    "TSS": {"max": 200.0}
}

MAX_DATA_DELAY_MINUTES = 18
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


def send_html_alert(subject, html_body, plain_text_fallback):
    if not SENDER_EMAIL or not SENDER_APP_PASSWORD or not RECEIVER_EMAIL:
        print("WARNING: Cannot send email because ALERT_EMAIL, ALERT_EMAIL_PASS, or RECEIVER_EMAIL secret is empty!")
        return

    msg = EmailMessage()
    msg["Subject"] = f"[URGENT] HLL TelePro: {subject}"
    msg["From"] = f"ETP Monitor <{SENDER_EMAIL}>"
    msg["To"] = [email.strip() for email in RECEIVER_EMAIL.split(",") if email.strip()]

    # Set plain text version first (fallback), then attach the HTML version
    msg.set_content(plain_text_fallback)
    msg.add_alternative(html_body, subtype='html')

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_APP_PASSWORD.replace(" ", ""))
            server.send_message(msg)
        print(f"HTML Alert email sent successfully: {subject}")
    except Exception as e:
        print(f"Failed to send email alert: {e}")


def solve_captcha_and_login(page):
    ocr = ddddocr.DdddOcr(show_ad=False)

    for attempt in range(1, 6):
        print(f"\n--- Captcha Login Attempt {attempt}/5 ---")
        try:
            page.goto(LOGIN_URL, timeout=30000)
            page.wait_for_timeout(2500)

            page.locator("input[type='text']:visible, input[type='email']:visible").first.fill(USERNAME)
            page.locator("input[type='password']:visible").first.fill(PASSWORD)

            captcha_el = page.locator("img.captcha, img[src*='captcha' i], form img").first
            img_bytes = captcha_el.screenshot(timeout=5000)
            raw_text = ocr.classification(img_bytes).strip().upper()
            captcha_text = re.sub(r"[^A-Z0-9]", "", raw_text)
            print(f"OCR read Captcha as: '{captcha_text}'")

            captcha_input = page.locator(
                "input[name='captcha_1'], input[placeholder*='captcha' i]:visible, input[type='text']:visible"
            ).last
            captcha_input.fill(captcha_text, timeout=5000)

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
    if SESSION_COOKIE:
        try:
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

    return solve_captcha_and_login(page)


def check_dashboard():
    check_secrets_status()
    limit_alerts = []
    system_alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        if not authenticate(context, page):
            browser.close()
            print("Login failed. Exiting without sending an alert email.")
            return

        page.wait_for_timeout(5000)
        body_text = page.locator("body").inner_text()
        print("\n--- Dashboard Loaded Successfully ---")

        # 1. Check Limits (BOD, COD, pH, TSS)
        for param, bounds in LIMITS.items():
            match = re.search(rf"{param}\s*\n+\s*([\d.]+)", body_text)
            if match:
                val = float(match.group(1))
                print(f"{param}: {val}")
                if "min" in bounds and val < bounds["min"]:
                    limit_alerts.append(f"<b>{param}</b> is BELOW limit: {val} <i>(Min allowed: {bounds['min']})</i>")
                if "max" in bounds and val > bounds["max"]:
                    limit_alerts.append(f"<b>{param}</b> EXCEEDED limit: {val} <i>(Max allowed: {bounds['max']})</i>")
            else:
                print(f"Warning: Could not parse {param} from text.")

        # 2. Check IoT Status
        if "IoT: Online" not in body_text:
            system_alerts.append("IoT Device Status is <b>OFFLINE</b>")

        # 3. Check CPCB/KSPCB Transmission
        cpcb_ok = page.locator("text=CPCB: >> .. >> .fa-check, .fa-check-circle, .text-success, svg, img, i").count() > 0
        kspcb_ok = page.locator("text=KSPCB: >> .. >> .fa-check, .fa-check-circle, .text-success, svg, img, i").count() > 0
        
        if not cpcb_ok: system_alerts.append("Data NOT sending to <b>CPCB</b> (Checkmark missing)")
        if not kspcb_ok: system_alerts.append("Data NOT sending to <b>KSPCB</b> (Checkmark missing)")

        # 4. Check Data Freshness
        ts_match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", body_text)
        if ts_match:
            timestamp_str = ts_match.group(1)
            last_update = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            delay_mins = (datetime.now(IST) - last_update).total_seconds() / 60
            print(f"Latest Timestamp: {timestamp_str} ({delay_mins:.1f} mins old)")

            if delay_mins > MAX_DATA_DELAY_MINUTES:
                system_alerts.append(
                    f"Stale Data: Last update at {timestamp_str} ({int(delay_mins)} mins ago, exceeding {MAX_DATA_DELAY_MINUTES}m limit)."
                )
        else:
            system_alerts.append("Could not find a valid data timestamp on the dashboard.")

        browser.close()

    # Determine Dynamic Subject Line
    if limit_alerts and system_alerts:
        final_subject = "Parameter Limits Exceeded & Data Transmission Failure"
    elif limit_alerts:
        final_subject = "Parameter Limit Exceeded"
    elif system_alerts:
        final_subject = "Data Transmission & Delay Failure"
    else:
        print("\nAll parameters normal and transmitting on schedule.")
        return

    # Build the HTML Email Body
    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #ddd; border-top: 5px solid #d9534f; border-radius: 5px;">
        <div style="padding: 20px;">
            <h2 style="color: #d9534f; margin-top: 0; border-bottom: 2px solid #eee; padding-bottom: 10px;">⚠️ URGENT: ETP Alert</h2>
            <p>The automated monitoring system has detected issues at <b>Site OD23KL03233_Effluent_HLLONLINE</b>.</p>
    """
    
    # Inject Limit Alerts into HTML
    if limit_alerts:
        html_content += "<h3 style='color: #d9534f; margin-bottom: 5px;'>📊 Parameter Limit Violations:</h3><ul style='margin-top: 5px; background: #fff3f3; padding: 15px 30px; border-radius: 5px;'>"
        for a in limit_alerts: html_content += f"<li style='margin-bottom: 5px;'>{a}</li>"
        html_content += "</ul>"

    # Inject System/Delay Alerts into HTML
    if system_alerts:
        html_content += "<h3 style='color: #f0ad4e; margin-bottom: 5px;'>📡 Transmission & System Issues:</h3><ul style='margin-top: 5px; background: #fff8eb; padding: 15px 30px; border-radius: 5px;'>"
        for a in system_alerts: html_content += f"<li style='margin-bottom: 5px;'>{a}</li>"
        html_content += "</ul>"

    # Footer and Button
    html_content += f"""
            <hr style="border: 0; border-top: 1px solid #eee; margin: 25px 0;">
            <p style="font-size: 13px; color: #777; margin-bottom: 20px;">
                <b>Checked at:</b> {datetime.now(IST).strftime('%Y-%m-%d %I:%M:%S %p IST')}
            </p>
            <a href="{DASHBOARD_URL}" style="display: inline-block; background-color: #0275d8; color: white; padding: 12px 25px; text-decoration: none; border-radius: 4px; font-weight: bold;">View Live TelePro Dashboard</a>
        </div>
      </body>
    </html>
    """
    
    # Plain text fallback
    plain_text = "ETP Alerts Detected:\n\n"
    for a in limit_alerts + system_alerts:
        plain_text += f"- {re.sub('<[^<]+>', '', a)}\n"
    
    # Dispatch Email
    send_html_alert(final_subject, html_content, plain_text)


if __name__ == "__main__":
    try:
        check_dashboard()
    except Exception:
        print("CRITICAL ERROR DURING RUN:")
        traceback.print_exc()
        exit(1)
