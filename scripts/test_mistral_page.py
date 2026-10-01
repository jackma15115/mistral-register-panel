# -*- coding: utf-8 -*-
import sys
import time
from camoufox.sync_api import Camoufox

def main():
    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        print("Navigating to https://admin.mistral.ai/organization/api-keys ...")
        page.goto("https://admin.mistral.ai/organization/api-keys", timeout=60000)
        time.sleep(4)
        email_input = page.locator('input[name="email"]')
        test_email = f"mistral_{int(time.time())}@example.com"
        print("Filling email:", test_email)
        email_input.fill(test_email)
        time.sleep(1)
        email_input.press("Enter")
        
        # Wait up to 10 seconds for password field
        found = False
        for i in range(10):
            time.sleep(1)
            if page.locator('input[name="password"]').count() > 0:
                found = True
                print(f"Password field appeared after {i+1}s!")
                break
        
        if not found:
            print("Password not found! Checking page text:")
            print(page.locator('body').inner_text()[:600])
            return

        pwd = page.locator('input[name="password"]')
        first = page.locator('input[name="firstName"]')
        last = page.locator('input[name="lastName"]')
        pwd.fill("Mistral_Pass123!@#")
        first.fill("Alex")
        last.fill("Taylor")
        time.sleep(1)
        btn = page.locator('button:has-text("Signup")')
        print("Clicking Signup button...")
        btn.click()
        time.sleep(5)
        print("Current URL:", page.url)
        print("Title:", page.title())
        content = page.content()
        with open("log/mistral_after_signup.html", "w", encoding="utf-8") as f:
            f.write(content)
        print("Page text after signup:\n", page.locator('body').inner_text()[:1000])

if __name__ == "__main__":
    main()
