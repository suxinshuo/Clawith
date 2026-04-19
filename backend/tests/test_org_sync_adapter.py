import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.org_sync_adapter import BaseOrgSyncAdapter, ExternalUser, FeishuOrgSyncAdapter


def _make_feishu_adapter():
    return FeishuOrgSyncAdapter(
        config={"app_id": "test_id", "app_secret": "test_secret"}
    )


def _mock_response(json_data):
    resp = MagicMock()
    resp.json.return_value = json_data
    return resp


@pytest.mark.asyncio
async def test_fetch_auth_scopes_returns_department_ids():
    adapter = _make_feishu_adapter()
    mock_client = AsyncMock()
    mock_client.get.return_value = _mock_response({
        "code": 0,
        "data": {
            "department_ids": ["od-aaa", "od-bbb"],
            "user_ids": ["uid1"],
        },
    })

    result = await adapter.fetch_auth_scopes("fake_token", mock_client)

    assert result == ["od-aaa", "od-bbb"]
    mock_client.get.assert_called_once()
    call_args = mock_client.get.call_args
    assert "contact/v3/scopes" in call_args[0][0]
    assert call_args[1]["params"]["department_id_type"] == "open_department_id"
    assert call_args[1]["headers"]["Authorization"] == "Bearer fake_token"


@pytest.mark.asyncio
async def test_fetch_auth_scopes_returns_empty_on_api_error():
    adapter = _make_feishu_adapter()
    mock_client = AsyncMock()
    mock_client.get.return_value = _mock_response({
        "code": 99999,
        "msg": "no permission",
    })

    result = await adapter.fetch_auth_scopes("fake_token", mock_client)

    assert result == []


@pytest.mark.asyncio
async def test_fetch_department_info_returns_detail():
    adapter = _make_feishu_adapter()
    mock_client = AsyncMock()
    mock_client.get.return_value = _mock_response({
        "code": 0,
        "data": {
            "department": {
                "open_department_id": "od-aaa",
                "name": "Data Platform",
                "parent_department_id": "od-parent",
                "member_count": 5,
            },
        },
    })

    result = await adapter.fetch_department_info("od-aaa", "fake_token", mock_client)

    assert result["name"] == "Data Platform"
    assert result["parent_department_id"] == "od-parent"
    assert result["member_count"] == 5
    call_kwargs = mock_client.get.call_args[1]
    assert "departments/od-aaa" in mock_client.get.call_args[0][0]
    assert call_kwargs["headers"]["Authorization"] == "Bearer fake_token"
    assert call_kwargs["params"]["department_id_type"] == "open_department_id"


@pytest.mark.asyncio
async def test_fetch_department_info_returns_none_on_error():
    adapter = _make_feishu_adapter()
    mock_client = AsyncMock()
    mock_client.get.return_value = _mock_response({
        "code": 40004,
        "msg": "no dept authority",
    })

    result = await adapter.fetch_department_info("od-aaa", "fake_token", mock_client)

    assert result is None


@pytest.mark.asyncio
async def test_fetch_department_info_returns_none_on_transport_error():
    adapter = _make_feishu_adapter()
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.ConnectError("connection refused")

    result = await adapter.fetch_department_info("od-aaa", "fake_token", mock_client)

    assert result is None


class _DummyAdapter(BaseOrgSyncAdapter):
    provider_type = "feishu"

    @property
    def api_base_url(self) -> str:
        return "https://example.com"

    async def get_access_token(self) -> str:
        return "token"

    async def fetch_departments(self):
        return []

    async def fetch_users(self, department_external_id: str):
        return []


class _FakeDB:
    def __init__(self):
        self.flush_calls = 0

    @asynccontextmanager
    async def begin_nested(self):
        yield

    async def flush(self):
        self.flush_calls += 1


