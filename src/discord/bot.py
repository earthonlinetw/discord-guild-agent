"""Discord Bot 工廠。

負責建立正確設定的 discord.Client 實例。
"""

from __future__ import annotations

import discord

import structlog

logger = structlog.get_logger(__name__)


class BotFactory:
    """Discord Bot 工廠。

    建立具有正確 Intents 與設定的 Bot 實例。
    """

    @staticmethod
    def create(name: str) -> discord.Client:
        """建立 Discord Bot 實例。

        Args:
            name: Agent 名稱，用於識別。

        Returns:
            設定好 Intents 的 discord.Client。
        """
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.reactions = True

        bot = discord.Client(
            intents=intents,
            activity=discord.Game(name=f"管理中 | {name}"),
        )

        logger.info("bot_factory.created", name=name)
        return bot
