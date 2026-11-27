import html
import io
import json
import os
import re
import smtplib
import traceback
import urllib.error
import urllib.request
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
SESSION_FILE = "session_cookie.txt"
STATE_FILE = "system_state.json"

USERNAME = os.environ.get("TELEPRO_USER", "").strip()
PASSWORD = os.environ.get("TELEPRO_PASS", "").strip()
SESSION_COOKIE = os.environ.get("TELEPRO_COOKIE", "").strip()

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
    print(f"TELEGRAM_BOT_TOKEN set: {'YES' if TELEGRAM_BOT_TOKEN else 'NO (Optional)'}")
    print(f"TELEGRAM_CHAT_ID set: {'YES' if TELEGRAM_CHAT_ID else 'NO (Optional)'}")
    print("----------------------------------")


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
            print(f"Could not read state file: {e}")
    return {"offline": False, "offline_since": None}


def save_system_state(is_offline, offline_since=None):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"offline": is_offline, "offline_since": offline_since}, f)
    except Exception as e:
        print(f"Could not save state file: {e}")


def send_html_alert(subject, html_body, plain_text_fallback, prefix="[URGENT]"):
    if not SENDER_EMAIL or not SENDER_APP_PASSWORD or not RECEIVER_EMAIL:
        print("WARNING: Cannot send email because ALERT_EMAIL, ALERT_EMAIL_PASS, or RECEIVER_EMAIL secret is empty!")
        return

    msg = EmailMessage()
    msg["Subject"] = f"{prefix} HLL TelePro: {subject}"
    msg["From"] = f"ETP Monitor <{SENDER_EMAIL}>"
    msg["To"] = [email.strip() for email in RECEIVER_EMAIL.split(",") if email.strip()]

    msg.set_content(plain_text_fallback)
    msg.add_alternative(html_body, subtype='html')

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_APP_PASSWORD.replace(" ", ""))
            server.send_message(msg)
        print(f"HTML Alert email sent successfully: {prefix} {subject}")
    except Exception as e:
        print(f"Failed to send email alert: {e}")


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
                print(f"Telegram message #{idx} sent successfully (HTTP {resp.status}).")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            print(f"Failed to send Telegram message #{idx} (HTTP {e.code}): {err_body}")
        except Exception as e:
            print(f"Failed to send Telegram message #{idx}: {e}")


