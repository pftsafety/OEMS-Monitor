import io
import os
import re
import smtplib
import traceback
import urllib.parse
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

USERNAME = os.environ.get("TELEPRO_USER", "").strip()
PASSWORD = os.environ.get("TELEPRO_PASS", "").strip()
SESSION_COOKIE = os.environ.get("TELEPRO_COOKIE", "").strip()

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.environ.get("ALERT_EMAIL", "").strip()
SENDER_APP_PASSWORD = os.environ.get("ALERT_EMAIL_PASS", "").strip()
RECEIVER_EMAIL = os.environ.get("RECEIVER_EMAIL", "").strip()

WHATSAPP_PHONE = os.environ.get("WHATSAPP_PHONE", "").strip()
WHATSAPP_APIKEY = os.environ.get("WHATSAPP_APIKEY", "").strip()

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
    print(f"WHATSAPP_PHONE set: {'YES' if WHATSAPP_PHONE else 'NO (Optional)'}")
    print(f"WHATSAPP_APIKEY set: {'YES' if WHATSAPP_APIKEY else 'NO (Optional)'}")
    print("----------------------------------")


def strip_html(text):
    return re.sub(r"<[^<]+?>", "", text)


def send_html_alert(subject, html_body, plain_text_fallback):
    if not SENDER_EMAIL or not SENDER_APP_PASSWORD or not RECEIVER_EMAIL:
        print("WARNING: Cannot send email because ALERT_EMAIL, ALERT_EMAIL_PASS, or RECEIVER_EMAIL secret is empty!")
        return

    msg = EmailMessage()
    msg["Subject"] = f"[URGENT] HLL TelePro: {subject}"
    msg["From"] = f"ETP Monitor <{SENDER_EMAIL}>"
    msg["To"] = [email.strip() for email in RECEIVER_EMAIL.split(",") if email.strip()]

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


def send_whatsapp_alert(subject, limit_alerts, system_alerts, checked_at):
    if not WHATSAPP_PHONE or not WHATSAPP_APIKEY:
        print("WhatsApp secrets not set. Skipping WhatsApp alert.")
        return

    phones = [p.strip().replace(" ", "").replace("-", "") for p in WHATSAPP_PHONE.split(",") if p.strip()]
    apikeys = [k.strip() for k in WHATSAPP_APIKEY.split(",") if k.strip()]

    lines = [
        "⚠️ *HLL TelePro ETP Alert*",
        f"*{subject}*",
        "Site: OD23KL03233_Effluent_HLLONLINE",
        ""
    ]

    if limit_alerts:
        lines.append("📊 *Limit Violations:*")
        for a in limit_alerts:
            lines.append(f"• {strip_html(a)}")
        lines.append("")

    if system_alerts:
        lines.append("📡 *Transmission & System:*")
        for a in system_alerts:
            lines.append(f"• {strip_html(a)}")
        lines.append("")

    lines.append(f"🕒 {checked_at}")
    lines.append(f"🔗 {DASHBOARD_URL}")

    encoded_msg = urllib.parse.quote("\n".join(lines))

    for idx, (phone, apikey) in enumerate(zip(phones, apikeys), start=1):
        encoded_phone = urllib.parse.quote(phone)
        encoded_key = urllib.parse.quote(apikey)
        url = (
            f"https://api.callmebot.com/whatsapp.php?"
            f"phone={encoded_phone}&text={encoded_msg}&apikey={encoded_key}"
        )
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                print(f"WhatsApp alert #{idx} sent successfully (HTTP {resp.status}).")
        except Exception as e:
            print(f"Failed to send WhatsApp alert #{idx}: {e}")


