"""資料庫 Migration 管理器。

負責追蹤已套用的 migration 並執行新的 migration 腳本。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from src.database.connection import ConnectionProvider

logger = structlog.get_logger(__name__)

# Migration 腳本目錄
MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


class MigrationManager:
    """Migration 管理器。

    確保資料庫 schema 版本一致，支援冪等執行。
    """

    def __init__(self, provider: ConnectionProvider) -> None:
        self._provider = provider

    async def ensure_migrations_table(self) -> None:
        """確保 migrations 資料表存在。"""
        await self._provider.executescript("""
            CREATE TABLE IF NOT EXISTS migrations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                version     TEXT NOT NULL UNIQUE,
                applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)

    async def get_applied_versions(self) -> list[str]:
        """取得已套用的 migration 版本清單。"""
        rows = await self._provider.fetchall("SELECT version FROM migrations ORDER BY id")
        return [row["version"] for row in rows]

    async def apply_migration(self, version: str, script: str) -> None:
        """套用單一 migration。

        Args:
            version: 版本標識，例如 "001_initial"。
            script: SQL 腳本內容。
        """
        logger.info("migration.applying", version=version)
        try:
            await self._provider.executescript(script)
            await self._provider.execute(
                "INSERT INTO migrations (version) VALUES (?)", (version,)
            )
            logger.info("migration.applied", version=version)
        except Exception as exc:
            logger.error("migration.failed", version=version, error=str(exc))
            raise

    async def run_pending(self) -> list[str]:
        """掃描並執行所有尚未套用的 migration。

        Returns:
            已套用的版本清單。
        """
        await self.ensure_migrations_table()
        applied = await self.get_applied_versions()

        if not MIGRATIONS_DIR.exists():
            logger.warning("migration.no_dir", path=str(MIGRATIONS_DIR))
            return []

        newly_applied: list[str] = []
        # 排序確保按版本順序執行
        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))

        for sql_file in sql_files:
            version = sql_file.stem  # 例如 "001_initial"
            if version in applied:
                logger.debug("migration.skip", version=version)
                continue

            script = sql_file.read_text(encoding="utf-8")
            await self.apply_migration(version, script)
            newly_applied.append(version)

        if not newly_applied:
            logger.info("migration.up_to_date")

        return newly_applied