def send_telegram_alert(subject, limit_alerts, system_alerts, checked_at):
    lines = [
        "⚠️ <b>HLL TelePro ETP Alert</b>",
        f"<b>{html.escape(subject)}</b>",
        "Site: <code>OD23KL03233_Effluent_HLLONLINE</code>",
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
    lines.append(f'🔗 <a href="{DASHBOARD_URL}">View Live TelePro Dashboard</a>')
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
            <h2 style="color: #28a745; margin-top: 0; border-bottom: 2px solid #eee; padding-bottom: 10px;">✅ RESOLVED: System Back Online</h2>
            <p>The automated monitoring system at <b>Site OD23KL03233_Effluent_HLLONLINE</b> is back <b>ONLINE</b> and transmitting fresh data.</p>
            <ul style="margin-top: 10px; background: #eefaf0; padding: 15px 30px; border-radius: 5px;">
                {offline_line}
                <li>System is online as of: <b>{latest_ts}</b></li>
            </ul>
            <hr style="border: 0; border-top: 1px solid #eee; margin: 25px 0;">
            <p style="font-size: 13px; color: #777; margin-bottom: 20px;">
                <b>Checked at:</b> {checked_at}
            </p>
            <a href="{DASHBOARD_URL}" style="display: inline-block; background-color: #28a745; color: white; padding: 12px 25px; text-decoration: none; border-radius: 4px; font-weight: bold;">View Live TelePro Dashboard</a>
        </div>
      </body>
    </html>
    """

    plain_text = (
        f"System Back Online - Site OD23KL03233_Effluent_HLLONLINE\n"
        f"{f'Was offline since: {was_offline_since}\n' if was_offline_since else ''}"
        f"System is online as of: {latest_ts}\n"
        f"Checked at: {checked_at}\n"
    )

    send_html_alert("System Back Online", html_content, plain_text, prefix="[RESOLVED]")

    tg_lines = [
        "✅ <b>HLL TelePro ETP: System Back Online</b>",
        "Site: <code>OD23KL03233_Effluent_HLLONLINE</code>",
        "",
        "🟢 System is back <b>ONLINE</b> and transmitting fresh data."
    ]
    if was_offline_since:
        tg_lines.append(f"• Was offline since: <b>{html.escape(was_offline_since)}</b>")
    tg_lines.append(f"• Online as of: <b>{html.escape(latest_ts)}</b>")
    tg_lines.append("")
    tg_lines.append(f"🕒 {html.escape(checked_at)}")
    tg_lines.append(f'🔗 <a href="{DASHBOARD_URL}">View Live TelePro Dashboard</a>')
    post_telegram_message("\n".join(tg_lines))


def check_board_status(page, label):
    """Isolates the exact CPCB: or KSPCB: text line and inspects only its adjacent icon."""
    other_label = "KSPCB:" if label == "CPCB:" else "CPCB:"

    box_info = page.evaluate("""({ label, otherLabel }) => {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let node;
        while ((node = walker.nextNode())) {
            const raw = (node.nodeValue || '').trim();
            if (!raw) continue;
            const norm = raw.replace(/\\s+/g, '').toUpperCase();
            if (!norm.includes(label.toUpperCase()) || norm.includes(otherLabel.toUpperCase())) {
                continue;
            }
            const parent = node.parentElement;
            if (!parent) continue;
            parent.scrollIntoView({ block: 'center' });

            const range = document.createRange();
            range.selectNodeContents(node);
            const rect = range.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            if (/[❌✖✗✘]/.test(raw)) {
                return { status: 'fail', info: `Text emoji fail (${raw})` };
            }
            if (/[✅✔✓]/.test(raw)) {
                return { status: 'pass', info: `Text emoji pass (${raw})` };
            }

            const iconEl = document.elementFromPoint(rect.right + 12, rect.top + rect.height / 2);
            const iconCls = iconEl
                ? (typeof iconEl.className === 'string' ? iconEl.className : (iconEl.className?.baseVal || ''))
                : '';

            return {
                status: 'clip',
                x: Math.max(0, rect.left),
                y: Math.max(0, rect.top - 4),
                width: rect.width + 45,
                height: Math.max(24, rect.height + 8),
                info: `iconTag=${iconEl ? iconEl.tagName : 'none'} cls='${iconCls}'`
            };
        }
        return { status: 'missing', info: 'Visible label not found' };
    }""", {"label": label, "otherLabel": other_label})

    if box_info["status"] == "fail":
        return False, box_info["info"]
    if box_info["status"] == "pass":
        return True, box_info["info"]
    if box_info["status"] != "clip":
        return False, box_info["info"]

    try:
        png_bytes = page.screenshot(clip={
            "x": box_info["x"],
            "y": box_info["y"],
            "width": box_info["width"],
            "height": box_info["height"]
        })
        img = PIL.Image.open(io.BytesIO(png_bytes)).convert("RGB")
        red_px = 0
        green_px = 0
        for r, g, b in img.getdata():
            if r > 140 and r > g * 1.4 and r > b * 1.4:
                red_px += 1
            elif g > 110 and g > r * 1.25:
                green_px += 1

        info = f"Icon crop (red={red_px}, green={green_px}) | {box_info['info']}"
        if red_px > green_px and red_px > 15:
            return False, info
        if green_px > red_px and green_px > 15:
            return True, info
        return False, f"No green checkmark detected | {info}"
    except Exception as e:
        return False, f"Crop error: {e} | {box_info['info']}"


def save_session_cookie(context):
    for c in context.cookies():
        if c.get("name") == "sessionid" and c.get("value"):
            with open(SESSION_FILE, "w") as f:
                f.write(c["value"].strip())
            print("Saved live sessionid to cache for future runs (Captcha bypassed next time).")
            return


def read_captcha_best(img_bytes, ocr_beta, ocr_std):
    img = PIL.Image.open(io.BytesIO(img_bytes)).convert("L")
    img = img.resize((img.width * 2, img.height * 2), PIL.Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    upscaled_bytes = buf.getvalue()

    candidates = []
    for engine, data in [(ocr_beta, upscaled_bytes), (ocr_beta, img_bytes), (ocr_std, img_bytes)]:
        try:
            raw = engine.classification(data).strip().upper()
            cleaned = re.sub(r"[^A-Z]", "", raw)
            if cleaned:
                candidates.append(cleaned)
        except Exception:
            pass

    for c in candidates:
        if len(c) == 6:
            return c
    return candidates[0] if candidates else ""


def solve_captcha_and_login(context, page):
    ocr_std = ddddocr.DdddOcr(show_ad=False)
    try:
        ocr_beta = ddddocr.DdddOcr(beta=True, show_ad=False)
    except Exception:
        ocr_beta = ocr_std

    context.clear_cookies()

    for attempt in range(1, 13):
        print(f"\n--- Captcha Login Attempt {attempt}/12 ---")
        try:
            page.goto(LOGIN_URL, timeout=30000)
            page.wait_for_timeout(2000)

            page.locator("input[type='text']:visible, input[type='email']:visible").first.fill(USERNAME)
            page.locator("input[type='password']:visible").first.fill(PASSWORD)

            captcha_el = page.locator("img.captcha, img[src*='captcha' i], form img").first
            img_bytes = captcha_el.screenshot(timeout=5000)
            captcha_text = read_captcha_best(img_bytes, ocr_beta, ocr_std)
            print(f"OCR read Captcha as: '{captcha_text}'")

            if len(captcha_text) < 5:
                print("Incomplete OCR read, reloading fresh Captcha...")
                continue

            captcha_input = page.locator(
                "input[name='captcha_1'], input[placeholder*='captcha' i]:visible, input[type='text']:visible"
            ).last
            captcha_input.fill(captcha_text, timeout=5000)

            page.locator("button:has-text('Login'), input[value*='Login' i], button[type='submit']").first.click()
            page.wait_for_timeout(3500)

            body_now = page.locator("body").inner_text()
            if "dashboard" in page.url.lower() or "IoT:" in body_now or "OD23KL" in body_now:
                print("Successfully logged in via Captcha solver!")
                save_session_cookie(context)
                return True
            print(f"Captcha rejected ({page.url}), retrying...")
        except Exception as e:
            print(f"Attempt {attempt} error: {e}")

    return False


def try_cookie(context, page, raw_cookie, source_label):
    match_32 = re.search(r"\b([a-z0-9]{32})\b", raw_cookie)
    cookie_val = match_32.group(1) if match_32 else re.sub(r"[^a-zA-Z0-9]", "", raw_cookie.split("=")[-1])[:32]
    if not cookie_val:
        return False

    context.clear_cookies()
    context.add_cookies([{
        "name": "sessionid",
        "value": cookie_val,
        "url": "https://tpro.telsys.in"
    }])
    print(f"Trying sessionid from {source_label} (length {len(cookie_val)})...")
    page.goto(DASHBOARD_URL, timeout=30000)
    page.wait_for_timeout(4000)

    body_preview = page.locator("body").inner_text()
    if "IoT:" in body_preview or "OD23KL" in body_preview:
        print(f"Successfully logged in via {source_label}!")
        save_session_cookie(context)
        return True

    print(f"Cookie from {source_label} expired or logged out.")
    return False


def authenticate(context, page):
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r") as f:
                cached_cookie = f.read().strip()
            if cached_cookie and try_cookie(context, page, cached_cookie, "auto-cache"):
                return True
        except Exception as e:
            print(f"Cached cookie check failed: {e}")

    if SESSION_COOKIE:
        try:
            if try_cookie(context, page, SESSION_COOKIE, "GitHub Secret"):
                return True
        except Exception as e:
            print(f"Secret cookie check failed: {e}")

    print("Falling back to upgraded Captcha login...")
    if solve_captcha_and_login(context, page):
        return True

    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    return False


def check_dashboard():
    check_secrets_status()
    prev_state = load_system_state()
    limit_alerts = []
    system_alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        if not authenticate(context, page):
            browser.close()
            print("Login failed. Exiting without changing state or sending an alert.")
            return

        page.wait_for_timeout(4000)
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

        # 2. Extract Latest Data Timestamp
        ts_match = re.search(r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", body_text)
        latest_ts_formatted = None
        delay_mins = None
        if ts_match:
            timestamp_str = ts_match.group(1)
            last_update = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            latest_ts_formatted = last_update.strftime("%d-%b-%Y %I:%M:%S %p IST")
            delay_mins = (datetime.now(IST) - last_update).total_seconds() / 60
            print(f"Latest Timestamp: {latest_ts_formatted} ({delay_mins:.1f} mins old)")

        # 3. Check IoT Status
        iot_offline = "IoT: Online" not in body_text
        if iot_offline:
            if latest_ts_formatted:
                system_alerts.append(f"IoT Device Status is <b>OFFLINE</b> since <b>{latest_ts_formatted}</b>.")
            else:
                system_alerts.append("IoT Device Status is <b>OFFLINE</b>.")

        # 4. Check CPCB/KSPCB Transmission (Exact Line Crop Check)
        cpcb_ok, cpcb_info = check_board_status(page, "CPCB:")
        kspcb_ok, kspcb_info = check_board_status(page, "KSPCB:")
        print(f"CPCB Status: {'OK' if cpcb_ok else 'FAILED'} | {cpcb_info}")
        print(f"KSPCB Status: {'OK' if kspcb_ok else 'FAILED'} | {kspcb_info}")

        if not cpcb_ok:
            system_alerts.append("Data NOT sending to <b>CPCB</b> (Red X / Transmission Failed)")
        if not kspcb_ok:
            system_alerts.append("Data NOT sending to <b>KSPCB</b> (Red X / Transmission Failed)")

        # 5. Check Data Freshness
        data_stale = False
        if latest_ts_formatted is not None:
            if delay_mins > MAX_DATA_DELAY_MINUTES:
                data_stale = True
                system_alerts.append(
                    f"System is offline since <b>{latest_ts_formatted}</b> ({int(delay_mins)} mins ago, exceeding {MAX_DATA_DELAY_MINUTES}m limit)."
                )
        else:
            data_stale = True
            system_alerts.append("Could not find a valid data timestamp on the dashboard.")

        browser.close()

    checked_at = datetime.now(IST).strftime("%d-%b-%Y %I:%M:%S %p IST")
    is_currently_offline = iot_offline or data_stale

    # 6. Track Offline -> Online Recovery State
    if is_currently_offline:
        recorded_since = prev_state.get("offline_since") or latest_ts_formatted or checked_at
        save_system_state(True, recorded_since)
    else:
        if prev_state.get("offline"):
            print("\nSystem recovered! Sending Back Online alert...")
            send_online_recovery_alert(
                prev_state.get("offline_since"),
                latest_ts_formatted or checked_at,
                checked_at
            )
        save_system_state(False, None)

    # Determine Dynamic Subject Line for Active Alerts
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
            <a href="{DASHBOARD_URL}" style="display: inline-block; background-color: #0275d8; color: white; padding: 12px 25px; text-decoration: none; border-radius: 4px; font-weight: bold;">View Live TelePro Dashboard</a>
        </div>
      </body>
    </html>
    """

    plain_text = "ETP Alerts Detected:\n\n"
    for a in limit_alerts + system_alerts:
        plain_text += f"- {strip_html(a)}\n"

    # Dispatch Both HTML Email & Telegram Alert
    send_html_alert(final_subject, html_content, plain_text)
    send_telegram_alert(final_subject, limit_alerts, system_alerts, checked_at)


if __name__ == "__main__":
    try:
        check_dashboard()
    except Exception:
        print("CRITICAL ERROR DURING RUN:")
        traceback.print_exc()
        exit(1)