def check_board_status(page, label):
    """Accurately detects green checkmark vs red X for CPCB: or KSPCB:."""
    other_label = "KSPCB:" if label == "CPCB:" else "CPCB:"
    tag_id = label.replace(":", "").lower()

    js_result = page.evaluate("""({ label, otherLabel, tagId }) => {
        const all = Array.from(document.querySelectorAll('*'));
        let target = null;
        for (const el of all) {
            const txt = (el.innerText || '').trim();
            if (txt.includes(label) && !txt.includes(otherLabel) && txt.length < 40) {
                if (!target || el.innerHTML.length < target.innerHTML.length) {
                    target = el;
                }
            }
        }
        if (!target) return { status: 'missing', info: 'Label not found' };

        let container = target;
        if (!target.querySelector('i, svg, img')) {
            const next = target.nextElementSibling;
            if (next && (['I', 'SVG', 'IMG'].includes(next.tagName) || next.querySelector('i, svg, img'))) {
                container = next;
            } else if (target.parentElement && !(target.parentElement.innerText || '').includes(otherLabel)) {
                container = target.parentElement;
            }
        }

        container.setAttribute('data-board-check', tagId);

        const icon = container.querySelector('i, svg, img') || container;
        const cls = (typeof icon.className === 'string' ? icon.className : (icon.className?.baseVal || '')).toLowerCase();
        const src = (icon.getAttribute('src') || '').toLowerCase();
        const alt = (icon.getAttribute('alt') || icon.getAttribute('title') || '').toLowerCase();
        const text = (container.innerText || '');
        const outer = (icon.outerHTML || '').toLowerCase();

        const combined = `${cls} ${src} ${alt} ${outer}`;
        const failWords = [
            'fa-times', 'fa-close', 'fa-window-close', 'fa-xmark', 'fa-square-xmark',
            'fa-ban', 'text-danger', 'bg-danger', 'cross', 'fail', 'error', 'offline'
        ];
        const passWords = [
            'fa-check', 'fa-circle-check', 'text-success', 'bg-success', 'tick'
        ];

        if (/[❌✖✗✘]/.test(text) || failWords.some(w => combined.includes(w))) {
            return { status: 'fail', info: `DOM fail (${cls || src || text.trim()})` };
        }
        if (/[✅✔✓]/.test(text) || passWords.some(w => combined.includes(w))) {
            return { status: 'pass', info: `DOM pass (${cls || src || text.trim()})` };
        }
        return { status: 'unknown', info: `cls='${cls}' src='${src}'` };
    }""", {"label": label, "otherLabel": other_label, "tagId": tag_id})

    if js_result["status"] == "fail":
        return False, js_result["info"]
    if js_result["status"] == "pass":
        return True, js_result["info"]

    # Pixel-color fallback (detects red X vs green checkmark for any SVG/PNG/emoji)
    try:
        loc = page.locator(f"[data-board-check='{tag_id}']").first
        png_bytes = loc.screenshot(timeout=5000)
        img = PIL.Image.open(io.BytesIO(png_bytes)).convert("RGB")
        red_px = 0
        green_px = 0
        for r, g, b in img.getdata():
            if r > 150 and r > g * 1.4 and r > b * 1.4:
                red_px += 1
            elif g > 110 and g > r * 1.25:
                green_px += 1
        info = f"Pixel check (red={red_px}, green={green_px}) | {js_result['info']}"
        if red_px > green_px and red_px > 10:
            return False, info
        if green_px > red_px and green_px > 10:
            return True, info
        return False, f"No green checkmark found | {info}"
    except Exception as e:
        return False, f"Fallback error: {e} | {js_result['info']}"


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
    limit_alerts = []
    system_alerts = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        if not authenticate(context, page):
            browser.close()
            print("Login failed. Exiting without sending an alert.")
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

        # 2. Check IoT Status
        if "IoT: Online" not in body_text:
            system_alerts.append("IoT Device Status is <b>OFFLINE</b>")

        # 3. Check CPCB/KSPCB Transmission (DOM + Pixel Color Verification)
        cpcb_ok, cpcb_info = check_board_status(page, "CPCB:")
        kspcb_ok, kspcb_info = check_board_status(page, "KSPCB:")
        print(f"CPCB Status: {'OK' if cpcb_ok else 'FAILED'} | {cpcb_info}")
        print(f"KSPCB Status: {'OK' if kspcb_ok else 'FAILED'} | {kspcb_info}")

        if not cpcb_ok:
            system_alerts.append("Data NOT sending to <b>CPCB</b> (Red X / Transmission Failed)")
        if not kspcb_ok:
            system_alerts.append("Data NOT sending to <b>KSPCB</b> (Red X / Transmission Failed)")

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

    checked_at = datetime.now(IST).strftime("%Y-%m-%d %I:%M:%S %p IST")

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

    # Dispatch Both HTML Email & WhatsApp Alert
    send_html_alert(final_subject, html_content, plain_text)
    send_whatsapp_alert(final_subject, limit_alerts, system_alerts, checked_at)


if __name__ == "__main__":
    try:
        check_dashboard()
    except Exception:
        print("CRITICAL ERROR DURING RUN:")
        traceback.print_exc()
        exit(1)
