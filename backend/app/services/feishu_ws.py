"""Feishu WebSocket Long Connection Manager."""

import asyncio
import json
import threading
from typing import Any, Dict
import uuid

from loguru import logger
try:
    import lark_oapi as lark
    import lark_oapi.ws as ws
    from lark_oapi.ws.client import _new_ping_frame as _lark_new_ping_frame
    _HAS_LARK = True
except ImportError:
    lark = None  # type: ignore
    ws = None    # type: ignore
    _lark_new_ping_frame = None  # type: ignore
    _HAS_LARK = False

if _HAS_LARK:
    try:
        import websockets as _websockets
        # Keep a reference to the original connect so we can restore it if needed.
        _orig_websockets_connect = _websockets.connect
        _PROXY_PATCH_AVAILABLE = True
    except ImportError:
        _PROXY_PATCH_AVAILABLE = False
else:
    _PROXY_PATCH_AVAILABLE = False


def _make_no_proxy_connect(orig_connect):
    """Return a drop-in replacement for websockets.connect that forces proxy=None.

    This is intentionally NOT applied at module import time to avoid polluting
    the global websockets namespace for other modules in the process.  Instead
    it is applied as a scoped context manager around lark-oapi's _connect() call.
    """
    import contextlib

    class _NoProxyConnect:
        """Wraps websockets.connect to inject proxy=None, preventing macOS
        system-proxy interference with long-lived SSE / WebSocket connections."""

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("proxy", None)
            self._coro = orig_connect(*args, **kwargs)
            self._ws = None

        def __await__(self):
            return self._coro.__await__()

        async def __aenter__(self):
            self._ws = await self._coro
            return self._ws

        async def __aexit__(self, *exc):
            if self._ws:
                await self._ws.close()

    @contextlib.asynccontextmanager
    async def _scoped_no_proxy():
        """Context manager that temporarily replaces websockets.connect for
        the duration of the lark-oapi connection handshake only."""
        if not _PROXY_PATCH_AVAILABLE:
            yield
            return
        old = _websockets.connect
        _websockets.connect = _NoProxyConnect
        logger.debug("[Feishu WS] Scoped websockets proxy bypass: active")
        try:
            yield
        finally:
            _websockets.connect = old
            logger.debug("[Feishu WS] Scoped websockets proxy bypass: restored")

    return _scoped_no_proxy

from app.database import async_session
from app.models.channel_config import ChannelConfig
from sqlalchemy import select


if not _HAS_LARK:
    logger.warning(
        "[Feishu WS] lark-oapi package not installed. "
        "Feishu WebSocket features will be disabled. "
        "Install with: pip install lark-oapi"
    )


