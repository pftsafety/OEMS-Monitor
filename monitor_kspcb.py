import html
import json
import os
import re
import smtplib
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from playwright.sync_api import sync_playwright

# --- URLS & CREDENTIALS FROM GITHUB SECRETS ---
LOGIN_URL = "https://ebhoomcem.kspcb.kerala.gov.in/"
DASHBOARD_URL = "https://ebhoomcem.kspcb.kerala.gov.in/water"
FACILITY_NAME = "HLL Lifecare Limited, Thiruvananthapuram"
STATE_FILE = "kspcb_state.json"

KSPCB_USER = os.environ.get("KSPCB_USER", "").strip()
KSPCB_PASS = os.environ.get("KSPCB_PASS", "").strip()

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.environ.get("ALERT_EMAIL", "").strip()
SENDER_APP_PASSWORD = os.environ.get("ALERT_EMAIL_PASS", "").strip()
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "").strip()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# --- EFFLUENT THRESHOLD LIMITS ---
LIMITS = {
    "BOD": {"max": 30.0},
    "COD": {"max": 250.0},
    "pH":  {"min": 6.5, "max": 8.5},
    "TSS": {"max": 200.0}
}

# Portal updates every 5 mins -> alert if delayed more than 12 mins (2+ missed pushes)
MAX_DATA_DELAY_MINUTES = 12
IST = timezone(timedelta(hours=5, minutes=30))


def check_secrets_status():
    print("--- KSPCB Secret Configuration Check ---")
    print(f"KSPCB_USER set: {'YES' if KSPCB_USER else 'NO (Missing!)'}")
    print(f"KSPCB_PASS set: {'YES' if KSPCB_PASS else 'NO (Missing!)'}")
    print(f"ALERT_EMAIL set: {'YES' if SENDER_EMAIL else 'NO (Missing!)'}")
    print(f"ALERT_EMAIL_PASS set: {'YES' if SENDER_APP_PASSWORD else 'NO (Missing!)'}")
    print(f"RECEIVER_EMAIL set: {'YES' if RECEIVER_EMAIL else 'NO (Missing!)'}")
    print(f"TELEGRAM_BOT_TOKEN set: {'YES' if TELEGRAM_BOT_TOKEN else 'NO (Optional)'}")
    print(f"TELEGRAM_CHAT_ID set: {'YES' if TELEGRAM_CHAT_ID else 'NO (Optional)'}")
    print("----------------------------------------")


def strip_html(text):
    return re.sub(r"<[^<]+?>", "", text)


def load_system_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return {
                    "offline": bool(data.get("offline", False)),
                    "offline_since": data.get("offline_since")
                }
        except Exception as e:
            print(f"Could not read KSPCB state file: {e}")
    return {"offline": False, "offline_since": None}


def save_system_state(is_offline, offline_since=None):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"offline": is_offline, "offline_since": offline_since}, f)
    except Exception as e:
        print(f"Could not save KSPCB state file: {e}")


def send_html_alert(subject, html_body, plain_text_fallback, prefix="[URGENT]"):
    if not SENDER_EMAIL or not SENDER_APP_PASSWORD or not RECEIVER_EMAIL:
        print("WARNING: Email secrets missing. Skipping email alert.")
        return

    msg = EmailMessage()
    msg["Subject"] = f"{prefix} KSPCB EMS: {subject}"
    msg["From"] = f"ETP Monitor <{SENDER_EMAIL}>"
    msg["To"] = [email.strip() for email in RECEIVER_EMAIL.split(",") if email.strip()]

    msg.set_content(plain_text_fallback)
    msg.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_APP_PASSWORD.replace(" ", ""))
            server.send_message(msg)
        print(f"KSPCB HTML Alert email sent successfully: {prefix} {subject}")
    except Exception as e:
        print(f"Failed to send KSPCB email alert: {e}")


