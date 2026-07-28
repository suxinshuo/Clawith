"""Typed Feishu tools must keep the pre-Runtime per-user OAuth recovery.

The Durable Runtime executes Feishu tools with the app identity.  When the app
was never granted a scope, Feishu answers with a permission error.  Before the
Runtime migration the tool retried under the end user's Feishu identity and, if
no credential was stored, delivered an OAuth link to that user.  These tests
pin that behaviour onto the typed-outcome path.
"""

from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace
import uuid

import httpx
import pytest

from app.services import activity_logger, agent_tools
from app.services import feishu_service as feishu_module
from app.services.agent_runtime.tool_execution import ToolExecutionOutcome
from app.services.feishu_service import feishu_service


TENANT_ID = str(uuid.uuid4())
OAUTH_URL = "https://open.feishu.cn/open-apis/authen/v1/authorize?app_id=cli_test"

DENIED_BODY = {
    "code": 99991672,
    "msg": (
        "Access denied. One of the following scopes is required: "
        "[docx:document, docx:document:readonly]."
    ),
}


class FakeResponse:
    def __init__(self, payload, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeHTTP:
    """Queue of provider responses, recording the token each call presented."""

    def __init__(self) -> None:
        self.responses: dict[str, list[object]] = defaultdict(list)
        self.tokens: dict[str, list[str]] = defaultdict(list)

    def add(self, method: str, *responses: object) -> None:
        self.responses[method].extend(responses)

    async def request(self, method: str, url: str, **kwargs):
        headers = kwargs.get("headers") or {}
        self.tokens[method].append(
            str(headers.get("Authorization", "")).removeprefix("Bearer ")
        )
        if not self.responses[method]:
            raise AssertionError(f"unexpected or replayed {method.upper()}: {url}")
        response = self.responses[method].pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeProvider:
    """Records the identity each provider call ran under."""

    def __init__(self) -> None:
        self.http = FakeHTTP()
        self.responses: dict[str, list[object]] = defaultdict(list)
        self.tokens: dict[str, list[str]] = defaultdict(list)
        self.cards: list[dict] = []
        self.credential: object | None = None

    def add(self, method: str, *responses: object) -> None:
        self.responses[method].extend(responses)

    def _dispatch(self, method: str, kwargs: dict):
        # Every service method resolves its token through get_tenant_access_token,
        # so the override contextvar is what decides the acting identity.
        self.tokens[method].append(
            kwargs.get("access_token")
            or feishu_module.feishu_user_token_override.get()
            or "tenant-token"
        )
        if not self.responses[method]:
            raise AssertionError(f"unexpected or replayed provider call: {method}")
        response = self.responses[method].pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def read_feishu_doc(self, *args, **kwargs):
        return self._dispatch("read_feishu_doc", kwargs)

    async def bitable_query_records(self, *args, **kwargs):
        return self._dispatch("bitable_query_records", kwargs)


def install(monkeypatch, provider: FakeProvider, *, channel: str = "feishu") -> None:
    class Client:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **kwargs):
            return await provider.http.request("get", url, **kwargs)

        async def post(self, url, **kwargs):
            return await provider.http.request("post", url, **kwargs)

        async def delete(self, url, **kwargs):
            return await provider.http.request("delete", url, **kwargs)

    async def credentials(_agent_id):
        return "app-id", "app-secret"

    async def tenant_token(_app_id=None, _app_secret=None):
        override = feishu_module.feishu_user_token_override.get()
        return override or "tenant-token"

    async def tenant_id(_agent_id):
        return TENANT_ID

    async def resolve_credential(_user_id, _tenant_id, _provider, **_kwargs):
        return provider.credential

    async def oauth_url(_agent_id, _user_id, _scopes):
        return OAUTH_URL

    async def session_channel(_session_id):
        return channel, "feishu_p2p_ou_user" if channel == "feishu" else None

    async def send_card(**kwargs):
        provider.cards.append(kwargs)
        return True

    async def bitable_target(_agent_id, _arguments, *, require_table: bool):
        del require_table
        return "app-id", "app-secret", "app-token", "tbl1", None

    async def no_activity(*args, **kwargs):
        del args, kwargs

    def no_log(*args, **kwargs):
        del args, kwargs

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(agent_tools, "_get_feishu_credentials", credentials)
    monkeypatch.setattr(agent_tools, "_get_agent_tenant_id", tenant_id)
    monkeypatch.setattr(agent_tools, "_resolve_feishu_user_credential", resolve_credential)
    monkeypatch.setattr(agent_tools, "_build_feishu_oauth_url", oauth_url)
    monkeypatch.setattr(agent_tools, "_feishu_session_channel", session_channel)
    monkeypatch.setattr(agent_tools, "_send_feishu_credential_card", send_card)
    monkeypatch.setattr(agent_tools, "_bitable_target_outcome", bitable_target)
    monkeypatch.setattr(feishu_service, "get_tenant_access_token", tenant_token)
    monkeypatch.setattr(feishu_service, "read_feishu_doc", provider.read_feishu_doc)
    monkeypatch.setattr(
        feishu_service,
        "bitable_query_records",
        provider.bitable_query_records,
    )
    monkeypatch.setattr(activity_logger, "log_activity", no_activity)
    monkeypatch.setattr(
        agent_tools,
        "logger",
        SimpleNamespace(
            debug=no_log,
            info=no_log,
            warning=no_log,
            error=no_log,
            exception=no_log,
        ),
    )


