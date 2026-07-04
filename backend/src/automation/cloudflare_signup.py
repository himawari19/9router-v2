#!/usr/bin/env python3
"""Cloudflare account auto-signup via Playwright + Ammail OTP verification.
Outputs JSON lines to stdout: {"step": "..."} or {"status": "success", "api_key": "...", "account_id": "..."}
"""

import sys
import json
import argparse
import time
import random
import string
import re
import urllib.request
import urllib.parse
from pathlib import Path

# ── Stdout JSON helpers ────────────────────────────────────────────────────────
def emit(obj):
    print(json.dumps(obj), flush=True)

def step(msg):
    emit({"step": msg})

def success(api_key, account_id, email):
    emit({"status": "success", "api_key": api_key, "account_id": account_id, "email": email})

def error(msg):
    emit({"status": "error", "error": msg})
    sys.exit(1)

# ── Ammail helpers ─────────────────────────────────────────────────────────────
def ammail_request(base_url, api_key, path, method="GET", data=None):
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("X-API-Key", api_key)
    req.add_header("Content-Type", "application/json")
    if data:
        req.data = json.dumps(data).encode()
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

def generate_temp_email(base_url, api_key, domain):
    """Create a fresh inbox via Ammail and return the email address."""
    alias = "cf" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    full_email = f"{alias}@{domain}"
    ammail_request(base_url, api_key, "/inboxes", method="POST", data={"email": full_email})
    return full_email