def post_telegram_message(message_text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram secrets not set. Skipping Telegram alert.")
        return

    chat_ids = [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    for idx, chat_id in enumerate(chat_ids, start=1):
        payload = json.dumps({
            "chat_id": chat_id,
            "text": message_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                print(f"KSPCB Telegram message #{idx} sent successfully (HTTP {resp.status}).")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            print(f"Failed to send KSPCB Telegram message #{idx} (HTTP {e.code}): {err_body}")
        except Exception as e:
            print(f"Failed to send KSPCB Telegram message #{idx}: {e}")


def send_telegram_alert(subject, limit_alerts, system_alerts, checked_at):
    lines = [
        "⚠️ <b>KSPCB EMS Portal Alert</b>",
        f"<b>{html.escape(subject)}</b>",
        f"Facility: <code>{html.escape(FACILITY_NAME)}</code>",
        ""
    ]

    if limit_alerts:
        lines.append("📊 <b>Limit Violations:</b>")
        for a in limit_alerts:
            lines.append(f"• {a}")
        lines.append("")

    if system_alerts:
        lines.append("📡 <b>Transmission &amp; System:</b>")
        for a in system_alerts:
            lines.append(f"• {a}")
        lines.append("")

    lines.append(f"🕒 {html.escape(checked_at)}")
    lines.append(f'🔗 <a href="{DASHBOARD_URL}">View Live KSPCB EMS Portal</a>')
    post_telegram_message("\n".join(lines))


def send_online_recovery_alert(was_offline_since, latest_ts, checked_at):
    offline_line = (
        f"<li>Was offline since: <b>{was_offline_since}</b></li>"
        if was_offline_since else ""
    )

    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #ddd; border-top: 5px solid #28a745; border-radius: 5px;">
        <div style="padding: 20px;">
            <h2 style="color: #28a745; margin-top: 0; border-bottom: 2px solid #eee; padding-bottom: 10px;">✅ RESOLVED: KSPCB EMS Back Online</h2>
            <p>Data transmission to <b>KSPCB EMS ({FACILITY_NAME})</b> is back <b>ONLINE</b> and syncing fresh data.</p>
            <ul style="margin-top: 10px; background: #eefaf0; padding: 15px 30px; border-radius: 5px;">
                {offline_line}
                <li>System is online as of: <b>{latest_ts}</b></li>
            </ul>
            <hr style="border: 0; border-top: 1px solid #eee; margin: 25px 0;">
            <p style="font-size: 13px; color: #777; margin-bottom: 20px;">
                <b>Checked at:</b> {checked_at}
            </p>
            <a href="{DASHBOARD_URL}" style="display: inline-block; background-color: #28a745; color: white; padding: 12px 25px; text-decoration: none; border-radius: 4px; font-weight: bold;">View Live KSPCB EMS Portal</a>
        </div>
      </body>
    </html>
    """

    offline_plain = f"Was offline since: {was_offline_since}\n" if was_offline_since else ""
    plain_text = (
        f"KSPCB EMS Back Online - {FACILITY_NAME}\n"
        f"{offline_plain}"
        f"System is online as of: {latest_ts}\n"
        f"Checked at: {checked_at}\n"
    )

    send_html_alert("System Back Online", html_content, plain_text, prefix="[RESOLVED]")

    tg_lines = [
        "✅ <b>KSPCB EMS: System Back Online</b>",
        f"Facility: <code>{html.escape(FACILITY_NAME)}</code>",
        "",
        "🟢 KSPCB portal transmission is back <b>ONLINE</b> and syncing fresh data."
    ]
    if was_offline_since:
        tg_lines.append(f"• Was offline since: <b>{html.escape(was_offline_since)}</b>")
    tg_lines.append(f"• Online as of: <b>{html.escape(latest_ts)}</b>")
    tg_lines.append("")
    tg_lines.append(f"🕒 {html.escape(checked_at)}")
    tg_lines.append(f'🔗 <a href="{DASHBOARD_URL}">View Live KSPCB EMS Portal</a>')
    post_telegram_message("\n".join(tg_lines))


def login_and_load_facility(page):
    if not KSPCB_USER or not KSPCB_PASS:
        print("KSPCB_USER or KSPCB_PASS secret is missing.")
        return False

    try:
        print("Opening KSPCB EMS login page...")
        page.goto(LOGIN_URL, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        page.locator("input[type='email'], input[type='text']:visible").first.fill(KSPCB_USER)
        page.locator("input[type='password']:visible").first.fill(KSPCB_PASS)

        # Select 'End User' role from dropdown
        role_select = page.locator("select").first
        role_select.select_option(label="End User")
        page.wait_for_timeout(500)

        page.locator("button:has-text('Login'), button[type='submit']").first.click()
        print("Submitted KSPCB login form, waiting for dashboard...")

        # Wait for redirect to /water and Welcome Back popup to clear
        page.wait_for_url("**/water**", timeout=25000)
        page.wait_for_timeout(4000)

        # Open 'Select User' dropdown in the top header
        select_user_btn = page.locator("text=Select User").first
        select_user_btn.wait_for(state="visible", timeout=15000)
        select_user_btn.click()
        page.wait_for_timeout(1500)

        # Select HLL Lifecare Limited, Thiruvananthapuram
        facility_opt = page.locator(f"text={FACILITY_NAME}").first
        if facility_opt.count() == 0:
            facility_opt = page.locator("text=HLL Lifecare Limited").first
        facility_opt.wait_for(state="visible", timeout=10000)
        facility_opt.click()

        # Wait for FINAL OUTLET data card to render
        page.locator("text=FINAL OUTLET").first.wait_for(state="visible", timeout=20000)
        page.wait_for_timeout(3000)
        return True
    except Exception as e:
        print(f"KSPCB login/navigation failed: {e}")
        return False


def parse_last_sync(body_text):
    # Matches format: "27 Nov 2026, 06:45:44 pm"
    m1 = re.search(
        r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4},\s*\d{1,2}:\d{2}:\d{2}\s*[APap][Mm])",
        body_text
    )
    if m1:
        raw = re.sub(r"\s+", " ", m1.group(1).strip()).upper()
        dt = datetime.strptime(raw, "%d %b %Y, %I:%M:%S %p").replace(tzinfo=IST)
        return dt

    # Fallback: "2026-11-27 18:45:44"
    m2 = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", body_text)
    if m2:
        dt = datetime.strptime(m2.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        return dt

    return None


def check_kspcb_portal():
    check_secrets_status()
    prev_state = load_system_state()
    limit_alerts = []
    system_alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        if not login_and_load_facility(page):
            browser.close()
            print("KSPCB login failed. Exiting without changing state or sending an alert.")
            return

        body_text = page.locator("body").inner_text()
        print("\n--- KSPCB EMS Dashboard Loaded Successfully ---")

        # 1. Parse LAST SYNC Timestamp
        last_sync_dt = parse_last_sync(body_text)
        latest_ts_formatted = None
        delay_mins = None
        if last_sync_dt:
            latest_ts_formatted = last_sync_dt.strftime("%d-%b-%Y %I:%M:%S %p IST")
            delay_mins = (datetime.now(IST) - last_sync_dt).total_seconds() / 60
            print(f"KSPCB Last Sync: {latest_ts_formatted} ({delay_mins:.1f} mins old)")

        # 2. Check ONLINE / OFFLINE Status Badge
        status_match = re.search(r"Status\s*:\s*(ONLINE|OFFLINE)", body_text, re.IGNORECASE)
        portal_status = status_match.group(1).upper() if status_match else "UNKNOWN"
        print(f"KSPCB Portal Status: {portal_status}")

        status_offline = portal_status != "ONLINE"
        if status_offline:
            if latest_ts_formatted:
                system_alerts.append(
                    f"KSPCB Portal Status is <b>{portal_status}</b> since <b>{latest_ts_formatted}</b>."
                )
            else:
                system_alerts.append(f"KSPCB Portal Status is <b>{portal_status}</b>.")

        # 3. Check Data Freshness (5-min update cycle -> 12-min threshold)
        data_stale = False
        if latest_ts_formatted is not None:
            if delay_mins > MAX_DATA_DELAY_MINUTES:
                data_stale = True
                system_alerts.append(
                    f"System is offline on KSPCB EMS since <b>{latest_ts_formatted}</b> "
                    f"({int(delay_mins)} mins ago, exceeding {MAX_DATA_DELAY_MINUTES}m limit)."
                )
        else:
            data_stale = True
            system_alerts.append("Could not find a valid LAST SYNC timestamp on KSPCB EMS.")

        # 4. Check Parameter Limits inside FINAL OUTLET card
        outlet_section = body_text
        if "FINAL OUTLET" in body_text:
            outlet_section = body_text.split("FINAL OUTLET", 1)[1]
            if "Stack:" in outlet_section:
                outlet_section = outlet_section.split("Stack:", 1)[0]

        for param, bounds in LIMITS.items():
            match = re.search(rf"\b{param}\b(?:\s*\([^)]*\))?\s*\n+\s*([\d.]+)", outlet_section)
            if match:
                val = float(match.group(1))
                print(f"KSPCB {param}: {val}")
                if "min" in bounds and val < bounds["min"]:
                    limit_alerts.append(
                        f"<b>{param}</b> is BELOW limit on KSPCB EMS: {val} <i>(Min allowed: {bounds['min']})</i>"
                    )
                if "max" in bounds and val > bounds["max"]:
                    limit_alerts.append(
                        f"<b>{param}</b> EXCEEDED limit on KSPCB EMS: {val} <i>(Max allowed: {bounds['max']})</i>"
                    )
            else:
                print(f"Warning: Could not parse KSPCB {param} from FINAL OUTLET.")

        browser.close()

    checked_at = datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")
    is_currently_offline = status_offline or data_stale

    # 5. Track Offline -> Online Recovery State
    if is_currently_offline:
        recorded_since = prev_state.get("offline_since") or latest_ts_formatted or checked_at
        save_system_state(True, recorded_since)
    else:
        if prev_state.get("offline"):
            print("\nKSPCB EMS recovered! Sending Back Online alert...")
            send_online_recovery_alert(
                prev_state.get("offline_since"),
                latest_ts_formatted or checked_at,
                checked_at
            )
        save_system_state(False, None)

    # Determine Dynamic Subject Line
    if limit_alerts and system_alerts:
        final_subject = "Parameter Limits Exceeded & Data Transmission Failure"
    elif limit_alerts:
        final_subject = "Parameter Limit Exceeded"
    elif system_alerts:
        final_subject = "Data Transmission & Delay Failure"
    else:
        print("\nKSPCB EMS: All parameters normal and syncing on schedule.")
        return

    # Build HTML Email Body
    html_content = f"""
    <html>
      <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #ddd; border-top: 5px solid #d9534f; border-radius: 5px;">
        <div style="padding: 20px;">
            <h2 style="color: #d9534f; margin-top: 0; border-bottom: 2px solid #eee; padding-bottom: 10px;">⚠️ URGENT: KSPCB EMS Alert</h2>
            <p>The automated monitoring system has detected issues on <b>KSPCB EMS ({FACILITY_NAME})</b>.</p>
    """

    if limit_alerts:
        html_content += "<h3 style='color: #d9534f; margin-bottom: 5px;'>📊 Parameter Limit Violations:</h3><ul style='margin-top: 5px; background: #fff3f3; padding: 15px 30px; border-radius: 5px;'>"
        for a in limit_alerts:
            html_content += f"<li style='margin-bottom: 5px;'>{a}</li>"
        html_content += "</ul>"

    if system_alerts:
        html_content += "<h3 style='color: #f0ad4e; margin-bottom: 5px;'>📡 Transmission & System Issues:</h3><ul style='margin-top: 5px; background: #fff8eb; padding: 15px 30px; border-radius: 5px;'>"
        for a in system_alerts:
            html_content += f"<li style='margin-bottom: 5px;'>{a}</li>"
        html_content += "</ul>"

    html_content += f"""
            <hr style="border: 0; border-top: 1px solid #eee; margin: 25px 0;">
            <p style="font-size: 13px; color: #777; margin-bottom: 20px;">
                <b>Checked at:</b> {checked_at}
            </p>
            <a href="{DASHBOARD_URL}" style="display: inline-block; background-color: #0275d8; color: white; padding: 12px 25px; text-decoration: none; border-radius: 4px; font-weight: bold;">View Live KSPCB EMS Portal</a>
        </div>
      </body>
    </html>
    """

    plain_text = "KSPCB EMS Alerts Detected:\n\n"
    for a in limit_alerts + system_alerts:
        plain_text += f"- {strip_html(a)}\n"

    send_html_alert(final_subject, html_content, plain_text)
    send_telegram_alert(final_subject, limit_alerts, system_alerts, checked_at)


if __name__ == "__main__":
    try:
        check_kspcb_portal()
    except Exception:
        print("CRITICAL ERROR DURING KSPCB RUN:")
        traceback.print_exc()
        exit(1)
