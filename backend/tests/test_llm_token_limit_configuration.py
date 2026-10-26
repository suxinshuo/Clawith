"""Exercise persisted configuration and HTTP requests without a live database/gateway."""

import importlib.util
import json
import uuid
from io import StringIO
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.api import enterprise
from app.models.llm import LLMModel
from app.schemas.schemas import LLMModelCreate, LLMModelOut, LLMModelUpdate
from app.services.llm.client import create_llm_client
from app.services.llm import caller


class AsyncSessionFacade:
    """Use real SQLite persistence through the route's async session interface."""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, statement):
        return self.session.execute(statement)

    def add(self, obj):
        self.session.add(obj)

    async def flush(self):
        self.session.flush()

    async def commit(self):
        self.session.commit()

    async def refresh(self, obj):
        self.session.refresh(obj)

    async def rollback(self):
        self.session.rollback()


@pytest.fixture
def model_db(monkeypatch):
    engine = create_engine("sqlite://")
    LLMModel.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as session:
        db = AsyncSessionFacade(session)
        monkeypatch.setattr(enterprise, "async_session", lambda: db)
        monkeypatch.setattr(enterprise, "get_model_api_key", lambda model: "stored-test-key")
        yield db
    engine.dispose()


ADMIN = SimpleNamespace(role="platform_admin", tenant_id=None)


async def add_model(db, strategy="max_completion_tokens"):
    return await enterprise.add_llm_model(
        LLMModelCreate(provider="custom", model="private-alias", api_key="key", label="Private",
                       token_limit_param=strategy),
        tenant_id=None, current_user=ADMIN, db=db,
    )


@pytest.mark.asyncio
async def test_model_create_persists_and_returns_strategy(model_db):
    result = await add_model(model_db)
    assert result.token_limit_param == "max_completion_tokens"
    stored = model_db.session.get(LLMModel, result.id)
    assert stored.token_limit_param == "max_completion_tokens"
    assert LLMModelOut.model_validate(stored).token_limit_param == "max_completion_tokens"


@pytest.mark.asyncio
async def test_update_omission_preserves_strategy_and_auto_resets_it(model_db):
    result = await add_model(model_db)
    updated = await enterprise.update_llm_model(
        result.id, LLMModelUpdate(label="Renamed"), current_user=ADMIN, db=model_db,
    )
    assert updated.token_limit_param == "max_completion_tokens"
    updated = await enterprise.update_llm_model(
        result.id, LLMModelUpdate(token_limit_param="auto"), current_user=ADMIN, db=model_db,
    )
    assert updated.token_limit_param == "auto"


@pytest.mark.parametrize("schema,base", [
    (LLMModelCreate, dict(provider="openai", model="gpt-6-sol", api_key="key", label="Model")),
    (LLMModelUpdate, {}),
    (enterprise.LLMTestRequest, dict(provider="custom", model="alias")),
])
@pytest.mark.parametrize("value", (None, "invalid", ""))
def test_invalid_or_null_strategy_is_rejected(schema, base, value):
    with pytest.raises(ValidationError):
        schema(**base, token_limit_param=value)


def test_legacy_create_and_output_default_to_auto():
    created = LLMModelCreate(provider="openai", model="gpt-4o", api_key="key", label="Legacy")
    assert created.token_limit_param == "auto"
    out = LLMModelOut(id=uuid.uuid4(), provider="openai", model="gpt-4o", label="Legacy",
                      enabled=True, created_at=datetime.now(timezone.utc))
    assert out.token_limit_param == "auto"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit,expected", [
    (None, "max_completion_tokens"), ("max_tokens", "max_tokens"), ("auto", "max_tokens"),
])
@pytest.mark.parametrize("provided_key", (None, "unsaved-key"))
async def test_llm_test_resolves_saved_and_unsaved_strategy(model_db, monkeypatch, explicit, expected, provided_key):
    model = await add_model(model_db)
    requests, clients = [], []

    def factory(**kwargs):
        client = create_llm_client(**kwargs)

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client

    monkeypatch.setattr(enterprise, "create_llm_client", factory)
    data = dict(provider="custom", model="private-alias", model_id=str(model.id), api_key=provided_key)
    if explicit is not None:
        data["token_limit_param"] = explicit
    result = await enterprise.test_llm_model(enterprise.LLMTestRequest(**data), current_user=ADMIN)
    try:
        assert result["success"] is True
        assert requests[0][expected] == 256
        assert len({"max_tokens", "max_completion_tokens"}.intersection(requests[0])) == 1
        assert clients[0]._client.is_closed
    finally:
        for client in clients:
            await client.close()


@pytest.mark.asyncio
async def test_unsaved_gpt6_test_defaults_to_auto_and_closes_on_error(monkeypatch):
    clients, requests = [], []

    def factory(**kwargs):
        client = create_llm_client(**kwargs)

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(400, json={"error": "deliberate upstream failure"})

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client

    monkeypatch.setattr(enterprise, "create_llm_client", factory)
    result = await enterprise.test_llm_model(
        enterprise.LLMTestRequest(provider="openai", model="gpt-6-astra", api_key="key"), current_user=ADMIN,
    )
    try:
        assert result["success"] is False
        assert requests[0]["max_completion_tokens"] == 256
        assert len(requests) == 1
        assert clients[0]._client.is_closed
    finally:
        for client in clients:
            await client.close()


