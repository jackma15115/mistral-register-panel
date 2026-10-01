from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from email_providers import ti_temp_mail
from webui import email_provider_store


def response(status: int, data: object, text: str = "") -> MagicMock:
    item = MagicMock()
    item.status_code = status
    item.text = text
    item.json.return_value = data
    return item


def test_provider_normalization_and_create_modes():
    assert ti_temp_mail.normalize_base("https://keldie.cyou/mailbox") == "https://keldie.cyou"
    assert ti_temp_mail.normalize_mode("main-domain") == "maindomain"
    assert ti_temp_mail.normalize_mode("sub") == "subdomain"

    post = MagicMock(return_value=response(201, {
        "token": "mailbox-token",
        "mailbox": "random@b.example",
    }))
    with patch.object(ti_temp_mail.random, "choice", return_value="b.example") as choose:
        address, token = ti_temp_mail.create_mailbox(
            post,
            "https://keldie.cyou/mailbox",
            "create-token",
            domain="a.example;b.example,c.example",
            mailbox_mode="sub-domain",
        )

    assert (address, token) == ("random@b.example", "mailbox-token")
    choose.assert_called_once_with(["a.example", "b.example", "c.example"])
    post.assert_called_once_with(
        "https://keldie.cyou/mailbox",
        json={"type": "subdomain", "domain": "b.example"},
        headers={"Authorization": "create-token", "Content-Type": "application/json"},
        timeout=30,
        proxies={},
    )


def test_fetch_uses_mailbox_token_and_reports_detail_errors():
    get = MagicMock(side_effect=[
        response(200, {"messages": [{"_id": "m/1", "subject": "Verify"}]}),
        response(502, {"error": "upstream"}, text="upstream unavailable"),
    ])
    messages = ti_temp_mail.fetch_messages(
        get,
        "https://keldie.cyou",
        "mailbox-secret",
        "person@example.test",
    )
    assert len(messages) == 1
    assert "HTTP 502" in messages[0]["_detail_error"]
    expected_headers = {"Authorization": "mailbox-secret"}
    assert get.call_args_list[0].kwargs["headers"] == expected_headers
    assert get.call_args_list[1].args[0] == "https://keldie.cyou/messages/m%2F1"
    assert get.call_args_list[1].kwargs["headers"] == expected_headers


def test_wait_logs_poll_recovery_without_tokens():
    logs: list[str] = []
    with patch.object(
        ti_temp_mail,
        "fetch_messages",
        side_effect=[
            RuntimeError("HTTP 502 temporary"),
            [],
            [{"id": "message-1", "subject": "ABC-123 xAI", "content": "ABC-123 xAI"}],
        ],
    ):
        code = ti_temp_mail.wait_for_code(
            MagicMock(),
            "https://keldie.cyou",
            "mailbox-token-secret",
            "person@example.test",
            timeout=5,
            poll_interval=0.4,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
            log_callback=logs.append,
        )

    output = "\n".join(logs)
    assert code == "ABC-123"
    assert "轮询 #1 异常" in output
    assert "接口已恢复" in output
    assert "找到验证码" in output
    assert "mailbox-token-secret" not in output


def test_wait_extracts_numeric_code_when_subject_is_truncated():
    with patch.object(
        ti_temp_mail,
        "fetch_messages",
        return_value=[
            {
                "id": "message-1",
                "subject": "S",
                "from": "noreply@x.ai",
                "html": (
                    "<p>Validate your email</p>"
                    "<p>Please use the code below: <strong>427-599</strong></p>"
                ),
            }
        ],
    ):
        code = ti_temp_mail.wait_for_code(
            MagicMock(),
            "https://keldie.cyou",
            "mailbox-token-secret",
            "person@example.test",
            timeout=2,
            poll_interval=0.4,
            raise_if_cancelled=lambda _callback: None,
            sleep_with_cancel=lambda _seconds, _callback: None,
        )

    assert code == "427-599"


def test_panel_provider_schema_supports_ti_fields():
    state = email_provider_store._public_state({})
    providers = {item["id"]: item for item in state["providers"]}
    assert "ti-temp-mail" in providers
    fields = {item["name"] for item in providers["ti-temp-mail"]["fields"]}
    assert fields == {
        "ti_temp_mail_base_url",
        "ti_temp_mail_api_key",
        "ti_temp_mail_domain",
        "ti_temp_mail_mode",
    }
    candidate = email_provider_store._candidate_config(
        {},
        "ti-temp-mail",
        {
            "ti_temp_mail_base_url": "https://keldie.cyou/mailbox",
            "ti_temp_mail_domain": "a.example;b.example,c.example",
            "ti_temp_mail_mode": "subdomain",
        },
    )
    assert candidate["ti_temp_mail_domain"] == "a.example,b.example,c.example"
    assert candidate["ti_temp_mail_mode"] == "subdomain"


if __name__ == "__main__":
    test_provider_normalization_and_create_modes()
    test_fetch_uses_mailbox_token_and_reports_detail_errors()
    test_wait_logs_poll_recovery_without_tokens()
    test_wait_extracts_numeric_code_when_subject_is_truncated()
    test_panel_provider_schema_supports_ti_fields()
    print("OK TI Temp Mail")
