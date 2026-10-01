# -*- coding: utf-8 -*-
import json
import re
import sys
import time
from camoufox.sync_api import Camoufox

EMAIL = "xnkguinzgc@lpxeeqjo.keldie.cyou"
PASSWORD = "Mistral#Pass123!@#"

def main():
    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        
        def on_resp(r):
            url = r.url
            if "trpc" in url or "apiKeys" in url or "key" in url:
                print(f"[API Response {r.status}] {url}", flush=True)
                try:
                    if r.status in (200, 201):
                        txt = r.text()
                        print(f"[Body]: {txt[:500]}", flush=True)
                except Exception:
                    pass
        page.on("response", on_resp)
        
        print(f"Navigating to https://admin.mistral.ai/organization/api-keys ...", flush=True)
        page.goto("https://admin.mistral.ai/organization/api-keys", timeout=60000)
        time.sleep(4)
        
        # Check if redirected to login
        if "login" in page.url or page.locator('input[name="email"]').count() > 0:
            print("Entering email...", flush=True)
            email_inp = page.locator('input[name="email"]')
            email_inp.fill(EMAIL)
            time.sleep(1)
            email_inp.press("Enter")
            time.sleep(3)
            
            pwd_inp = page.locator('input[name="password"]')
            if pwd_inp.count() > 0:
                print("Entering password...", flush=True)
                pwd_inp.fill(PASSWORD)
                time.sleep(1)
                # Submit login
                login_btn = page.locator('button:has-text("Login"), button:has-text("Continue"), button:has-text("Sign in")')
                if login_btn.count() > 0:
                    login_btn.first.click()
                else:
                    pwd_inp.press("Enter")
                time.sleep(5)
        
        # Now check if on admin keys page
        print("Current URL:", page.url, flush=True)
        if "organization/api-keys" not in page.url:
            print("Navigating to https://admin.mistral.ai/organization/api-keys ...", flush=True)
            page.goto("https://admin.mistral.ai/organization/api-keys", timeout=30000)
            time.sleep(4)
            
        page.screenshot(path="log/admin_keys_before_click.png")
        
        # Look for "+ New key" button
        new_key_btn = page.locator('button:has-text("New key")')
        print(f"New key button count: {new_key_btn.count()}", flush=True)
        if new_key_btn.count() > 0:
            print("Clicking '+ New key' button...", flush=True)
            new_key_btn.first.click()
            time.sleep(3)
            page.screenshot(path="log/admin_key_modal.png")
            with open("log/admin_key_modal.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            print("Modal Page text snippet:\n", page.locator('body').inner_text()[:1500], flush=True)
            
            # Print all inputs in modal
            for inp in page.locator('input').all():
                try:
                    print(f"Input: name={inp.get_attribute('name')} type={inp.get_attribute('type')} placeholder={inp.get_attribute('placeholder')}", flush=True)
                except Exception:
                    pass
            # Print buttons in modal
            for b in page.locator('button').all():
                try:
                    txt = b.inner_text().strip()
                    if txt:
                        print(f"Button: '{txt}'", flush=True)
                except Exception:
                    pass
            
            # If name input, fill name
            name_input = page.locator('input[placeholder*="name" i], input[name="name"], input[type="text"]')
            if name_input.count() > 0:
                print("Filling key name...", flush=True)
                name_input.first.fill("mistral_auto_key")
                time.sleep(1)
            
            # Workspace dropdown
            ws_btn = page.locator('button:has-text("Select workspace")')
            if ws_btn.count() > 0:
                print("Clicking 'Select workspace' dropdown...", flush=True)
                ws_btn.click()
                time.sleep(1)
                # Click the option
                opt = page.locator('div:has-text("Default Workspace")').last
                print("Clicking 'Default Workspace' option...", flush=True)
                opt.click()
                time.sleep(1)
            
            # Click modal's "New key" submit button
            modal_submit = page.locator('div[role="dialog"] button:has-text("New key"), button:has-text("New key")').last
            print("Clicking modal submit 'New key' button...", flush=True)
            modal_submit.click()
            time.sleep(5)
            page.screenshot(path="log/admin_key_result.png")
            with open("log/admin_key_result.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            print("Result Page text:\n", page.locator('body').inner_text()[:1500], flush=True)

if __name__ == "__main__":
    main()
