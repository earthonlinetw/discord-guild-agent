"""Dashboard Web server.

提供 FastAPI API 與靜態前端頁面。FastAPI / uvicorn 以 lazy import 載入，
缺少 web optional dependencies 時不會阻止 Discord bot 啟動。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import structlog

from src.config.settings import DashboardConfig
from src.dashboard.service import DashboardService

logger = structlog.get_logger(__name__)


def _to_jsonable(value: Any) -> Any:
    """Convert dataclasses and repository rows into JSON-friendly values."""
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_jsonable(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    return value


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """Parse JSON columns commonly used by repositories."""
    normalized = dict(row)
    for key in ("parameters", "payload", "arguments"):
        raw = normalized.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            normalized[key] = json.loads(raw)
        except json.JSONDecodeError:
            pass
    return normalized


class DashboardServer:
    """可選的 Dashboard HTTP server。"""

    def __init__(self, service: DashboardService, config: DashboardConfig) -> None:
        self._service = service
        self._config = config
        self._server: Any | None = None
        self._task: asyncio.Task[Any] | None = None
        self._log = logger.bind(component="dashboard_server")

    @property
    def url(self) -> str:
        """Dashboard URL。"""
        return f"http://{self._config.host}:{self._config.port}"

    async def start(self) -> bool:
        """啟動 Dashboard server。

        Returns:
            True 表示 server 已啟動，False 表示停用或缺少依賴。
        """
        if not self._config.enabled:
            self._log.info("dashboard.disabled")
            return False

        try:
            import uvicorn
        except ImportError:
            self._log.warning(
                "dashboard.dependencies_missing",
                hint="Install web dependencies with: pip install -e .[web]",
            )
            return False

        try:
            app = self._create_app()
        except RuntimeError as exc:
            self._log.warning(
                "dashboard.dependencies_missing",
                error=str(exc),
                hint="Install web dependencies with: pip install -e .[web]",
            )
            return False

        uvicorn_config = uvicorn.Config(
            app,
            host=self._config.host,
            port=self._config.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(uvicorn_config)
        self._task = asyncio.create_task(
            self._server.serve(),
            name="dashboard-server",
        )
        await asyncio.sleep(0)
        if self._task.done():
            exc = self._task.exception()
            self._log.error("dashboard.start_failed", error=str(exc) if exc else "server stopped")
            self._server = None
            self._task = None
            return False
        self._log.info("dashboard.started", url=self.url)
        return True

    async def stop(self) -> None:
        """停止 Dashboard server。"""
        if not self._server or not self._task:
            return

        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except TimeoutError:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        finally:
            self._server = None
            self._task = None
            self._log.info("dashboard.stopped")

    def _create_app(self) -> Any:
        """建立 FastAPI app。"""
        try:
            from fastapi import FastAPI, HTTPException, Query
            from fastapi.responses import FileResponse
            from fastapi.staticfiles import StaticFiles
        except ImportError as exc:
            raise RuntimeError("Dashboard web dependencies are not installed") from exc

        static_dir = Path(__file__).with_name("static")
        index_file = static_dir / "index.html"

        app = FastAPI(title=self._config.title)

        @app.get("/api/health")
        async def health() -> dict[str, Any]:
            return {"ok": True, "title": self._config.title}

        @app.get("/api/overview")
        async def overview() -> dict[str, Any]:
            return _to_jsonable(await self._service.get_system_overview())

        @app.get("/api/agents")
        async def agents() -> list[dict[str, Any]]:
            return _to_jsonable(await self._service.get_all_agents())

        @app.get("/api/queue")
        async def queue() -> dict[str, Any]:
            return _to_jsonable(await self._service.get_queue_status())

        @app.get("/api/council")
        async def council() -> dict[str, Any]:
            return _to_jsonable(await self._service.get_council_status())

        @app.get("/api/override")
        async def override() -> dict[str, Any]:
            return _to_jsonable(await self._service.get_override_status())

        @app.get("/api/guilds")
        async def guilds() -> list[dict[str, Any]]:
            return _to_jsonable(await self._service.get_guilds())

        @app.get("/api/tools")
        async def tools() -> list[dict[str, Any]]:
            return _to_jsonable(await self._service.get_tools())

        @app.get("/api/action-logs")
        async def action_logs(
            guild_id: str = Query(..., min_length=1),
            limit: int = Query(50, ge=1, le=200),
        ) -> list[dict[str, Any]]:
            rows = await self._service.get_action_logs(guild_id, limit=limit)
            return [_normalize_row(row) for row in rows]

        @app.get("/api/tool-calls")
        async def tool_calls(
            guild_id: str = Query(..., min_length=1),
            limit: int = Query(50, ge=1, le=200),
        ) -> list[dict[str, Any]]:
            rows = await self._service.get_tool_calls(guild_id, limit=limit)
            return [_normalize_row(row) for row in rows]

        @app.get("/api/memory")
        async def memory(
            guild_id: str = Query(..., min_length=1),
            category: str | None = None,
        ) -> list[dict[str, Any]]:
            return await self._service.get_memory(guild_id, category=category)

        @app.get("/api/tasks")
        async def tasks(
            status: str | None = None,
            limit: int = Query(50, ge=1, le=200),
        ) -> list[dict[str, Any]]:
            if status == "all":
                status = None
            rows = await self._service.get_tasks(status=status, limit=limit)
            return [_normalize_row(row) for row in rows]

        if static_dir.exists():
            app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

        @app.get("/")
        async def index() -> FileResponse:
            if not index_file.exists():
                raise HTTPException(status_code=404, detail="Dashboard assets not found")
            return FileResponse(index_file)

        return app