def wait_for_verification_link(base_url, api_key, email, timeout=120):
    """Poll Ammail inbox until we find a Cloudflare verification email."""
    step(f"Menunggu email verifikasi Cloudflare di {email}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data = ammail_request(base_url, api_key, f"/inboxes/{urllib.parse.quote(email)}/messages")
            messages = data.get("messages", [])
            for msg in messages:
                subject = msg.get("subject", "")
                body = msg.get("body", msg.get("html", msg.get("text", "")))
                if "cloudflare" in subject.lower() or "verify" in subject.lower():
                    # Extract verification link
                    links = re.findall(r'https://[^\s\'"<>]+verify[^\s\'"<>]+', body)
                    if links:
                        return links[0]
                    links = re.findall(r'https://dash\.cloudflare\.com/[^\s\'"<>]+', body)
                    if links:
                        return links[0]
        except Exception as e:
            pass
        time.sleep(5)
    return None

# ── Cloudflare API helpers ─────────────────────────────────────────────────────
CF_API = "https://api.cloudflare.com/client/v4"

def cf_api(path, token=None, global_key=None, email=None, method="GET", data=None):
    url = CF_API + path
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    elif global_key and email:
        req.add_header("X-Auth-Key", global_key)
        req.add_header("X-Auth-Email", email)
    req.add_header("Content-Type", "application/json")
    if data:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise Exception(f"CF API {path} → {e.code}: {body}")

# ── Password generator ─────────────────────────────────────────────────────────
def gen_password():
    chars = string.ascii_letters + string.digits + "!@#$"
    pw = (random.choice(string.ascii_uppercase) +
          random.choice(string.digits) +
          random.choice("!@#$") +
          "".join(random.choices(chars, k=9)))
    return "".join(random.sample(pw, len(pw)))

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--ammail-base-url", required=True)
    parser.add_argument("--ammail-api-key", required=True)
    parser.add_argument("--ammail-domain", required=True)
    parser.add_argument("--profiles-dir", default="profiles/cloudflare")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--proxy-server")
    parser.add_argument("--proxy-user")
    parser.add_argument("--proxy-pass")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        error("Playwright tidak terinstall. Jalankan: pip install playwright && playwright install chromium")

    Path(args.profiles_dir).mkdir(parents=True, exist_ok=True)

    step("Membuka browser...")

    with sync_playwright() as pw:
        launch_opts = {
            "headless": args.headless,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        if args.proxy_server:
            proxy = {"server": args.proxy_server}
            if args.proxy_user:
                proxy["username"] = args.proxy_user
            if args.proxy_pass:
                proxy["password"] = args.proxy_pass
            launch_opts["proxy"] = proxy

        browser = pw.chromium.launch(**launch_opts)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        try:
            step("Membuka halaman registrasi Cloudflare...")
            page.goto("https://dash.cloudflare.com/sign-up", wait_until="networkidle", timeout=30000)
            time.sleep(2)

            # Fill email
            step("Mengisi email...")
            email_input = page.locator("input[name='email'], input[type='email'], #email").first
            email_input.wait_for(state="visible", timeout=10000)
            email_input.fill(args.email)
            time.sleep(0.5)

            # Fill password
            step("Mengisi password...")
            pw_inputs = page.locator("input[type='password']")
            pw_count = pw_inputs.count()
            if pw_count >= 1:
                pw_inputs.nth(0).fill(args.password)
            if pw_count >= 2:
                pw_inputs.nth(1).fill(args.password)
            time.sleep(0.5)

            # Handle Turnstile / CAPTCHA — wait for it to auto-solve or timeout
            step("Menunggu CAPTCHA Cloudflare Turnstile...")
            time.sleep(4)  # Turnstile biasanya auto-solve dalam 3-5 detik

            # Submit form
            step("Submit form registrasi...")
            submit = page.locator("button[type='submit'], input[type='submit'], button:has-text('Create Account'), button:has-text('Sign Up'), button:has-text('Get started')").first
            submit.click()
            time.sleep(3)

            # Check if redirected to verify email page or dashboard
            current_url = page.url
            step(f"Setelah submit: {current_url}")

            # Wait for email verification
            verify_link = wait_for_verification_link(
                args.ammail_base_url,
                args.ammail_api_key,
                args.email,
                timeout=120
            )

            if not verify_link:
                # Maybe already logged in (email already exists?) — try login
                step("Email verifikasi tidak diterima, mencoba login langsung...")
                page.goto("https://dash.cloudflare.com/login", wait_until="networkidle", timeout=20000)
                time.sleep(2)
                page.locator("input[name='email'], input[type='email']").first.fill(args.email)
                page.locator("input[type='password']").first.fill(args.password)
                page.locator("button[type='submit']").first.click()
                time.sleep(4)
            else:
                step("Email verifikasi diterima! Membuka link verifikasi...")
                page.goto(verify_link, wait_until="networkidle", timeout=30000)
                time.sleep(3)

                # After verification, might need to login
                if "login" in page.url or "sign-in" in page.url:
                    step("Verifikasi berhasil. Login ke dashboard...")
                    page.locator("input[name='email'], input[type='email']").first.fill(args.email)
                    page.locator("input[type='password']").first.fill(args.password)
                    page.locator("button[type='submit']").first.click()
                    time.sleep(4)

            # Navigate to dashboard
            step("Membuka Cloudflare Dashboard...")
            page.goto("https://dash.cloudflare.com/", wait_until="networkidle", timeout=30000)
            time.sleep(2)

            # Extract account_id from URL
            account_id = ""
            url_match = re.search(r"/([a-f0-9]{32})", page.url)
            if url_match:
                account_id = url_match.group(1)
                step(f"Account ID terdeteksi: {account_id[:8]}...")
            else:
                # Try getting account ID from API via cookies
                step("Mengambil Account ID dari API...")

            # Navigate to API tokens page
            step("Membuka halaman API Tokens...")
            page.goto("https://dash.cloudflare.com/profile/api-tokens", wait_until="networkidle", timeout=30000)
            time.sleep(2)

            # Extract Global API Key — click "View" button
            step("Mengambil Global API Key...")
            # Try to find "View" button next to Global API Key section
            view_btn = page.locator("button:has-text('View'), a:has-text('View')").first
            if view_btn.is_visible():
                view_btn.click()
                time.sleep(1)
                # Input password to confirm
                pw_confirm = page.locator("input[type='password']")
                if pw_confirm.is_visible():
                    pw_confirm.fill(args.password)
                    page.locator("button:has-text('View'), button[type='submit']").first.click()
                    time.sleep(2)

            # Get the Global API Key value from the page
            global_api_key = ""
            key_input = page.locator("input[data-testid='global-api-key'], input[readonly]").first
            if key_input.is_visible():
                global_api_key = key_input.input_value()

            if not global_api_key:
                # Fallback: create a regular API token instead
                step("Global API Key tidak tersedia, membuat Workers AI Token...")
                page.goto("https://dash.cloudflare.com/profile/api-tokens", wait_until="networkidle", timeout=20000)
                time.sleep(1)

                # Click "Create Token"
                create_btn = page.locator("button:has-text('Create Token'), a:has-text('Create Token')").first
                if create_btn.is_visible():
                    create_btn.click()
                    time.sleep(2)
                    # Use "Workers AI" template if available
                    template = page.locator("button:has-text('Workers AI'), a:has-text('Workers AI')").first
                    if template.is_visible():
                        template.click()
                        time.sleep(1)
                    # Submit
                    page.locator("button:has-text('Continue to summary'), button[type='submit']").first.click()
                    time.sleep(2)
                    page.locator("button:has-text('Create Token'), button[type='submit']").first.click()
                    time.sleep(2)
                    # Get token value
                    token_el = page.locator("code, [data-testid='token-value'], input[readonly]").first
                    if token_el.is_visible():
                        global_api_key = token_el.text_content() or token_el.input_value()

            # If we still don't have an API key but have account_id, try via Cloudflare API
            # using the session cookies
            if not global_api_key and not account_id:
                error("Tidak dapat mengambil API Key dari akun Cloudflare")
                return

            # If no account_id yet, get it via API
            if not account_id and global_api_key:
                try:
                    step("Mengambil Account ID via Cloudflare API...")
                    result = cf_api("/accounts?per_page=1", global_key=global_api_key, email=args.email)
                    if result.get("success") and result.get("result"):
                        account_id = result["result"][0]["id"]
                except Exception as e:
                    step(f"Gagal ambil account_id: {e}")

            browser.close()
            step("Browser ditutup. Menyimpan kredensial...")
            success(global_api_key, account_id, args.email)

        except Exception as e:
            try:
                browser.close()
            except:
                pass
            error(f"Automation error: {e}")

if __name__ == "__main__":
    main()
