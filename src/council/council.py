"""AI Council 機制。

多 Agent 討論系統，具有：
- 訊息上限防止無限迴圈
- 討論自動終止與決策
- 反迴圈保護（偵測重複內容）
- 決策投票 / 共識機制
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import discord

import structlog

from src.config.settings import CouncilConfig
from src.agents.agent import Agent
from src.ai.provider import AIProvider
from src.database.repository import ActionLogRepository

logger = structlog.get_logger(__name__)


class CouncilState(Enum):
    """Council 討論狀態。"""

    IDLE = "idle"
    DISCUSSING = "discussing"
    VOTING = "voting"
    CONCLUDED = "concluded"
    TIMEOUT = "timeout"


@dataclass
class CouncilMessage:
    """Council 討論訊息。"""

    agent_name: str
    content: str
    round_number: int
    is_vote: bool = False
    vote_decision: str | None = None  # "approve" / "deny" / "abstain"


class AICouncil:
    """AI Council 系統。

    當一個議題需要多個 Agent 討論時啟動 Council。
    流程：
    1. 發起議題（由 Agent 或 Human 觸發）
    2. 各 Agent 依序發言
    3. 反迴圈偵測
    4. 到達上限或達成共識後投票
    5. 產生最終決策

    使用 #ai-council Discord 頻道作為討論區。
    """

    def __init__(
        self,
        config: CouncilConfig,
        agents: list[Agent],
        ai_provider: AIProvider,
        action_log_repo: ActionLogRepository,
    ) -> None:
        """初始化。

        Args:
            config: Council 設定。
            agents: 所有 Agent 實例。
            ai_provider: AI Provider。
            action_log_repo: 操作日誌 Repository。
        """
        self._config = config
        self._agents = {a.name: a for a in agents}
        self._ai = ai_provider
        self._action_log = action_log_repo

        # 當前討論狀態（per guild）
        self._states: dict[str, CouncilState] = {}
        self._discussions: dict[str, list[CouncilMessage]] = {}
        self._rounds: dict[str, int] = {}

        # 反迴圈：追蹤最近 N 條訊息的相似度
        self._recent_contents: dict[str, list[str]] = {}

        self._log = logger.bind(component="council")

    # ---- 發起討論 ----

    async def start_discussion(
        self,
        guild_id: str,
        topic: str,
        initiator: str,
        channel: discord.TextChannel | None = None,
    ) -> str:
        """發起 Council 討論。

        Args:
            guild_id: 伺服器 ID。
            topic: 討論主題。
            initiator: 發起者名稱。
            channel: Council 頻道（可選，自動尋找）。

        Returns:
            討論結果。
        """
        if not self._config.enabled:
            return "Council 功能未啟用"

        # 初始化討論狀態
        self._states[guild_id] = CouncilState.DISCUSSING
        self._discussions[guild_id] = []
        self._rounds[guild_id] = 0
        self._recent_contents[guild_id] = []

        self._log.info(
            "council.started",
            guild=guild_id,
            topic=topic[:100],
            initiator=initiator,
        )

        # 發布主題
        if channel:
            await channel.send(
                f"🏛️ **Council 討論開始**\n"
                f"**發起者**: {initiator}\n"
                f"**主題**: {topic}\n"
                f"**參與者**: {', '.join(self._agents.keys())}\n"
                f"---"
            )

        # 記錄 Action Log
        await self._action_log.insert(
            guild_id=guild_id,
            agent_name=initiator,
            reason=f"Council 討論: {topic[:100]}",
            action="start_council",
            tool_name="council",
            parameters={"topic": topic},
            safety_level="SAFE",
        )

        # 各 Agent 依序發言
        result = await self._run_discussion(guild_id, topic, channel)

        # 清理狀態
        self._states[guild_id] = CouncilState.CONCLUDED

        return result

    async def _run_discussion(
        self,
        guild_id: str,
        topic: str,
        channel: discord.TextChannel | None,
    ) -> str:
        """執行討論流程。"""
        max_messages = self._config.max_messages
        agent_names = list(self._agents.keys())

        for round_num in range(1, max_messages + 1):
            self._rounds[guild_id] = round_num

            for agent_name in agent_names:
                agent = self._agents.get(agent_name)
                if not agent:
                    continue

                # 取得目前討論內容
                discussion = self._discussions[guild_id]
                context = self._format_discussion(discussion, topic, round_num)

                # 呼叫 AI 產生回應
                try:
                    response = await self._ai.chat(
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    f"你是 {agent_name}，正在參與 Council 討論。\n"
                                    f"請針對主題發表你的看法，或對其他 Agent 的觀點回應。\n"
                                    f"保持簡潔，每次發言不超過 200 字。"
                                ),
                            },
                            {"role": "user", "content": context},
                        ],
                        agent_name=agent_name,
                        tools_enabled=False,
                    )

                    content = response.content or "（無回應）"

                except Exception as exc:
                    self._log.error(
                        "council.agent_error",
                        agent=agent_name,
                        error=str(exc),
                    )
                    content = "（回應失敗）"

                # 記錄訊息
                msg = CouncilMessage(
                    agent_name=agent_name,
                    content=content,
                    round_number=round_num,
                )
                discussion.append(msg)
                self._recent_contents[guild_id].append(content)

                # 發送到 Discord
                if channel:
                    await channel.send(f"**{agent_name}** (Round {round_num}): {content}")

                # 反迴圈偵測
                if self._detect_loop(guild_id):
                    self._log.warning("council.loop_detected", guild=guild_id)
                    if channel:
                        await channel.send("⚠️ 偵測到重複內容，提前結束討論。")
                    return await self._conclude(guild_id, topic, channel)

                # 檢查是否達成共識
                if self._check_consensus(guild_id):
                    self._log.info("council.consensus_reached", guild=guild_id)
                    if channel:
                        await channel.send("✅ 已達成共識！")
                    return await self._conclude(guild_id, topic, channel)

        # 到達上限
        self._log.info("council.max_messages_reached", guild=guild_id)
        if channel:
            await channel.send(f"⏰ 已達到訊息上限 ({max_messages})，進入投票階段。")

        return await self._vote(guild_id, topic, channel)

    def _format_discussion(
        self, discussion: list[CouncilMessage], topic: str, round_num: int
    ) -> str:
        """格式化討論內容供 AI 參考。"""
        lines = [f"討論主題: {topic}", f"目前第 {round_num} 輪", "---"]

        for msg in discussion[-10:]:  # 只取最近 10 條避免過長
            lines.append(f"{msg.agent_name}: {msg.content}")

        lines.append("---")
        lines.append("請發表你的看法：")
        return "\n".join(lines)

    # ---- 反迴圈偵測 ----

    def _detect_loop(self, guild_id: str) -> bool:
        """偵測討論是否陷入迴圈。

        檢查最近 4 條訊息是否有過度相似。
        """
        recent = self._recent_contents.get(guild_id, [])
        if len(recent) < 4:
            return False

        last_4 = recent[-4:]
        # 簡單重複偵測：4 條中有 3 條相同
        if len(set(last_4)) <= 1:
            return True

        # 關鍵詞重複偵測
        keywords = [self._extract_keywords(c) for c in last_4]
        if len(keywords) >= 4:
            overlap = set(keywords[0]) & set(keywords[1]) & set(keywords[2]) & set(keywords[3])
            if len(overlap) >= 3:
                return True

        return False

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """從文字中萃取關鍵詞（簡易版）。"""
        # 移除標點符號，分詞，取長度 >= 2 的詞
        words = re.findall(r"\w{2,}", text.lower())
        return words[:10]  # 取前 10 個

    # ---- 共識偵測 ----

    def _check_consensus(self, guild_id: str) -> bool:
        """偵測是否已達成共識。

        簡單規則：如果連續 2 輪所有 Agent 都表達類似立場。
        """
        discussion = self._discussions.get(guild_id, [])
        if len(discussion) < len(self._agents) * 2:
            return False

        # 取最近一輪的所有發言
        current_round = self._rounds.get(guild_id, 0)
        recent = [m for m in discussion if m.round_number == current_round]

        # 簡單偵測：所有發言都包含「同意」或「贊成」
        agreement_keywords = {"同意", "贊成", "認同", "agree", "concur", "same"}
        if len(recent) >= 2:
            all_agree = all(
                any(kw in msg.content.lower() for kw in agreement_keywords)
                for msg in recent
            )
            if all_agree:
                return True

        return False

    # ---- 投票 ----

    async def _vote(
        self,
        guild_id: str,
        topic: str,
        channel: discord.TextChannel | None,
    ) -> str:
        """進行投票。"""
        self._states[guild_id] = CouncilState.VOTING

        if channel:
            await channel.send("🏛️ **進入投票階段**")

        votes: dict[str, str] = {}

        for agent_name, agent in self._agents.items():
            discussion = self._discussions[guild_id]
            context = self._format_discussion(discussion, topic, 0)
            context += "\n\n請投票：approve（贊成）、deny（反對）、abstain（棄權）"

            try:
                response = await self._ai.chat(
                    messages=[
                        {
                            "role": "system",
                            "content": f"你是 {agent_name}，請根據討論結果投票。只回覆 approve/deny/abstain。",
                        },
                        {"role": "user", "content": context},
                    ],
                    agent_name=agent_name,
                    tools_enabled=False,
                )

                vote_text = (response.content or "abstain").lower().strip()

                if "approve" in vote_text or "贊成" in vote_text:
                    vote = "approve"
                elif "deny" in vote_text or "反對" in vote_text:
                    vote = "deny"
                else:
                    vote = "abstain"

                votes[agent_name] = vote

                # 記錄投票
                msg = CouncilMessage(
                    agent_name=agent_name,
                    content=f"投票: {vote}",
                    round_number=self._rounds.get(guild_id, 0),
                    is_vote=True,
                    vote_decision=vote,
                )
                discussion.append(msg)

                if channel:
                    emoji = {"approve": "✅", "deny": "❌", "abstain": "⚪"}[vote]
                    await channel.send(f"**{agent_name}** 投票: {emoji} {vote}")

            except Exception as exc:
                self._log.error("council.vote_error", agent=agent_name, error=str(exc))
                votes[agent_name] = "abstain"

        # 統計結果
        approve_count = sum(1 for v in votes.values() if v == "approve")
        deny_count = sum(1 for v in votes.values() if v == "deny")
        abstain_count = sum(1 for v in votes.values() if v == "abstain")

        result = (
            f"📊 **投票結果**\n"
            f"✅ 贊成: {approve_count}\n"
            f"❌ 反對: {deny_count}\n"
            f"⚪ 棄權: {abstain_count}\n"
        )

        if approve_count > deny_count:
            decision = "approved"
            result += "\n**決策: 通過** ✅"
        elif deny_count > approve_count:
            decision = "denied"
            result += "\n**決策: 否決** ❌"
        else:
            decision = "no_consensus"
            result += "\n**決策: 未達共識** ⚖️"

        if channel:
            await channel.send(result)

        # 記錄 Action Log
        await self._action_log.insert(
            guild_id=guild_id,
            agent_name="council",
            reason=f"Council 投票: {topic[:100]}",
            action=f"council_vote_{decision}",
            tool_name="council",
            parameters={"votes": votes, "decision": decision},
            safety_level="SAFE",
        )

        return result

    # ---- 結論 ----

    async def _conclude(
        self,
        guild_id: str,
        topic: str,
        channel: discord.TextChannel | None,
    ) -> str:
        """產生討論結論。"""
        discussion = self._discussions.get(guild_id, [])

        if not discussion:
            return "討論無內容"

        # 請 AI 總結
        discussion_text = "\n".join(
            f"{m.agent_name}: {m.content}" for m in discussion
        )

        try:
            response = await self._ai.chat(
                messages=[
                    {"role": "system", "content": "請根據以下 Council 討論產生簡潔結論（100 字以內）。"},
                    {"role": "user", "content": f"主題: {topic}\n\n討論:\n{discussion_text}"},
                ],
                agent_name="council",
                tools_enabled=False,
            )

            conclusion = response.content or "無法產生結論"

        except Exception:
            conclusion = "結論產生失敗"

        if channel:
            await channel.send(f"📝 **結論**: {conclusion}")

        return conclusion

    # ---- 狀態查詢 ----

    def get_state(self, guild_id: str) -> CouncilState:
        """取得 Council 狀態。"""
        return self._states.get(guild_id, CouncilState.IDLE)

    def get_discussion(self, guild_id: str) -> list[CouncilMessage]:
        """取得討論記錄。"""
        return self._discussions.get(guild_id, [])

    async def force_stop(self, guild_id: str) -> None:
        """強制停止討論。"""
        self._states[guild_id] = CouncilState.TIMEOUT
        self._log.info("council.force_stopped", guild=guild_id)