@pytest.mark.asyncio
async def test_saved_test_configuration_cannot_be_read_from_another_tenant(model_db):
    model = await add_model(model_db)
    model_db.session.get(LLMModel, model.id).tenant_id = uuid.uuid4()
    model_db.session.commit()
    other_admin = SimpleNamespace(role="org_admin", tenant_id=uuid.uuid4())
    with pytest.raises(HTTPException) as error:
        await enterprise.test_llm_model(
            enterprise.LLMTestRequest(provider="custom", model="alias", model_id=str(model.id)),
            current_user=other_admin,
        )
    assert error.value.status_code in (403, 404)


def test_migration_preserves_existing_records_and_sets_auto_default():
    path = Path(__file__).parents[1] / "alembic/versions/add_llm_token_limit_param.py"
    assert path.exists(), "Incremental migration is required for existing databases"
    spec = importlib.util.spec_from_file_location("token_limit_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE llm_models (id TEXT PRIMARY KEY, label TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO llm_models VALUES ('old', 'Original')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(text("SELECT label, token_limit_param FROM llm_models WHERE id='old'")).one() == (
            "Original", "auto",
        )
        connection.execute(text("INSERT INTO llm_models (id, label) VALUES ('new', 'New')"))
        assert connection.execute(text("SELECT token_limit_param FROM llm_models WHERE id='new'")).scalar() == "auto"
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert connection.execute(text("SELECT label FROM llm_models WHERE id='old'")).scalar() == "Original"
    engine.dispose()


def test_migration_handles_column_already_created_by_initial_schema():
    path = Path(__file__).parents[1] / "alembic/versions/add_llm_token_limit_param.py"
    spec = importlib.util.spec_from_file_location("token_limit_existing_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE llm_models (id TEXT PRIMARY KEY, token_limit_param VARCHAR(32) NOT NULL DEFAULT 'auto')"))
        connection.execute(text("INSERT INTO llm_models VALUES ('existing', 'max_completion_tokens')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        assert connection.execute(text("SELECT token_limit_param FROM llm_models")).scalar() == "max_completion_tokens"
    engine.dispose()


def test_postgres_offline_migration_emits_idempotent_ddl():
    path = Path(__file__).parents[1] / "alembic/versions/add_llm_token_limit_param.py"
    spec = importlib.util.spec_from_file_location("token_limit_offline_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "ADD COLUMN IF NOT EXISTS" in sql
    assert "token_limit_param" in sql
    assert "DEFAULT 'auto'" in sql


@pytest.mark.asyncio
async def test_chat_failover_preserves_each_models_explicit_strategy(monkeypatch):
    from app.services import agent_context

    monkeypatch.setattr(agent_context, "build_agent_context", AsyncMock(return_value=("system", "")))
    monkeypatch.setattr(caller, "get_model_api_key", lambda model: "key")
    requests = []

    def factory(**kwargs):
        client = create_llm_client(**kwargs)

        def handle(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if payload["model"] == "primary-alias":
                return httpx.Response(503, text="temporary upstream failure")
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"fallback-ok"}}]}\n\ndata: [DONE]\n\n')

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        return client

    monkeypatch.setattr(caller, "create_llm_client", factory)
    primary = SimpleNamespace(provider="custom", model="primary-alias", base_url="https://test.invalid/v1",
                              temperature=None, max_output_tokens=333, token_limit_param="max_completion_tokens")
    fallback = SimpleNamespace(provider="custom", model="fallback-alias", base_url="https://test.invalid/v1",
                               temperature=None, max_output_tokens=444, token_limit_param="max_tokens")
    result = await caller.call_llm_with_failover(
        primary, fallback, [{"role": "user", "content": "ok"}], "Agent", "Role", skip_tools=True,
    )
    assert result == "fallback-ok"
    assert len(requests) == 2
    assert requests[0]["max_completion_tokens"] == 333
    assert "max_tokens" not in requests[0]
    assert requests[1]["max_tokens"] == 444
    assert "max_completion_tokens" not in requests[1]


@pytest.mark.asyncio
async def test_background_tool_loop_uses_explicit_strategy(monkeypatch):
    agent_id, model_id = uuid.uuid4(), uuid.uuid4()
    model = SimpleNamespace(provider="custom", model="background-alias", base_url="https://test.invalid/v1",
                            temperature=None, max_output_tokens=321, token_limit_param="max_completion_tokens")
    agent = SimpleNamespace(id=agent_id, name="Agent", primary_model_id=model_id, fallback_model_id=None)
    db = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(scalar_one_or_none=lambda: agent), SimpleNamespace(scalar_one_or_none=lambda: model),
    ]))
    monkeypatch.setattr(caller, "get_model_api_key", lambda model: "key")
    monkeypatch.setattr(caller, "get_agent_tools_for_llm", AsyncMock(return_value=[]))
    monkeypatch.setattr(caller, "record_token_usage", AsyncMock())
    requests = []

    def factory(**kwargs):
        client = create_llm_client(**kwargs)

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "background-ok"}}]})

        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        return client

    monkeypatch.setattr(caller, "create_llm_client", factory)
    result = await caller.call_agent_llm_with_tools(db, agent_id, "system", "user")
    assert result == "background-ok"
    assert requests[0]["max_completion_tokens"] == 321
    assert "max_tokens" not in requests[0]
