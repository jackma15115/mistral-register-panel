# -*- coding: utf-8 -*-
import json
import random
import re
import string
import sys
import time
import requests
from camoufox.sync_api import Camoufox

MAIL_TM_BASE = "https://api.mail.tm"

def get_mail_tm_account():
    r = requests.get(f"{MAIL_TM_BASE}/domains")
    r.raise_for_status()
    domains = r.json().get("hydra:member", [])
    if not domains:
        raise RuntimeError("No mail.tm domains available")
    domain = domains[0]["domain"]
    
    rand_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    address = f"mistral_user_{rand_id}@{domain}"
    password = "P@" + "".join(random.choices(string.ascii_letters + string.digits, k=12))
    
    print(f"[Mail.tm] Creating account: {address}")
    r2 = requests.post(f"{MAIL_TM_BASE}/accounts", json={"address": address, "password": password})
    r2.raise_for_status()
    
    r3 = requests.post(f"{MAIL_TM_BASE}/token", json={"address": address, "password": password})
    r3.raise_for_status()
    token = r3.json()["token"]
    print(f"[Mail.tm] Got token for {address}")
    return address, password, token

def poll_mail_tm_code(token: str, timeout: int = 90):
    headers = {"Authorization": f"Bearer {token}"}
    start = time.time()
    print("[Mail.tm] Waiting for verification email...")
    while time.time() - start < timeout:
        r = requests.get(f"{MAIL_TM_BASE}/messages", headers=headers)
        if r.status_code == 200:
            msgs = r.json().get("hydra:member", [])
            if msgs:
                msg_id = msgs[0]["id"]
                r_detail = requests.get(f"{MAIL_TM_BASE}/messages/{msg_id}", headers=headers)
                detail = r_detail.json()
                subject = detail.get("subject", "")
                text = detail.get("text", "") or detail.get("intro", "")
                html = detail.get("html", "")
                print(f"[Mail.tm] Received email! Subject: {subject}")
                print(f"[Mail.tm] Email text snippet:\n{text[:400]}")
                with open("log/mistral_verification_email.json", "w", encoding="utf-8") as f:
                    json.dump(detail, f, ensure_ascii=False, indent=2)
                # Look for 6-digit code
                # In subject or body
                codes = re.findall(r"\b\d{6}\b", f"{subject}\n{text}\n{html}")
                if codes:
                    print(f"[Mail.tm] Extracted 6-digit code: {codes[0]}")
                    return codes[0]
        time.sleep(3)
    return None

def main():
    email, email_pwd, mail_token = get_mail_tm_account()
    mistral_account_password = "Mistral#Pass" + "".join(random.choices(string.ascii_letters + string.digits, k=6))
    
    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        page.on("response", lambda r: print(f"[HTTP {r.status}] {r.url}") if "auth.mistral.ai" in r.url or "kratos" in r.url else None)
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
        page.locator('input[name="password"]').fill("Mistral_Pass123!@#")
        page.locator('input[name="firstName"]').fill("Alex")
        last_inp = page.locator('input[name="lastName"]')
        last_inp.fill("Morgan")
        time.sleep(1)
        
        # Click Signup
        signup_btn = page.locator('button:has-text("Signup")')
        print("[Step 3] Clicking Signup button...")
        signup_btn.click()
        time.sleep(2)
        
        # Check if form submitted; if not try enter
        if page.locator('input[name="password"]').count() > 0:
            print("[Step 3] Trying press Enter on lastName...")
            last_inp.press("Enter")
            time.sleep(2)
        
        # Step 4: Wait for OTP input
        print("[Step 4] Waiting for verification code input...")
        found_otp = False
        for i in range(20):
            time.sleep(1)
            if page.locator('input[data-input-otp="true"]').count() > 0 or page.locator('input[autocomplete="one-time-code"]').count() > 0:
                found_otp = True
                print(f"[Step 4] OTP input appeared after {i+1}s!")
                break
            # Check for error toasts
            toasts = page.locator('[data-sonner-toast]').all()
            for t in toasts:
                try:
                    print(f"[Toast] {t.inner_text()}")
                except Exception:
                    pass
        
        if not found_otp:
            print("[Error] OTP input not found after signup!")
            page.screenshot(path="log/err_step4_otp.png")
            with open("log/err_step4.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            return
            
        print("[Step 4] OTP input found. Polling email for code...")
        code = poll_mail_tm_code(mail_token, timeout=90)
        if not code:
            print("[Error] Failed to receive verification code in time!")
            return
            
        print(f"[Step 5] Entering verification code: {code}")
        otp_inp = page.locator('input[data-input-otp="true"]')
        if otp_inp.count() == 0:
            otp_inp = page.locator('input[autocomplete="one-time-code"]')
        otp_inp.focus()
        otp_inp.fill(code)
        time.sleep(2)
        # In case it needs Enter or submit
        otp_inp.press("Enter")
        
        # Step 6: Wait for post-verification redirect or onboarding
        print("[Step 6] Waiting for redirect after OTP...")
        for i in range(25):
            time.sleep(2)
            cur_url = page.url
            print(f"[{i*2}s] Current URL: {cur_url}")
            if "admin.mistral.ai" in cur_url or "console.mistral.ai" in cur_url:
                print(f"[Success] Reached Mistral console/admin: {cur_url}")
                break
        
        time.sleep(5)
        print("Final URL:", page.url)
        print("Final Title:", page.title())
        page.screenshot(path="log/mistral_after_auth.png")
        with open("log/mistral_after_auth.html", "w", encoding="utf-8") as f:
            f.write(page.content())
            
        # Check elements on page
        print("Page text snippet:\n", page.locator('body').inner_text()[:1000])

if __name__ == "__main__":
    main()
