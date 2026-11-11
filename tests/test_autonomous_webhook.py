"""Webhook notification path for the autonomous runner.

The webhook is the one channel that works before the trading engines exist,
so the parent supervisor can push restarts and watchdog alerts even when no
email or wechat is configured. These tests stub requests.post to avoid real
network calls.
"""

from unittest.mock import MagicMock, patch


import pytest


from vnpy.autonomous.config import AutonomousConfig, NotificationConfig
from vnpy.autonomous.runner import AutonomousRunner


@pytest.fixture
def runner() -> AutonomousRunner:
    config: AutonomousConfig = AutonomousConfig(
        mode="paper",
        gateways={"CTP": {}},
        notification=NotificationConfig(webhook_url="http://webhook.test/hook"),
    )
    return AutonomousRunner(config)


def test_webhook_not_called_when_url_empty() -> None:
    config: AutonomousConfig = AutonomousConfig(
        mode="paper",
        gateways={"CTP": {}},
        notification=NotificationConfig(webhook_url=""),
    )
    instance: AutonomousRunner = AutonomousRunner(config)
    with patch("requests.post") as mock_post:
        instance._send_webhook("hello")
    mock_post.assert_not_called()


def test_webhook_posts_payload(runner: AutonomousRunner) -> None:
    with patch("requests.post") as mock_post:
        mock_response: MagicMock = MagicMock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        runner._send_webhook("测试消息")

    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert call_args[0] == "http://webhook.test/hook"
    assert call_kwargs["json"] == {
        "source": "vnpy.autonomous",
        "mode": "paper",
        "message": "测试消息",
    }
    assert call_kwargs["timeout"] == 5


def test_webhook_logs_on_http_error(runner: AutonomousRunner) -> None:
    with patch("requests.post") as mock_post:
        mock_response: MagicMock = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "boom"
        mock_post.return_value = mock_response
        runner._send_webhook("hello")
    mock_post.assert_called_once()


def test_webhook_survives_network_exception(runner: AutonomousRunner) -> None:
    with patch("requests.post", side_effect=ConnectionError("offline")):
        # Must not raise.
        runner._send_webhook("hello")


def test_notify_falls_back_to_webhook_without_engines() -> None:
    """Parent supervisor notifies before main_engine exists."""
    config: AutonomousConfig = AutonomousConfig(
        mode="paper",
        gateways={"CTP": {}},
        notification=NotificationConfig(webhook_url="http://webhook.test/hook"),
    )
    instance: AutonomousRunner = AutonomousRunner(config)
    assert instance.main_engine is None
    with patch.object(instance, "_send_webhook") as mock_webhook:
        instance._notify("重启冷却结束")
    mock_webhook.assert_called_once_with("重启冷却结束")
