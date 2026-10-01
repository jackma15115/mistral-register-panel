# -*- coding: utf-8 -*-
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import connectivity
import register_flow


def test_constants():
    assert register_flow.SIGNUP_URL == "https://admin.mistral.ai/organization/api-keys"
    assert connectivity.MISTRAL_SIGNUP_URL == "https://admin.mistral.ai/organization/api-keys"


def test_profile_generation():
    profile = register_flow.build_profile()
    assert "given_name" in profile and profile["given_name"]
    assert "family_name" in profile and profile["family_name"]
    pwd = profile.get("password", "")
    assert len(pwd) >= 12
    assert any(c.isupper() for c in pwd)
    assert any(c.islower() for c in pwd)
    assert any(c.isdigit() for c in pwd)
    assert any(c in "!@#$%^&*#" for c in pwd)


def test_detect_domain_rejection():
    assert register_flow.detect_email_domain_rejection("Error: [Node traits.email]: 'Email invalid.'")
    assert register_flow.detect_email_domain_rejection("Disposable email domain rejected")
    assert not register_flow.detect_email_domain_rejection("Please enter your password to continue")


def test_connectivity_check():
    mock_resp_ok = MagicMock()
    mock_resp_ok.status_code = 302
    mock_resp_ok.text = "Redirecting..."
    mock_resp_ok.headers = {}
    name, ok, detail = connectivity.check_mistral_signup("", lambda *a, **kw: mock_resp_ok)
    assert ok
    assert "可达 HTTP 302" in detail

    mock_resp_cf = MagicMock()
    mock_resp_cf.status_code = 403
    mock_resp_cf.text = "Just a moment..."
    mock_resp_cf.headers = {"server": "cloudflare"}
    name, ok, detail = connectivity.check_mistral_signup("", lambda *a, **kw: mock_resp_cf)
    assert not ok
    assert "Cloudflare" in detail


if __name__ == "__main__":
    test_constants()
    test_profile_generation()
    test_detect_domain_rejection()
    test_connectivity_check()
    print("OK Mistral flow tests")