class FeishuWSManager:
    """Manages Feishu WebSocket clients for all agents."""

    def __init__(self):
        self._clients: Dict[uuid.UUID, ws.Client] = {}
        # Tasks for reconnection or ping loops if we want to cancel them later
        self._tasks: Dict[uuid.UUID, asyncio.Task] = {}

    def _create_event_handler(self, agent_id: uuid.UUID) -> lark.EventDispatcherHandler:
        """Create an event dispatcher for a specific agent."""

        def handle_message(data: Any) -> None:
            """Handle im.message.receive_v1 events from Feishu WebSocket."""
            try:
                # The data object carries the raw event body
                raw_body = getattr(data, "raw_body", None)
                logger.info(f"[Feishu WS] Received event: {data}")
                if not raw_body:
                    # Some SDK versions pass the dict directly
                    if isinstance(data, dict):
                        body_dict = data
                    else:
                        # Handle lark_oapi.event.custom.CustomizedEvent
                        body_dict = {}
                        if hasattr(data, "header"):
                            header_obj = data.header
                            body_dict["header"] = vars(header_obj) if hasattr(header_obj, "__dict__") else {
                                "event_type": getattr(header_obj, "event_type", "im.message.receive_v1"),
                                "event_id": getattr(header_obj, "event_id", ""),
                                "create_time": getattr(header_obj, "create_time", "")
                            }
                            # Ensure event_type is present as it's required downstream
                            if "event_type" not in body_dict["header"]:
                                body_dict["header"]["event_type"] = getattr(header_obj, "event_type", "im.message.receive_v1")
                        else:
                            body_dict["header"] = {"event_type": "im.message.receive_v1"}

                        if hasattr(data, "event"):
                            body_dict["event"] = data.event
                        elif hasattr(data, "content") and isinstance(getattr(data, "content"), str):
                            import json
                            try:
                                body_dict["event"] = json.loads(data.content)
                            except json.JSONDecodeError:
                                body_dict["event"] = {"content": data.content}
                        
                        if not hasattr(data, "header") and not hasattr(data, "event"):
                            logger.warning(f"[Feishu WS] Unexpected event data type with no recognizable fields: {type(data)}")
                            return
                else:
                    body_dict = json.loads(raw_body.decode("utf-8"))

                loop = asyncio.get_running_loop()
                loop.create_task(self._async_handle_message(agent_id, data))
            except RuntimeError:
                try:
                    # If no running loop in this thread, try to find the main event loop
                    # This is a heuristic and might need adjustment depending on the exact async framework setup
                    main_loop = [t for t in asyncio.all_tasks() if t.get_name() != "feishu-ws"][0].get_loop()
                    asyncio.run_coroutine_threadsafe(self._async_handle_message(agent_id, data), main_loop)
                except Exception as e:
                    logger.exception(f"[Feishu WS] Could not dispatch event to main loop: {e}")

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_customized_event("im.message.receive_v1", handle_message)
            .build()
        )
        return dispatcher

    async def _async_handle_message(self, agent_id: uuid.UUID, data: Dict[str, Any]) -> None:
        """Handle im.message.receive_v1 events from Feishu WebSocket asynchronously."""
        try:
            # The data object carries the raw event body
            raw_body = getattr(data, "raw_body", None)
            if not raw_body:
                # Some SDK versions pass the dict directly
                if isinstance(data, dict):
                    body_dict = data
                else:
                    # Handle lark_oapi.event.custom.CustomizedEvent
                    body_dict = {}
                    if hasattr(data, "header"):
                        header_obj = data.header
                        body_dict["header"] = vars(header_obj) if hasattr(header_obj, "__dict__") else {
                            "event_type": getattr(header_obj, "event_type", "im.message.receive_v1"),
                            "event_id": getattr(header_obj, "event_id", ""),
                            "create_time": getattr(header_obj, "create_time", "")
                        }
                        if "event_type" not in body_dict["header"]:
                            body_dict["header"]["event_type"] = getattr(header_obj, "event_type", "im.message.receive_v1")
                    else:
                        body_dict["header"] = {"event_type": "im.message.receive_v1"}

                    if hasattr(data, "event"):
                        body_dict["event"] = data.event
                    elif hasattr(data, "content") and isinstance(getattr(data, "content"), str):
                        import json
                        try:
                            body_dict["event"] = json.loads(data.content)
                        except json.JSONDecodeError:
                            body_dict["event"] = {"content": data.content}
                    
                    if not hasattr(data, "header") and not hasattr(data, "event"):
                        logger.warning(f"[Feishu WS] Unexpected event data type with no recognizable fields: {type(data)}")
                        return
            else:
                body_dict = json.loads(raw_body.decode("utf-8"))

            event_type = body_dict.get("header", {}).get("event_type", "unknown")
            logger.info(f"[Feishu WS] Event received for agent {agent_id}: {event_type}")

            # Import here to avoid circular dependencies
            from app.api.feishu import process_feishu_event

            async with async_session() as db:
                await process_feishu_event(agent_id, body_dict, db)

        except Exception as e:
            logger.exception(f"[Feishu WS] Error processing event for {agent_id}: {e}")

    async def start_client(
        self,
        agent_id: uuid.UUID,
        app_id: str,
        app_secret: str,
        stop_existing: bool = True,
    ):
        """Spawns a WebSocket client fully asynchronously inside FastAPI's loop."""
        if not _HAS_LARK:
            logger.warning("[Feishu WS] lark-oapi not installed, cannot start client")
            return
        if not app_id or not app_secret:
            logger.warning(f"[Feishu WS] Missing app_id or app_secret for {agent_id}, skipping")
            return

        logger.info(f"[Feishu WS] Starting async WS client for agent {agent_id} (App ID: {app_id})")

        # Stop existing client task if any
        if stop_existing and agent_id in self._tasks:
            old_task = self._tasks.pop(agent_id, None)
            if old_task and not old_task.done():
                old_task.cancel()
                logger.info(f"[Feishu WS] Cancelled old WS task for {agent_id}")

        try:
            event_handler = self._create_event_handler(agent_id)
        except Exception as e:
            logger.exception(f"[Feishu WS] Failed to create event handler for {agent_id}: {e}")
            return

        # Instantiate Client — SDK manages connect + receive + ping internally.
        # We set auto_reconnect=True so the SDK handles reconnections.
        client = ws.Client(
            app_id,
            app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            auto_reconnect=True,
        )
        self._clients[agent_id] = client

        # Build scoped proxy bypass: active only during _connect() to avoid
        # permanently replacing websockets.connect for the whole process.
        _no_proxy_ctx = (
            _make_no_proxy_connect(_orig_websockets_connect)
            if _PROXY_PATCH_AVAILABLE
            else None
        )

        # Serialize force-reconnect attempts from ping_loop and health-watch.
        _reconnect_lock = asyncio.Lock()

        async def _scoped_connect():
            """Call client._connect() under the macOS proxy-bypass scope when applicable."""
            if _no_proxy_ctx:
                async with _no_proxy_ctx():
                    await client._connect()
            else:
                await client._connect()

        async def _force_reconnect(reason: str) -> bool:
            """Disconnect + reconnect, gated by _reconnect_lock.

            Bypasses lark-oapi 1.5.3's brittle reconnect path (which depends on
            _receive_message_loop detecting a closed recv() — observed to hang
            silently in production). Returns True on success.
            """
            async with _reconnect_lock:
                logger.warning(
                    f"[Feishu WS] Force-reconnect for agent {agent_id} ({reason})"
                )
                try:
                    await client._disconnect()
                except Exception as de:
                    logger.debug(
                        f"[Feishu WS] disconnect during force-reconnect raised "
                        f"(usually safe): {de}"
                    )
                try:
                    await _scoped_connect()
                    logger.info(
                        f"[Feishu WS] Force-reconnect ok for agent {agent_id} "
                        f"(new conn_id={getattr(client, '_conn_id', None)})"
                    )
                    return True
                except Exception as ce:
                    logger.error(
                        f"[Feishu WS] Force-reconnect failed for agent {agent_id}: {ce}"
                    )
                    return False

        async def _custom_ping_loop():
            """Replacement for SDK's _ping_loop() that triggers reconnect on failure.

            SDK's _ping_loop only logs warnings on send failure and keeps spinning,
            relying entirely on _receive_message_loop to detect death and reconnect.
            When the recv loop hangs, the connection becomes a permanent zombie.
            Here we treat any ping send failure as proof the conn is dead and
            force a reconnect immediately.
            """
            interval = getattr(client, "_ping_interval", 120) or 120
            while True:
                try:
                    if client._conn is not None and _lark_new_ping_frame is not None:
                        frame = _lark_new_ping_frame(int(client._service_id))
                        await client._write_message(frame.SerializeToString())
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    await _force_reconnect(f"ping failed: {e}")
                    interval = getattr(client, "_ping_interval", 120) or 120
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return

        async def _do_full_connect():
            """Initial connect + start the custom ping loop."""
            await _scoped_connect()
            asyncio.create_task(
                _custom_ping_loop(), name=f"feishu-ws-ping-{str(agent_id)[:8]}"
            )

        def _conn_state(conn) -> str:
            """Best-effort representation of the underlying websocket state."""
            if conn is None:
                return "None"
            for attr in ("state", "closed", "close_code"):
                if hasattr(conn, attr):
                    return f"{attr}={getattr(conn, attr)!r}"
            return repr(conn)

        def _is_dead(conn) -> bool:
            if conn is None:
                return True
            # websockets 15.x asyncio API
            state = getattr(conn, "state", None)
            if state is not None:
                return str(state).rsplit(".", 1)[-1] in {"CLOSED", "CLOSING"}
            # legacy API
            if hasattr(conn, "closed"):
                return bool(conn.closed)
            return False

        async def _run_async_client():
            try:
                logger.info(f"[Feishu WS] Connecting for agent {agent_id}")
                await _do_full_connect()
                logger.info(f"[Feishu WS] Connected for agent {agent_id}, receive loop started")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.exception(f"[Feishu WS] Initial connect failed for agent {agent_id}: {e}")

            # Health-watch with active force-reconnect after sustained dead state.
            # We no longer trust the SDK's internal _reconnect path; lark-oapi 1.5.3
            # has been observed to silently hang with conn alive but unresponsive.
            _last_conn_id = getattr(client, "_conn_id", None)
            _dead_seconds = 0
            while True:
                try:
                    await asyncio.sleep(30)

                    conn = client._conn
                    curr_conn_id = getattr(client, "_conn_id", None)
                    recv_alive = sum(
                        1 for t in asyncio.all_tasks()
                        if "_receive_message_loop" in str(t.get_coro()) and not t.done()
                    )

                    if _is_dead(conn):
                        _dead_seconds += 30
                        logger.warning(
                            f"[Feishu WS] {agent_id} connection looks dead "
                            f"(state={_conn_state(conn)}, dead_for={_dead_seconds}s, "
                            f"recv_loops_alive={recv_alive}, last_conn_id={_last_conn_id})"
                        )
                        if _dead_seconds >= 60:
                            ok = await _force_reconnect(
                                f"health-watch detected {_dead_seconds}s of dead conn"
                            )
                            if ok:
                                _dead_seconds = 0
                                _last_conn_id = getattr(client, "_conn_id", None)
                    else:
                        if _dead_seconds > 0:
                            logger.info(
                                f"[Feishu WS] {agent_id} connection healthy again "
                                f"(conn_id={curr_conn_id}, recv_loops={recv_alive})"
                            )
                        _dead_seconds = 0
                        if curr_conn_id != _last_conn_id and curr_conn_id:
                            logger.info(
                                f"[Feishu WS] Connection ID changed for agent {agent_id}: "
                                f"{_last_conn_id} → {curr_conn_id}"
                            )
                            _last_conn_id = curr_conn_id
                except asyncio.CancelledError:
                    logger.info(f"[Feishu WS] Task cancelled for agent {agent_id}")
                    try:
                        await client._disconnect()
                    except Exception:
                        pass
                    return
                except Exception as e:
                    logger.exception(f"[Feishu WS] Health-watch error for agent {agent_id}: {e}")

        task = asyncio.create_task(_run_async_client(), name=f"feishu-ws-async-{str(agent_id)[:8]}")
        self._tasks[agent_id] = task
        logger.info(f"[Feishu WS] Async WS task scheduled for agent {agent_id}")

    async def stop_client(self, agent_id: uuid.UUID):
        """Stops an actively running WebSocket client for an agent."""
        if agent_id in self._tasks:
            task = self._tasks.pop(agent_id)
            if not task.done():
                task.cancel()
                logger.info(f"[Feishu WS] Stopped client task for agent {agent_id}")
        if agent_id in self._clients:
            client = self._clients.pop(agent_id)
            try:
                await client._disconnect()
            except Exception as e:
                logger.error(f"[Feishu WS] Error disconnecting client for {agent_id}: {e}")

    async def start_all(self):
        """Start WS clients for all configured Feishu agents."""
        if not _HAS_LARK:
            logger.info("[Feishu WS] lark-oapi not installed, skipping Feishu WS initialization")
            return
        logger.info("[Feishu WS] Initializing all active Feishu channels...")
        async with async_session() as db:
            result = await db.execute(
                select(ChannelConfig).where(
                    ChannelConfig.is_configured == True,
                    ChannelConfig.channel_type == "feishu",
                )
            )
            configs = result.scalars().all()

        for config in configs:
            extra = config.extra_config or {}
            mode = extra.get("connection_mode", "webhook")
            if mode == "websocket":
                if config.app_id and config.app_secret:
                    await self.start_client(
                        config.agent_id, config.app_id, config.app_secret, stop_existing=False
                    )
                else:
                    logger.warning(f"[Feishu WS] Skipping agent {config.agent_id}: missing credentials")

    def status(self) -> dict:
        """Return status of all active WS tasks."""
        return {
            str(aid): not self._tasks[aid].done()
            for aid in self._tasks
        }


feishu_ws_manager = FeishuWSManager()
