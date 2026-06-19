"""Slash Commands 定義與註冊。

提供 Discord Slash Commands：
- /memory: 查看/管理長期記憶
- /summary: 查看頻道摘要
- /tasks: 查看任務狀態
- /logs: 查看操作日誌
- /agent-status: 查看 Agent 狀態
- /reload-config: 重新載入設定（管理員）
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

import discord
from discord import app_commands

import structlog

from src.agents.manager import AgentManager
from src.memory.long_term import LongTermMemoryService
from src.memory.summary import SummaryService
from src.database.repository import (
    ActionLogRepository,
    TaskRepository,
    MemoryRepository,
    SummaryRepository,
)

logger = structlog.get_logger(__name__)

# Type alias
CommandHandler = Callable[..., Coroutine[Any, Any, None]]


class CommandRegistry:
    """Slash Command 註冊中心。

    統一管理所有 Slash Commands 的定義與註冊。
    """

    def __init__(
        self,
        agent_manager: AgentManager,
        long_term_memory: LongTermMemoryService,
        summary_service: SummaryService,
        action_log_repo: ActionLogRepository,
        task_repo: TaskRepository,
        memory_repo: MemoryRepository,
        summary_repo: SummaryRepository,
    ) -> None:
        self._manager = agent_manager
        self._ltm = long_term_memory
        self._summary = summary_service
        self._action_log = action_log_repo
        self._task_repo = task_repo
        self._memory_repo = memory_repo
        self._summary_repo = summary_repo

    def register_to(self, tree: app_commands.CommandTree, bot: discord.Client) -> None:
        """將所有指令註冊到 CommandTree。

        Args:
            tree: Discord Command Tree。
            bot: Discord Bot 實例。
        """
        _register_memory_commands(tree, bot, self)
        _register_summary_commands(tree, bot, self)
        _register_task_commands(tree, bot, self)
        _register_log_commands(tree, bot, self)
        _register_agent_commands(tree, bot, self)
        _register_admin_commands(tree, bot, self)

        logger.info("slash_commands.registered")


def register_all_commands(
    bot: discord.Client,
    agent_manager: AgentManager,
    long_term_memory: LongTermMemoryService,
    summary_service: SummaryService,
    action_log_repo: ActionLogRepository,
    task_repo: TaskRepository,
    memory_repo: MemoryRepository,
    summary_repo: SummaryRepository,
) -> app_commands.CommandTree:
    """註冊所有 Slash Commands。

    Args:
        bot: Discord Bot 實例。
        agent_manager: Agent 管理器。
        long_term_memory: 長期記憶服務。
        summary_service: 摘要服務。
        action_log_repo: 操作日誌 Repository。
        task_repo: 任務 Repository。
        memory_repo: 記憶 Repository。
        summary_repo: 摘要 Repository。

    Returns:
        設定好的 CommandTree。
    """
    tree = app_commands.CommandTree(bot)
    registry = CommandRegistry(
        agent_manager, long_term_memory, summary_service,
        action_log_repo, task_repo, memory_repo, summary_repo,
    )
    registry.register_to(tree, bot)
    return tree


# ============================================================
# Memory Commands
# ============================================================


def _register_memory_commands(
    tree: app_commands.CommandTree,
    bot: discord.Client,
    registry: CommandRegistry,
) -> None:
    """註冊記憶相關指令。"""

    @tree.command(name="memory", description="查看或管理長期記憶")
    @app_commands.describe(
        action="操作類型",
        category="記憶類別",
        key="記憶鍵值",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="查看", value="view"),
        app_commands.Choice(name="搜尋", value="search"),
        app_commands.Choice(name="伺服器資訊", value="server"),
    ])
    async def memory_command(
        interaction: discord.Interaction,
        action: str = "view",
        category: str | None = None,
        key: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild_id) if interaction.guild_id else "0"

        if action == "server":
            info = await registry._ltm.get_server_info(guild_id)
            if info:
                embed = discord.Embed(
                    title="🏠 伺服器資訊",
                    color=discord.Color.blue(),
                )
                for k, v in info.items():
                    embed.add_field(name=k, value=str(v), inline=True)
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("尚無伺服器記憶", ephemeral=True)

        elif action == "search":
            if category:
                memories = await registry._memory_repo.get_by_guild(guild_id)
                filtered = [m for m in memories if m.get("category") == category]
            else:
                filtered = await registry._memory_repo.get_by_guild(guild_id)

            if filtered:
                lines = []
                for m in filtered[:10]:
                    lines.append(f"**{m.get('key', '?')}**: {m.get('value', '?')}")
                embed = discord.Embed(
                    title="🧠 長期記憶",
                    description="\n".join(lines),
                    color=discord.Color.green(),
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("未找到相符的記憶", ephemeral=True)

        else:
            await interaction.followup.send("使用 `/memory action:搜尋` 或 `/memory action:伺服器資訊`", ephemeral=True)


# ============================================================
# Summary Commands
# ============================================================


def _register_summary_commands(
    tree: app_commands.CommandTree,
    bot: discord.Client,
    registry: CommandRegistry,
) -> None:
    """註冊摘要相關指令。"""

    @tree.command(name="summary", description="查看頻道摘要")
    @app_commands.describe(
        action="操作類型",
        limit="顯示數量",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="查看", value="view"),
        app_commands.Choice(name="產生", value="generate"),
    ])
    async def summary_command(
        interaction: discord.Interaction,
        action: str = "view",
        limit: int = 5,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild_id) if interaction.guild_id else "0"
        channel_id = str(interaction.channel_id) if interaction.channel_id else "0"

        if action == "generate":
            await registry._summary.summarize_channel(guild_id, channel_id)
            await interaction.followup.send("✅ 摘要已產生", ephemeral=True)

        else:
            summaries = await registry._summary_repo.get_latest(
                guild_id, channel_id, limit=min(limit, 10)
            )
            if summaries:
                embed = discord.Embed(
                    title="📝 頻道摘要",
                    color=discord.Color.purple(),
                )
                for s in summaries:
                    content = s.get("content", "")
                    created = s.get("created_at", "")
                    embed.add_field(
                        name=f"摘要 ({created})",
                        value=content[:200] + ("..." if len(content) > 200 else ""),
                        inline=False,
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.followup.send("尚無摘要", ephemeral=True)


# ============================================================
# Task Commands
# ============================================================


def _register_task_commands(
    tree: app_commands.CommandTree,
    bot: discord.Client,
    registry: CommandRegistry,
) -> None:
    """註冊任務相關指令。"""

    @tree.command(name="tasks", description="查看任務狀態")
    @app_commands.describe(
        status="任務狀態篩選",
        limit="顯示數量",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="全部", value="all"),
        app_commands.Choice(name="處理中", value="PROCESSING"),
        app_commands.Choice(name="已完成", value="COMPLETED"),
        app_commands.Choice(name="失敗", value="FAILED"),
        app_commands.Choice(name="重試中", value="RETRY"),
    ])
    async def tasks_command(
        interaction: discord.Interaction,
        status: str = "all",
        limit: int = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if status == "all":
            # 取得佇列狀態
            queue_status = {}
            for agent in registry._manager.get_all_agents():
                queue_status.update(agent.get_status())

            embed = discord.Embed(
                title="📋 任務狀態",
                color=discord.Color.orange(),
            )
            embed.add_field(
                name="佇列狀態",
                value=f"```json\n{queue_status}\n```",
                inline=False,
            )
        else:
            tasks = await registry._task_repo.list_by_status(status, limit=min(limit, 20))
            lines = []
            for t in tasks[:10]:
                lines.append(
                    f"- `{t.get('task_type', '?')}` "
                    f"Agent: {t.get('agent_name', '?')} "
                    f"Status: {t.get('status', '?')}"
                )
            embed = discord.Embed(
                title=f"📋 任務 - {status}",
                description="\n".join(lines) if lines else "無任務",
                color=discord.Color.orange(),
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================
# Log Commands
# ============================================================


def _register_log_commands(
    tree: app_commands.CommandTree,
    bot: discord.Client,
    registry: CommandRegistry,
) -> None:
    """註冊日誌相關指令。"""

    @tree.command(name="logs", description="查看操作日誌")
    @app_commands.describe(
        limit="顯示數量",
        agent_name="篩選 Agent",
    )
    async def logs_command(
        interaction: discord.Interaction,
        limit: int = 10,
        agent_name: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild_id) if interaction.guild_id else "0"

        logs = await registry._action_log.get_recent(
            guild_id, limit=min(limit, 20)
        )

        if agent_name:
            logs = [l for l in logs if l.get("agent_name") == agent_name]

        if logs:
            lines = []
            for l in logs[:10]:
                lines.append(
                    f"- 🤖 {l.get('agent_name', '?')}: "
                    f"{l.get('action', '?')} "
                    f"[{l.get('status', '?')}]"
                )
            embed = discord.Embed(
                title="📜 操作日誌",
                description="\n".join(lines),
                color=discord.Color.dark_grey(),
            )
        else:
            embed = discord.Embed(
                title="📜 操作日誌",
                description="尚無日誌",
                color=discord.Color.dark_grey(),
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================
# Agent Status Commands
# ============================================================


def _register_agent_commands(
    tree: app_commands.CommandTree,
    bot: discord.Client,
    registry: CommandRegistry,
) -> None:
    """註冊 Agent 狀態指令。"""

    @tree.command(name="agent-status", description="查看 Agent 狀態")
    async def agent_status_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        agents = registry._manager.get_all_agents()
        if not agents:
            await interaction.followup.send("尚無 Agent", ephemeral=True)
            return

        embed = discord.Embed(
            title="🤖 Agent 狀態",
            color=discord.Color.teal(),
        )

        for agent in agents:
            status = agent.get_status()
            embed.add_field(
                name=agent.name,
                value=(
                    f"人格: {status['personality'][:50]}\n"
                    f"Context: {status['context_tokens']}/{status['token_budget']} tokens\n"
                    f"Bot: {'✅ 在線' if status['bot_ready'] else '❌ 離線'}"
                ),
                inline=True,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================
# Admin Commands
# ============================================================


def _register_admin_commands(
    tree: app_commands.CommandTree,
    bot: discord.Client,
    registry: CommandRegistry,
) -> None:
    """註冊管理員指令。"""

    @tree.command(name="reload-config", description="重新載入設定（管理員）")
    @app_commands.checks.has_permissions(administrator=True)
    async def reload_config_command(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        try:
            from src.config.settings import ConfigLoader
            new_config = ConfigLoader.load()
            # TODO: 熱更新邏輯
            await interaction.followup.send("✅ 設定已重新載入", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ 載入失敗: {exc}", ephemeral=True
            )

    @reload_config_command.error
    async def reload_config_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ 你沒有管理員權限", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"❌ 錯誤: {error}", ephemeral=True
            )
