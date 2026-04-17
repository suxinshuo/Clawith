import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