class _SyncAdapterWithFailure(_DummyAdapter):
    def __init__(self):
        super().__init__()
        self.reconcile_called = False
        self.member_counts_updated = False
        self.provider = SimpleNamespace(id="provider-1", config={})

    async def _ensure_provider(self, db):
        return self.provider

    async def _upsert_department(self, db, provider, dept):
        return None

    async def _upsert_member(self, db, provider, user, department_external_id):
        raise ValueError("unionid is required")

    async def _reconcile(self, db, provider_id, sync_start):
        self.reconcile_called = True

    async def _update_member_counts(self, db, provider_id):
        self.member_counts_updated = True

    async def fetch_departments(self):
        return [SimpleNamespace(external_id="dept-1", name="Dept 1")]

    async def fetch_users(self, department_external_id: str):
        return [ExternalUser(external_id="user-1", name="Alice", unionid="")]


def test_validate_member_identifiers_requires_unionid_for_feishu():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type="feishu")
    user = ExternalUser(external_id="ou_123", name="Alice", unionid="")

    with pytest.raises(ValueError, match="unionid is required"):
        adapter._validate_member_identifiers(provider, user)


def test_validate_member_identifiers_rejects_unionid_equal_to_external_id():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type="dingtalk")
    user = ExternalUser(external_id="same-id", name="Bob", unionid="same-id")

    with pytest.raises(ValueError, match="must not equal external_id"):
        adapter._validate_member_identifiers(provider, user)


def test_validate_member_identifiers_allows_wecom_without_unionid():
    adapter = _DummyAdapter()
    provider = SimpleNamespace(provider_type="wecom")
    user = ExternalUser(external_id="zhangsan", name="Zhang San", unionid="")

    adapter._validate_member_identifiers(provider, user)


def test_sync_org_structure_skips_reconcile_after_member_failure():
    adapter = _SyncAdapterWithFailure()
    db = _FakeDB()

    result = asyncio.run(adapter.sync_org_structure(db))

    assert adapter.reconcile_called is False
    assert adapter.member_counts_updated is True
    assert "Reconcile skipped due to partial sync failures" in result["errors"]


@pytest.mark.asyncio
async def test_fetch_departments_partial_access_restores_hierarchy():
    """When scopes returns specific depts, hierarchy is restored among them."""
    adapter = _make_feishu_adapter()

    scopes_response = _mock_response({
        "code": 0,
        "data": {"department_ids": ["od-A", "od-B", "od-C"], "user_ids": []},
    })

    dept_info_responses = {
        "od-A": _mock_response({
            "code": 0,
            "data": {"department": {
                "open_department_id": "od-A",
                "name": "Tech",
                "parent_department_id": "od-root-unknown",
                "member_count": 0,
            }},
        }),
        "od-B": _mock_response({
            "code": 0,
            "data": {"department": {
                "open_department_id": "od-B",
                "name": "Backend",
                "parent_department_id": "od-A",
                "member_count": 0,
            }},
        }),
        "od-C": _mock_response({
            "code": 0,
            "data": {"department": {
                "open_department_id": "od-C",
                "name": "Data Platform",
                "parent_department_id": "od-B",
                "member_count": 3,
            }},
        }),
    }

    children_response = _mock_response({
        "code": 0,
        "data": {"items": [], "has_more": False, "page_token": ""},
    })

    async def mock_get(url, **kwargs):
        if "scopes" in url:
            return scopes_response
        if "/children" in url:
            return children_response
        for dept_id, resp in dept_info_responses.items():
            if dept_id in url:
                return resp
        return children_response

    with patch.object(adapter, "get_access_token", return_value="fake_token"):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.side_effect = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            depts = await adapter.fetch_departments()

    dept_map = {d.external_id: d for d in depts}

    # Exactly 4 departments: root + 3 scoped (no duplicates)
    assert len(depts) == 4
    assert "0" in dept_map
    assert dept_map["0"].parent_external_id is None
    assert dept_map["od-A"].parent_external_id == "0"
    assert dept_map["od-A"].name == "Tech"
    assert dept_map["od-B"].parent_external_id == "od-A"
    assert dept_map["od-C"].parent_external_id == "od-B"


