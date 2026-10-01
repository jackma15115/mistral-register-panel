# -*- coding: utf-8 -*-
import json
import re
import sys
import time
import requests
from camoufox.sync_api import Camoufox

TIMAIL_BASE = "https://anymail.77669876.xyz"
TIMAIL_DOMAIN = "keldie.cyou"
TIMAIL_MODE = "subdomain"

def create_timail_account():
    print(f"[TiMail] Requesting mailbox from {TIMAIL_BASE} (mode={TIMAIL_MODE}, domain={TIMAIL_DOMAIN})...", flush=True)
    r = requests.post(f"{TIMAIL_BASE}/mailbox", json={"type": TIMAIL_MODE, "domain": TIMAIL_DOMAIN}, timeout=30)
    r.raise_for_status()
    data = r.json()
    email = data["mailbox"]
    token = data["token"]
    print(f"[TiMail] Mailbox created: {email}, token: {token}", flush=True)
    return email, token

def poll_timail_code(token: str, timeout: int = 120):
    headers = {"Authorization": token}
    start = time.time()
    print("[TiMail] Waiting for verification email...", flush=True)
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{TIMAIL_BASE}/messages", headers=headers, timeout=20)
            if r.status_code == 200:
                msgs = r.json().get("messages", [])
                if msgs:
                    msg = msgs[0]
                    msg_id = msg.get("_id") or msg.get("id")
                    subject = msg.get("subject", "")
                    preview = msg.get("bodyPreview", "")
                    print(f"[TiMail] Received message! id={msg_id}, subject={subject}", flush=True)
                    
                    full_text = f"{subject}\n{preview}"
                    if msg_id:
                        r_detail = requests.get(f"{TIMAIL_BASE}/messages/{msg_id}", headers=headers, timeout=20)
                        if r_detail.status_code == 200:
                            detail = r_detail.json()
                            html = detail.get("bodyHtml", "") or detail.get("html", "")
                            text = detail.get("bodyPreview", "") or detail.get("text", "")
                            full_text += f"\n{text}\n{html}"
                    
                    # Method A: check verification?code=(\d{6})
                    m_link = re.search(r"code=(\d{6})", full_text)
                    if m_link:
                        code = m_link.group(1)
                        print(f"[TiMail] Extracted 6-digit code from link: {code}", flush=True)
                        return code
                    
                    # Method B: check 6 digits in html/text
                    m_code = re.findall(r"\b(\d{6})\b", full_text)
                    for c in m_code:
                        if c not in ("599px", "155520"):
                            print(f"[TiMail] Extracted 6-digit code: {c}", flush=True)
                            return c
        except Exception as e:
            print("[TiMail] Error polling messages:", e, flush=True)
        time.sleep(3)
    return None