async def execute(tool_name: str, arguments: dict):
    return await agent_tools.execute_builtin_tool_outcome(
        tool_name,
        arguments,
        agent_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=str(uuid.uuid4()),
    )


def denied_error(stage: str) -> Exception:
    from app.services.feishu_service import FeishuAPIError

    return FeishuAPIError(
        stage=stage,
        http_status=400,
        code=DENIED_BODY["code"],
        msg=DENIED_BODY["msg"],
    )


@pytest.mark.asyncio
async def test_doc_read_missing_scope_delivers_oauth_link_to_the_user(
    monkeypatch,
) -> None:
    provider = FakeProvider()
    provider.credential = None
    provider.add("read_feishu_doc", denied_error("doc_read"))
    install(monkeypatch, provider)

    outcome = await execute("feishu_doc_read", {"document_token": "doc1"})

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.status == "failed"
    assert outcome.error_code == "feishu_authorization_required"
    assert "授权" in (outcome.summary or "")
    assert len(provider.cards) == 1
    assert provider.cards[0]["link"] == OAUTH_URL


@pytest.mark.asyncio
async def test_doc_read_missing_scope_retries_under_the_user_identity(
    monkeypatch,
) -> None:
    provider = FakeProvider()
    provider.credential = SimpleNamespace(access_token="user-token")
    provider.add(
        "read_feishu_doc",
        denied_error("doc_read"),
        {"code": 0, "data": {"content": "hello from the user identity"}},
    )
    install(monkeypatch, provider)

    outcome = await execute("feishu_doc_read", {"document_token": "doc1"})

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.status == "succeeded"
    assert "hello from the user identity" in (outcome.summary or "")
    # First attempt as the app, retry as the user — and no OAuth card.
    assert provider.tokens["read_feishu_doc"] == ["tenant-token", "user-token"]
    assert provider.cards == []


@pytest.mark.asyncio
async def test_doc_read_reports_resource_denial_when_user_identity_also_fails(
    monkeypatch,
) -> None:
    provider = FakeProvider()
    provider.credential = SimpleNamespace(access_token="user-token")
    provider.add(
        "read_feishu_doc",
        denied_error("doc_read"),
        denied_error("doc_read"),
    )
    install(monkeypatch, provider)

    outcome = await execute("feishu_doc_read", {"document_token": "doc1"})

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.status == "failed"
    assert outcome.error_code == "feishu_user_permission_denied"
    assert provider.cards == []


@pytest.mark.asyncio
async def test_bitable_query_missing_scope_delivers_oauth_link(monkeypatch) -> None:
    provider = FakeProvider()
    provider.credential = None
    provider.add("bitable_query_records", denied_error("bitable_query_records"))
    install(monkeypatch, provider)

    outcome = await execute(
        "bitable_query_records",
        {"app_token": "app-token", "table_id": "tbl1"},
    )

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.status == "failed"
    assert outcome.error_code == "feishu_authorization_required"
    assert len(provider.cards) == 1


@pytest.mark.asyncio
async def test_non_permission_rejection_keeps_the_original_outcome(
    monkeypatch,
) -> None:
    from app.services.feishu_service import FeishuAPIError

    provider = FakeProvider()
    provider.credential = SimpleNamespace(access_token="user-token")
    provider.add(
        "read_feishu_doc",
        FeishuAPIError(
            stage="doc_read",
            http_status=404,
            code=1770001,
            msg="document not found",
        ),
    )
    install(monkeypatch, provider)

    outcome = await execute("feishu_doc_read", {"document_token": "doc1"})

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.status == "failed"
    assert outcome.error_code == "feishu_doc_read_rejected"
    # No second identity attempt and no OAuth link for a genuine 404.
    assert provider.tokens["read_feishu_doc"] == ["tenant-token"]
    assert provider.cards == []


@pytest.mark.asyncio
async def test_partially_applied_share_is_not_replayed_under_a_second_identity(
    monkeypatch,
) -> None:
    """A write that already took effect keeps its receipts instead of replaying."""
    provider = FakeProvider()
    provider.credential = SimpleNamespace(access_token="user-token")
    provider.http.add(
        "post",
        FakeResponse(
            {
                "code": 0,
                "data": {
                    "member": {
                        "member_type": "openid",
                        "member_id": "ou_member1",
                        "perm": "edit",
                    }
                },
            }
        ),
        FakeResponse(DENIED_BODY, status_code=400),
    )
    install(monkeypatch, provider)

    outcome = await execute(
        "feishu_drive_share",
        {
            "document_token": "doc1",
            "action": "add",
            "member_open_ids": ["ou_member1", "ou_member2"],
        },
    )

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.status == "failed"
    assert outcome.error_code != "feishu_authorization_required"
    # Exactly two POSTs: the granted member is never re-shared.
    assert len(provider.http.tokens["post"]) == 2
    assert provider.cards == []


@pytest.mark.asyncio
async def test_web_session_returns_the_oauth_link_in_the_tool_outcome(
    monkeypatch,
) -> None:
    provider = FakeProvider()
    provider.credential = None
    provider.add("read_feishu_doc", denied_error("doc_read"))
    install(monkeypatch, provider, channel="web")

    outcome = await execute("feishu_doc_read", {"document_token": "doc1"})

    assert isinstance(outcome, ToolExecutionOutcome)
    assert outcome.status == "failed"
    assert outcome.error_code == "feishu_authorization_required"
    assert OAUTH_URL in (outcome.summary or "")
    assert provider.cards == []