@pytest.mark.asyncio
async def test_fetch_departments_scopes_failure_falls_back_to_root():
    """When scopes API fails, fall back to fetching from root '0'."""
    adapter = _make_feishu_adapter()

    scopes_error = _mock_response({"code": 99999, "msg": "no permission"})
    children_response = _mock_response({
        "code": 0,
        "data": {
            "items": [{"open_department_id": "od-child", "name": "Child", "member_count": 1}],
            "has_more": False,
            "page_token": "",
        },
    })
    no_children = _mock_response({
        "code": 0,
        "data": {"items": [], "has_more": False, "page_token": ""},
    })

    call_count = {"children": 0}

    async def mock_get(url, **kwargs):
        if "scopes" in url:
            return scopes_error
        if "/children" in url:
            call_count["children"] += 1
            if call_count["children"] == 1:
                return children_response
            return no_children
        return no_children

    with patch.object(adapter, "get_access_token", return_value="fake_token"):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.side_effect = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            depts = await adapter.fetch_departments()

    dept_map = {d.external_id: d for d in depts}
    assert "0" in dept_map
    assert "od-child" in dept_map
    assert dept_map["od-child"].parent_external_id == "0"


@pytest.mark.asyncio
async def test_fetch_departments_dept_detail_failure_degrades_to_root():
    """When a single dept detail fails, that dept falls back to root parent."""
    adapter = _make_feishu_adapter()

    scopes_response = _mock_response({
        "code": 0,
        "data": {"department_ids": ["od-ok", "od-fail"], "user_ids": []},
    })

    no_children = _mock_response({
        "code": 0,
        "data": {"items": [], "has_more": False, "page_token": ""},
    })

    async def mock_get(url, **kwargs):
        if "scopes" in url:
            return scopes_response
        if "/children" in url:
            return no_children
        if "od-ok" in url:
            return _mock_response({
                "code": 0,
                "data": {"department": {
                    "open_department_id": "od-ok",
                    "name": "OK Dept",
                    "parent_department_id": "od-unknown",
                    "member_count": 2,
                }},
            })
        if "od-fail" in url:
            return _mock_response({"code": 40004, "msg": "no dept authority"})
        return no_children

    with patch.object(adapter, "get_access_token", return_value="fake_token"):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.side_effect = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            depts = await adapter.fetch_departments()

    dept_map = {d.external_id: d for d in depts}
    assert "od-ok" in dept_map
    assert dept_map["od-ok"].parent_external_id == "0"
    assert "od-fail" in dept_map
    assert dept_map["od-fail"].parent_external_id == "0"
    assert dept_map["od-fail"].name == "od-fail"


@pytest.mark.asyncio
async def test_fetch_departments_full_access_uses_root():
    """When scopes contains '0', use original logic from root."""
    adapter = _make_feishu_adapter()

    scopes_response = _mock_response({
        "code": 0,
        "data": {"department_ids": ["0", "od-other"], "user_ids": []},
    })
    children_response = _mock_response({
        "code": 0,
        "data": {
            "items": [{"open_department_id": "od-child", "name": "Child", "member_count": 1}],
            "has_more": False,
            "page_token": "",
        },
    })
    no_children = _mock_response({
        "code": 0,
        "data": {"items": [], "has_more": False, "page_token": ""},
    })

    call_count = {"children": 0}

    async def mock_get(url, **kwargs):
        if "scopes" in url:
            return scopes_response
        if "/children" in url:
            call_count["children"] += 1
            if call_count["children"] == 1:
                return children_response
            return no_children
        return no_children

    with patch.object(adapter, "get_access_token", return_value="fake_token"):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client = AsyncMock()
            mock_client.get.side_effect = mock_get
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client

            depts = await adapter.fetch_departments()

    dept_map = {d.external_id: d for d in depts}
    # Full access: fetches from root, no dept detail calls
    assert "0" in dept_map
    assert "od-child" in dept_map
    assert dept_map["od-child"].parent_external_id == "0"
