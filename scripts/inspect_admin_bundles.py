# -*- coding: utf-8 -*-
import re
import requests

def main():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"})
    r = s.get("https://admin.mistral.ai/organization/api-keys", allow_redirects=False)
    print("Status:", r.status_code)
    print("Headers:", r.headers)
    
    # Try fetching the root or login or script tags
    r2 = s.get("https://admin.mistral.ai/", allow_redirects=False)
    print("Root status:", r2.status_code, r2.headers.get("Location"))
    
    # Let's inspect console.mistral.ai as well
    r3 = s.get("https://console.mistral.ai/api-keys", allow_redirects=False)
    print("Console status:", r3.status_code, r3.headers.get("Location"))

if __name__ == "__main__":
    main()
