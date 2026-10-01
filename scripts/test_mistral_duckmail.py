# -*- coding: utf-8 -*-
import json
import random
import re
import string
import sys
import time
import requests
from camoufox.sync_api import Camoufox

DUCK_BASE = "https://api.duckmail.sbs"

def get_duckmail_account():
    r = requests.get(f"{DUCK_BASE}/domains")
    r.raise_for_status()
    domains = r.json().get("hydra:member", [])
    if not domains:
        raise RuntimeError("No duckmail domains available")
    domain = domains[0]["domain"]
    
    rand_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    address = f"mistral_{rand_id}@{domain}"
    password = "Pass_" + "".join(random.choices(string.ascii_letters + string.digits, k=10)) + "!@"
    
    print(f"[DuckMail] Creating account: {address}")
    r2 = requests.post(f"{DUCK_BASE}/accounts", json={"address": address, "password": password})
    r2.raise_for_status()
    
    r3 = requests.post(f"{DUCK_BASE}/token", json={"address": address, "password": password})
    r3.raise_for_status()
    token = r3.json()["token"]
    print(f"[DuckMail] Got token for {address}")
    return address, password, token

def poll_duckmail_code(token: str, timeout: int = 90):
    headers = {"Authorization": f"Bearer {token}"}
    start = time.time()
    print("[DuckMail] Waiting for verification email...")
    while time.time() - start < timeout:
        r = requests.get(f"{DUCK_BASE}/messages", headers=headers)
        if r.status_code == 200:
            data = r.json()
            msgs = data.get("hydra:member") or data.get("member") or []
            if msgs:
                msg_id = msgs[0]["id"]
                r_detail = requests.get(f"{DUCK_BASE}/messages/{msg_id}", headers=headers)
                detail = r_detail.json()
                subject = detail.get("subject", "")
                text = detail.get("text", "") or detail.get("intro", "")
                html = detail.get("html", "")
                print(f"[DuckMail] Received email! Subject: {subject}")
                print(f"[DuckMail] Email text snippet:\n{text[:400]}")
                with open("log/mistral_verification_email.json", "w", encoding="utf-8") as f:
                    json.dump(detail, f, ensure_ascii=False, indent=2)
                
                # Check for 6-digit code
                # Usually: "Your verification code is 123456" or similar
                codes = re.findall(r"\b\d{6}\b", f"{subject}\n{text}\n{html}")
                if codes:
                    print(f"[DuckMail] Extracted 6-digit code: {codes[0]}")
                    return codes[0]
        time.sleep(3)
    return None

def main():
    email, email_pwd, mail_token = get_duckmail_account()
    mistral_account_password = "Mistral#Pass123!@#"
    
    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        def on_resp(r):
            if "registration" in r.url and r.status >= 400:
                print(f"[HTTP {r.status}] {r.url}")
                try:
                    data = r.json()
                    print("[Registration UI Messages]:", data.get("ui", {}).get("messages"))
                    for n in data.get("ui", {}).get("nodes", []):
                        if n.get("messages"):
                            print(f"[Node {n.get('attributes', {}).get('name')} Messages]:", n.get("messages"))
                except Exception as e:
                    print("[HTTP Body Err]", e)
        page.on("response", on_resp)
        print("[Step 1] Navigating to https://admin.mistral.ai/organization/api-keys ...")
        page.goto("https://admin.mistral.ai/organization/api-keys", timeout=60000)
        time.sleep(4)
        print("[Step 1] URL after redirect:", page.url)
        
        # Step 2: Fill Email
        print(f"[Step 2] Filling email: {email}")
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
            print("[Error] Password input not shown!")
            page.screenshot(path="log/err_step2_email.png")
            return
            
        print("[Step 3] Password field appeared. Filling credentials...")
        page.locator('input[name="password"]').fill(mistral_account_password)
        page.locator('input[name="firstName"]').fill("Alex")
        last_inp = page.locator('input[name="lastName"]')
        last_inp.fill("Morgan")
        time.sleep(1)
        
        # Click Signup
        signup_btn = page.locator('button:has-text("Signup")')
        print("[Step 3] Clicking Signup button...")
        signup_btn.click()
        time.sleep(2)
        
        # Check if form still there
        if page.locator('input[name="password"]').count() > 0:
            print("[Step 3] Trying Enter on lastName...")
            last_inp.press("Enter")
            time.sleep(2)
        
        # Step 4: Wait for OTP input
        print("[Step 4] Waiting for verification code input...")
        found_otp = False
        for i in range(15):
            time.sleep(1)
            if page.locator('input[data-input-otp="true"]').count() > 0 or page.locator('input[autocomplete="one-time-code"]').count() > 0:
                found_otp = True
                print(f"[Step 4] OTP input appeared after {i+1}s!")
                break
            toasts = page.locator('[data-sonner-toast]').all()
            for t in toasts:
                try:
                    print(f"[Toast] {t.inner_text()}")
                except Exception:
                    pass
        
        if not found_otp:
            print("[Error] OTP input not found after signup!")
            page.screenshot(path="log/err_step4_otp.png")
            return
            
        print("[Step 4] Polling email for code...")
        code = poll_duckmail_code(mail_token, timeout=90)
        if not code:
            print("[Error] Failed to receive verification code in time!")
            page.screenshot(path="log/err_step4_nocode.png")
            return
            
        print(f"[Step 5] Entering verification code: {code}")
        otp_inp = page.locator('input[data-input-otp="true"]')
        if otp_inp.count() == 0:
            otp_inp = page.locator('input[autocomplete="one-time-code"]')
        
        # Use fill and evaluate to set value if needed
        otp_inp.fill(code)
        time.sleep(1)
        otp_inp.press("Enter")
        
        # Step 6: Wait for redirect to admin/console
        print("[Step 6] Waiting for redirect after OTP...")
        for i in range(30):
            time.sleep(2)
            cur_url = page.url
            print(f"[{i*2}s] Current URL: {cur_url}")
            if "admin.mistral.ai" in cur_url or "console.mistral.ai" in cur_url:
                print(f"[Success] Reached Mistral console/admin: {cur_url}")
                break
            # Check for error messages
            toasts = page.locator('[data-sonner-toast]').all()
            for t in toasts:
                try:
                    print(f"[Toast] {t.inner_text()}")
                except Exception:
                    pass
        
        time.sleep(5)
        print("Final URL:", page.url)
        print("Final Title:", page.title())
        page.screenshot(path="log/mistral_after_auth.png")
        with open("log/mistral_after_auth.html", "w", encoding="utf-8") as f:
            f.write(page.content())
            
        # Check elements on page
        print("Page text snippet:\n", page.locator('body').inner_text()[:1200])

if __name__ == "__main__":
    main()