def main():
    email, token = create_timail_account()
    account_password = "Mistral#Pass123!@#"
    
    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        
        extracted_keys = []
        def on_resp(r):
            url = r.url
            if "api-key" in url.lower() or "keys" in url.lower():
                try:
                    if r.status in (200, 201):
                        txt = r.text()
                        print(f"[API Response {r.status}] {url}\n{txt[:500]}", flush=True)
                        # Check for key pattern
                        keys = re.findall(r'\"key\"\s*:\s*\"([^\"]+)\"', txt)
                        if keys:
                            extracted_keys.extend(keys)
                            print(f"[Found Key via API Intercept]: {keys}", flush=True)
                except Exception:
                    pass
        page.on("response", on_resp)
        
        print("[Step 1] Navigating to https://admin.mistral.ai/organization/api-keys ...", flush=True)
        page.goto("https://admin.mistral.ai/organization/api-keys", timeout=60000)
        time.sleep(4)
        print("[Step 1] Redirect URL:", page.url, flush=True)
        
        # Step 2: Fill Email
        print(f"[Step 2] Filling email: {email}", flush=True)
        email_inp = page.locator('input[name="email"]')
        email_inp.fill(email)
        time.sleep(1)
        email_inp.press("Enter")
        
        # Wait for password field
        found_pwd = False
        for _ in range(12):
            time.sleep(1)
            if page.locator('input[name="password"]').count() > 0:
                found_pwd = True
                break
        
        if not found_pwd:
            print("[Error] Password input not shown after entering email!", flush=True)
            page.screenshot(path="log/err_step2_email.png")
            return
            
        print("[Step 3] Password field appeared. Filling credentials...", flush=True)
        page.locator('input[name="password"]').fill(account_password)
        page.locator('input[name="firstName"]').fill("Alex")
        last_inp = page.locator('input[name="lastName"]')
        last_inp.fill("Morgan")
        time.sleep(1)
        
        # Click Signup
        signup_btn = page.locator('button:has-text("Signup")')
        print("[Step 3] Clicking Signup button...", flush=True)
        signup_btn.click()
        time.sleep(2)
        
        # Check if form still there, try Enter on lastName
        if page.locator('input[name="password"]').count() > 0:
            print("[Step 3] Form still present, pressing Enter on lastName...", flush=True)
            last_inp.press("Enter")
            time.sleep(2)
            
        # Step 4: Wait for OTP input
        print("[Step 4] Waiting for verification code input...", flush=True)
        found_otp = False
        for i in range(15):
            time.sleep(1)
            if page.locator('input[data-input-otp="true"]').count() > 0 or page.locator('input[autocomplete="one-time-code"]').count() > 0:
                found_otp = True
                print(f"[Step 4] OTP input appeared after {i+1}s!", flush=True)
                break
            toasts = page.locator('[data-sonner-toast]').all()
            for t in toasts:
                try:
                    print(f"[Toast] {t.inner_text()}", flush=True)
                except Exception:
                    pass
        
        if not found_otp:
            print("[Error] OTP input not found after signup!", flush=True)
            page.screenshot(path="log/err_timail_otp.png")
            with open("log/err_timail_otp.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            return
            
        print("[Step 4] Polling TiMail for 6-digit code...", flush=True)
        code = poll_timail_code(token, timeout=120)
        if not code:
            print("[Error] Did not receive verification code!", flush=True)
            page.screenshot(path="log/err_timail_nocode.png")
            return
            
        print(f"[Step 5] Entering verification code: {code}", flush=True)
        otp_inp = page.locator('input[data-input-otp="true"]')
        if otp_inp.count() == 0:
            otp_inp = page.locator('input[autocomplete="one-time-code"]')
        
        # Type each digit or fill
        otp_inp.focus()
        otp_inp.fill(code)
        time.sleep(1)
        otp_inp.press("Enter")
        
        # Step 6: Wait for redirect
        print("[Step 6] Waiting for redirect after OTP submission...", flush=True)
        for i in range(35):
            time.sleep(2)
            cur_url = page.url
            print(f"[{i*2}s] Current URL: {cur_url}", flush=True)
            if "admin.mistral.ai" in cur_url or "console.mistral.ai" in cur_url:
                print(f"[Success] Reached Mistral admin/console: {cur_url}", flush=True)
                break
            toasts = page.locator('[data-sonner-toast]').all()
            for t in toasts:
                try:
                    print(f"[Toast] {t.inner_text()}", flush=True)
                except Exception:
                    pass
        
        time.sleep(5)
        print("Final URL:", page.url, flush=True)
        print("Final Title:", page.title(), flush=True)
        page.screenshot(path="log/mistral_timail_success.png")
        with open("log/mistral_timail_success.html", "w", encoding="utf-8") as f:
            f.write(page.content())
            
        # Inspect page content & look for API key creation elements
        print("\n--- Current Page Text Snippet ---", flush=True)
        try:
            print(page.locator('body').inner_text()[:1500], flush=True)
        except Exception:
            pass
        
        # Check buttons and links
        print("\n--- Buttons found ---", flush=True)
        for b in page.locator('button').all():
            try:
                t = b.inner_text().strip()
                if t:
                    print(f"  button: '{t}'", flush=True)
            except Exception:
                pass
                
        # Check if we are on api-keys page or need to navigate or create key
        print("\n[Step 7] Checking API key creation...", flush=True)
        create_btn = page.locator('button:has-text("Create new key"), button:has-text("Create key"), button:has-text("Add key")')
        if create_btn.count() == 0:
            # Maybe navigate explicitly to https://admin.mistral.ai/organization/api-keys
            print("Navigating explicitly to https://admin.mistral.ai/organization/api-keys ...", flush=True)
            page.goto("https://admin.mistral.ai/organization/api-keys", timeout=30000)
            time.sleep(5)
            page.screenshot(path="log/mistral_admin_keys_page.png")
            create_btn = page.locator('button:has-text("Create new key"), button:has-text("Create key"), button:has-text("Add key")')
            
        if create_btn.count() > 0:
            print("Clicking Create Key button...", flush=True)
            create_btn.first.click()
            time.sleep(3)
            page.screenshot(path="log/mistral_key_modal.png")
            
            # Fill key name if input exists
            name_inp = page.locator('input[name="name"], input[placeholder*="name" i], input[type="text"]')
            if name_inp.count() > 0:
                name_inp.first.fill("mistral_key_1")
                time.sleep(1)
            confirm_btn = page.locator('button:has-text("Create"), button:has-text("Save"), button:has-text("Confirm")')
            if confirm_btn.count() > 0:
                confirm_btn.last.click()
                time.sleep(5)
                page.screenshot(path="log/mistral_key_created.png")
                with open("log/mistral_key_created.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                print("Page text after key creation:\n", page.locator('body').inner_text()[:1500], flush=True)
        else:
            print("Create key button not found yet. Current URL:", page.url, flush=True)

        if extracted_keys:
            print(f"\n==========================================")
            print(f"[SUCCESS RESULT]")
            print(f"Account: {email}----{account_password}")
            print(f"API Key: {extracted_keys[0]}")
            print(f"Formatted: {email}----{account_password}----{extracted_keys[0]}")
            print(f"==========================================")
            with open("accounts/mistral_account_keys.txt", "a", encoding="utf-8") as f:
                f.write(f"{email}----{account_password}----{extracted_keys[0]}\n")

if __name__ == "__main__":
    main()
