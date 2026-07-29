"""`read_webpage` must not swallow Feishu document links.

A user who pastes a Feishu doc/wiki link into an IM channel used to get a
silently wrong answer: `read_webpage` fetched the link, followed the redirect
chain to `accounts.feishu.cn`, received HTTP 200 for the *login page*, and the
visible-text fallback salvaged enough of that page to report success.  The
Feishu OpenAPI was never called, so the per-user OAuth recovery in
`_feishu_recover_denied_outcome` never ran and the user never saw an
authorization link.

These tests pin two guards: a Feishu document link is routed to the typed tool
without being fetched at all, and an authentication wall is reported as a
failure rather than as page content.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import agent_tools


class ExplodingClient:
    """Any network use is a contract violation for the routing guard."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, *args, **kwargs):
        raise AssertionError("read_webpage must not fetch a Feishu document URL")


def _install_passthrough_validator(monkeypatch) -> None:
    async def validate(url):
        return url, None

    monkeypatch.setattr(agent_tools, "_validate_public_http_url", validate)


def _install_redirecting_client(monkeypatch, *, final_url: str, body: bytes) -> None:
    class Response:
        status_code = 200
        url = final_url
        encoding = "utf-8"
        headers = {"content-type": "text/html"}

        async def aiter_bytes(self):
            yield body

    class StreamContext:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_args):
            return False

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *args, **kwargs):
            return StreamContext()

    monkeypatch.setattr(httpx, "AsyncClient", Client)


@pytest.mark.parametrize(
    ("url", "token"),
    [
        ("https://tigertech.feishu.cn/wiki/V6cKwT0b7iBqq2k60pVcTiCenTg", "V6cKwT0b7iBqq2k60pVcTiCenTg"),
        ("https://tenant.feishu.cn/docx/DocxTokenAbc123", "DocxTokenAbc123"),
        ("https://tenant.larksuite.com/wiki/LarkNodeToken9", "LarkNodeToken9"),
    ],
)
@pytest.mark.asyncio
async def test_read_webpage_routes_feishu_doc_links_to_typed_tool(
    monkeypatch,
    url: str,
    token: str,
) -> None:
    _install_passthrough_validator(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", ExplodingClient)

    outcome = await agent_tools._read_webpage_outcome({"url": url})

    assert outcome.status == "failed"
    assert outcome.error_code == "feishu_doc_url_requires_typed_tool"
    assert outcome.retryable is False
    # The Agent has to be able to act on this without a second round trip, so
    # the token it needs and the tool that accepts it both have to be named.
    assert token in (outcome.summary or "")
    assert "feishu_doc_read" in (outcome.summary or "")


@pytest.mark.asyncio
async def test_read_webpage_routes_feishu_bitable_links_to_typed_tool(
    monkeypatch,
) -> None:
    _install_passthrough_validator(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", ExplodingClient)

    outcome = await agent_tools._read_webpage_outcome(
        {"url": "https://tenant.feishu.cn/base/BaseAppToken77?table=tblAbc"}
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "feishu_doc_url_requires_typed_tool"
    assert "BaseAppToken77" in (outcome.summary or "")
    assert "bitable_query_records" in (outcome.summary or "")


@pytest.mark.asyncio
async def test_read_webpage_reports_feishu_login_wall_as_failure(
    monkeypatch,
) -> None:
    """A redirect to the Feishu account wall is an auth failure, not content."""
    _install_passthrough_validator(monkeypatch)
    # Not a document path, so the routing guard lets this one through to the
    # network — the login page is only detectable after the redirect chain.
    _install_redirecting_client(
        monkeypatch,
        final_url=(
            "https://accounts.feishu.cn/accounts/page/login"
            "?app_id=2&redirect_uri=https%3A%2F%2Ftenant.feishu.cn%2Fspace%2Fhome"
        ),
        body="<html><title>登录</title><body>登录飞书以继续访问</body></html>".encode(),
    )

    outcome = await agent_tools._read_webpage_outcome(
        {"url": "https://tenant.feishu.cn/space/home"}
    )

    assert outcome.status == "failed"
    assert outcome.error_code == "webpage_authentication_required"
    assert outcome.retryable is False


def test_read_webpage_description_steers_feishu_links_to_typed_tools() -> None:
    """Cheapest fix for the wrong-tool pick is to say so in the description.

    The runtime guard recovers from the bad pick, but only after a wasted round
    trip; the Agent should not reach for read_webpage on a Feishu link at all.
    """
    from app.services.builtin_tool_definitions import builtin_model_definition

    description = builtin_model_definition("read_webpage")["function"]["description"]

    assert "feishu_doc_read" in description


@pytest.mark.asyncio
async def test_read_webpage_still_reads_ordinary_public_pages(monkeypatch) -> None:
    """The guards must not regress the normal path."""
    _install_passthrough_validator(monkeypatch)
    _install_redirecting_client(
        monkeypatch,
        final_url="https://example.test/article",
        body=b"<html><title>Ordinary</title><body><p>Real article body here.</p></body></html>",
    )

    outcome = await agent_tools._read_webpage_outcome(
        {"url": "https://example.test/article"}
    )

    assert outcome.status == "succeeded"
    assert outcome.evidence_refs == ("https://example.test/article",)
