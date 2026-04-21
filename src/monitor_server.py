"""
Little Helper - Monitor server.

Serves hardware monitor snapshots over HTTP and WebSocket using Starlette.
"""

import asyncio
from contextlib import asynccontextmanager
import logging
import threading

import system_overlay

log = logging.getLogger("little_helper.monitor_server")

_STARLETTE_IMPORT_ERROR = None

try:
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocketDisconnect
    import uvicorn
    import websockets  # noqa: F401
except Exception as exc:
    Starlette = None
    JSONResponse = None
    Route = None
    WebSocketRoute = None
    WebSocketDisconnect = Exception
    uvicorn = None
    _STARLETTE_IMPORT_ERROR = exc


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9980
DEFAULT_WS_INTERVAL_MS = 1000
MIN_WS_INTERVAL_MS = 200
MAX_WS_INTERVAL_MS = 60000


def monitor_server_dependencies_available() -> tuple[bool, str | None]:
    if _STARLETTE_IMPORT_ERROR is None:
        return True, None
    return False, str(_STARLETTE_IMPORT_ERROR)


def normalize_monitor_server_config(config: dict) -> dict:
    raw_cfg = config.get("monitor_server", {})
    host = str(raw_cfg.get("host", DEFAULT_HOST)).strip() or DEFAULT_HOST
    token = str(raw_cfg.get("token", "")).strip()
    try:
        port = int(raw_cfg.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    port = max(1, min(65535, port))
    return {
        "enabled": bool(raw_cfg.get("enabled", False)),
        "host": host,
        "port": port,
        "token": token,
    }


def get_monitor_urls(server_cfg: dict) -> dict:
    host = server_cfg.get("host", DEFAULT_HOST)
    port = server_cfg.get("port", DEFAULT_PORT)
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    return {
        "http": f"http://{display_host}:{port}/api/monitor",
        "websocket": f"ws://{display_host}:{port}/ws/monitor",
    }


def _extract_request_token(headers, query_params) -> str | None:
    auth_header = headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None

    for header_name in ("x-monitor-token", "x-api-token", "x-token"):
        header_value = headers.get(header_name, "")
        if header_value:
            return header_value.strip()

    for query_name in ("token", "access_token"):
        query_value = query_params.get(query_name)
        if query_value:
            return query_value.strip()

    return None


def _is_authorized(token: str, headers, query_params) -> bool:
    if not token:
        return True
    return _extract_request_token(headers, query_params) == token


def _parse_interval_ms(raw_value) -> int:
    try:
        interval_ms = int(raw_value)
    except (TypeError, ValueError):
        interval_ms = DEFAULT_WS_INTERVAL_MS
    return max(MIN_WS_INTERVAL_MS, min(MAX_WS_INTERVAL_MS, interval_ms))


def _create_app(server_cfg: dict, ready_event: threading.Event):
    async def homepage(request):
        return JSONResponse(
            {
                "service": "little-helper-monitor",
                "auth_required": bool(server_cfg["token"]),
                "endpoints": {
                    "health": "/health",
                    "monitor": "/api/monitor",
                    "websocket": "/ws/monitor",
                },
            }
        )

    async def healthcheck(request):
        return JSONResponse(
            {
                "status": "ok",
                "auth_required": bool(server_cfg["token"]),
                "bind": {
                    "host": server_cfg["host"],
                    "port": server_cfg["port"],
                },
            }
        )

    async def monitor_snapshot(request):
        if not _is_authorized(server_cfg["token"], request.headers, request.query_params):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return JSONResponse(system_overlay.get_monitor_snapshot())

    async def monitor_websocket(websocket):
        if not _is_authorized(server_cfg["token"], websocket.headers, websocket.query_params):
            await websocket.close(code=4401, reason="Unauthorized")
            return

        await websocket.accept()
        interval_ms = _parse_interval_ms(websocket.query_params.get("interval_ms"))

        try:
            while True:
                await websocket.send_json(
                    {
                        "type": "snapshot",
                        "payload": system_overlay.get_monitor_snapshot(max_age_ms=interval_ms),
                    }
                )
                await asyncio.sleep(interval_ms / 1000.0)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug(f"Monitor websocket closed with error: {exc}")

    routes = [
        Route("/", homepage),
        Route("/health", healthcheck),
        Route("/api/monitor", monitor_snapshot),
        WebSocketRoute("/ws/monitor", monitor_websocket),
    ]

    @asynccontextmanager
    async def lifespan(_app):
        ready_event.set()
        log.info(
            "Monitor server listening on %s:%s",
            server_cfg["host"],
            server_cfg["port"],
        )
        try:
            yield
        finally:
            log.info("Monitor server shutdown complete")

    try:
        return Starlette(debug=False, routes=routes, lifespan=lifespan)
    except TypeError:
        app = Starlette(debug=False, routes=routes)

        @app.on_event("startup")
        async def _on_startup():
            ready_event.set()
            log.info(
                "Monitor server listening on %s:%s",
                server_cfg["host"],
                server_cfg["port"],
            )

        @app.on_event("shutdown")
        async def _on_shutdown():
            log.info("Monitor server shutdown complete")

        return app


class MonitorServerController:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._server = None
        self._ready_event = threading.Event()
        self._startup_error = None
        self._server_cfg = normalize_monitor_server_config({})

    def start(self, config: dict) -> bool:
        server_cfg = normalize_monitor_server_config(config)
        if not server_cfg["enabled"]:
            self.stop()
            return False

        deps_ok, deps_error = monitor_server_dependencies_available()
        if not deps_ok:
            raise RuntimeError(f"Monitor server dependencies are unavailable: {deps_error}")

        with self._lock:
            if self.is_running() and self._server_cfg == server_cfg:
                return True

        self.stop()

        self._ready_event.clear()
        self._startup_error = None
        self._server_cfg = server_cfg
        thread = threading.Thread(
            target=self._run_server,
            args=(server_cfg,),
            daemon=True,
            name="monitor-server",
        )

        with self._lock:
            self._thread = thread

        thread.start()
        if not self._ready_event.wait(timeout=5):
            if self._startup_error is not None:
                raise RuntimeError(self._startup_error)
            if not thread.is_alive():
                raise RuntimeError(
                    f"Monitor server failed to start on {server_cfg['host']}:{server_cfg['port']}"
                )
            raise RuntimeError("Monitor server startup timed out")
        if self._startup_error is not None:
            raise RuntimeError(self._startup_error)
        if not thread.is_alive():
            raise RuntimeError(
                f"Monitor server failed to start on {server_cfg['host']}:{server_cfg['port']}"
            )
        return True

    def stop(self) -> None:
        thread = None
        server = None
        with self._lock:
            thread = self._thread
            server = self._server
            self._thread = None
            self._server = None
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
            if thread.is_alive() and server is not None:
                server.force_exit = True
                thread.join(timeout=2)

    def restart(self, config: dict) -> bool:
        self.stop()
        return self.start(config)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def current_config(self) -> dict:
        return dict(self._server_cfg)

    def _run_server(self, server_cfg: dict) -> None:
        try:
            app = _create_app(server_cfg, self._ready_event)
            config = uvicorn.Config(
                app,
                host=server_cfg["host"],
                port=server_cfg["port"],
                log_level="warning",
                log_config=None,
                access_log=False,
                server_header=False,
                ws="websockets",
                lifespan="on",
            )
            server = uvicorn.Server(config)
            with self._lock:
                self._server = server
            server.run()
            if not server.started and self._startup_error is None:
                self._startup_error = (
                    f"Monitor server failed to bind {server_cfg['host']}:{server_cfg['port']}"
                )
                self._ready_event.set()
        except Exception as exc:
            log.error(f"Monitor server crashed: {exc}", exc_info=True)
            self._startup_error = str(exc)
            self._ready_event.set()
        finally:
            with self._lock:
                self._server = None
                if self._thread is not None and not self._thread.is_alive():
                    self._thread = None


_controller = MonitorServerController()


def start_monitor_server(config: dict) -> bool:
    return _controller.start(config)


def stop_monitor_server() -> None:
    _controller.stop()


def restart_monitor_server(config: dict) -> bool:
    return _controller.restart(config)


def monitor_server_is_running() -> bool:
    return _controller.is_running()


def get_running_monitor_server_config() -> dict:
    return _controller.current_config()