from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import aiosqlite
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

log = logging.getLogger(__name__)

BUCKETS = ("weekly", "monthly", "yearly", "alltime")


@dataclass
class BotConfig:
    token: str
    timezone: timezone
    timezone_name: str
    maintainer_id: Optional[int]
    db_path: Path = Path("voice_time.db")
    leaderboard_limit: int = 10


def _build_timezone(name: str) -> timezone:
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - invalid tz string
        log.warning("Invalid BOT_TIMEZONE '%s'. Falling back to UTC.", name)
        return timezone.utc


def _parse_int(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        log.warning("Invalid integer value '%s'.", value)
        return None


def humanize_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}小時 {minutes:02d}分鐘 {secs:02d}秒"


def load_config() -> BotConfig:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("請先在 .env 內設定 DISCORD_TOKEN。")

    tz_name = os.getenv("BOT_TIMEZONE", "UTC")
    timezone_obj = _build_timezone(tz_name)
    maintainer_id = _parse_int(os.getenv("MAINTAINER_ID"))

    return BotConfig(
        token=token,
        timezone=timezone_obj,
        timezone_name=tz_name,
        maintainer_id=maintainer_id,
    )


class VoiceTrackingService:
    """Encapsulates all persistence and aggregation logic for voice tracking."""

    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.db_path = config.db_path
        self.leaderboard_limit = config.leaderboard_limit
        self.db: Optional[aiosqlite.Connection] = None
        self.db_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode = WAL;")
        await self.db.execute("PRAGMA foreign_keys = ON;")
        await self._initialize_schema()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def _initialize_schema(self) -> None:
        assert self.db is not None
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS voice_time (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                bucket TEXT NOT NULL,
                seconds INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, bucket)
            );

            CREATE TABLE IF NOT EXISTS active_sessions (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_voice_time_guild_bucket_seconds
                ON voice_time (guild_id, bucket, seconds DESC);

            CREATE INDEX IF NOT EXISTS idx_voice_time_guild_user
                ON voice_time (guild_id, user_id);

            CREATE INDEX IF NOT EXISTS idx_active_sessions_guild_user
                ON active_sessions (guild_id, user_id);
            """
        )
        await self.db.commit()

    async def reconcile_active_sessions(self, client: discord.Client) -> None:
        assert self.db is not None
        now = datetime.now(timezone.utc)
        async with self.db_lock:
            cursor = await self.db.execute(
                "SELECT guild_id, user_id, channel_id, started_at FROM active_sessions"
            )
            rows = await cursor.fetchall()

        recorded_map: Dict[Tuple[int, int], Tuple[int, datetime]] = {
            (row[0], row[1]): (row[2], datetime.fromisoformat(row[3])) for row in rows
        }

        live_voice: Dict[Tuple[int, int], int] = {}
        for guild in client.guilds:
            channels = list(guild.voice_channels) + list(getattr(guild, "stage_channels", []))
            for channel in channels:
                for member in channel.members:
                    if member.bot:
                        continue
                    live_voice[(guild.id, member.id)] = channel.id

        finalize_ops: List[Tuple[int, int, datetime]] = []
        start_ops: List[Tuple[int, int, int]] = []

        for pair, (channel_id, started_at) in recorded_map.items():
            live_channel = live_voice.get(pair)
            if live_channel == channel_id:
                continue
            finalize_ops.append((pair[0], pair[1], started_at))
            if live_channel is not None:
                start_ops.append((pair[0], pair[1], live_channel))

        for pair, live_channel in live_voice.items():
            if pair not in recorded_map:
                start_ops.append((pair[0], pair[1], live_channel))

        if not finalize_ops and not start_ops:
            return

        async with self.db_lock:
            for guild_id, user_id, started_at in finalize_ops:
                await self._finalize_session_locked(guild_id, user_id, started_at, now)
            for guild_id, user_id, channel_id in start_ops:
                await self._start_session_locked(guild_id, user_id, channel_id)
            await self.db.commit()

    async def start_session(self, guild_id: int, user_id: int, channel_id: int) -> None:
        async with self.db_lock:
            await self._start_session_locked(guild_id, user_id, channel_id)
            await self.db.commit()

    async def end_session(self, guild_id: int, user_id: int) -> None:
        now = datetime.now(timezone.utc)
        async with self.db_lock:
            assert self.db is not None
            cursor = await self.db.execute(
                "SELECT started_at FROM active_sessions WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return
            started_dt = datetime.fromisoformat(row[0])
            await self._finalize_session_locked(guild_id, user_id, started_dt, now)
            await self.db.commit()

    async def sync_active_sessions(self, guild_id: Optional[int] = None) -> None:
        assert self.db is not None
        now = datetime.now(timezone.utc)
        query = "SELECT guild_id, user_id, started_at FROM active_sessions"
        params: Sequence[int] = ()
        if guild_id is not None:
            query += " WHERE guild_id = ?"
            params = (guild_id,)

        async with self.db_lock:
            cursor = await self.db.execute(query, params)
            rows = await cursor.fetchall()

        if not rows:
            return

        now_iso = now.isoformat()
        duration_updates: List[Tuple[int, int, int]] = []  # (guild_id, user_id, duration)
        session_updates: List[Tuple[str, int, int]] = []   # (now_iso, guild_id, user_id)

        for row in rows:
            started_at = datetime.fromisoformat(row[2])
            duration = int((now - started_at).total_seconds())
            if duration <= 0:
                continue
            duration_updates.append((row[0], row[1], duration))
            session_updates.append((now_iso, row[0], row[1]))

        if not duration_updates:
            return

        async with self.db_lock:
            for gid, uid, dur in duration_updates:
                await self._apply_duration(gid, uid, dur, now_iso)
            await self.db.executemany(
                "UPDATE active_sessions SET started_at = ? WHERE guild_id = ? AND user_id = ?",
                session_updates,
            )
            await self.db.commit()

    async def clear_guild_stats(self, guild_id: int) -> None:
        assert self.db is not None
        async with self.db_lock:
            await self.db.execute("DELETE FROM voice_time WHERE guild_id = ?", (guild_id,))
            await self.db.execute("DELETE FROM active_sessions WHERE guild_id = ?", (guild_id,))
            await self.db.commit()

    async def _start_session_locked(self, guild_id: int, user_id: int, channel_id: int) -> None:
        assert self.db is not None
        started_at = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            INSERT INTO active_sessions (guild_id, user_id, channel_id, started_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (guild_id, user_id, channel_id, started_at),
        )

    async def _finalize_session_locked(
        self,
        guild_id: int,
        user_id: int,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        assert self.db is not None
        duration = int((ended_at - started_at).total_seconds())
        if duration > 0:
            await self._apply_duration(guild_id, user_id, duration, ended_at.isoformat())
        await self.db.execute(
            "DELETE FROM active_sessions WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )

    async def _apply_duration(self, guild_id: int, user_id: int, duration: int, timestamp: str) -> None:
        assert self.db is not None
        params = [(guild_id, user_id, bucket, duration, timestamp) for bucket in BUCKETS]
        await self.db.executemany(
            """
            INSERT INTO voice_time (guild_id, user_id, bucket, seconds, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, bucket)
            DO UPDATE SET seconds = voice_time.seconds + excluded.seconds,
                          updated_at = excluded.updated_at
            """,
            params,
        )

    async def fetch_leaderboard(self, guild_id: int, bucket: str) -> List[Tuple[int, int]]:
        assert self.db is not None
        async with self.db_lock:
            cursor = await self.db.execute(
                """
                SELECT user_id, seconds FROM voice_time
                WHERE guild_id = ? AND bucket = ?
                ORDER BY seconds DESC
                LIMIT ?
                """,
                (guild_id, bucket, self.leaderboard_limit),
            )
            rows = await cursor.fetchall()
            return [(row[0], row[1]) for row in rows]

    async def fetch_user_position(self, guild_id: int, user_id: int, bucket: str) -> Optional[Tuple[int, int]]:
        assert self.db is not None
        async with self.db_lock:
            cursor = await self.db.execute(
                """
                SELECT seconds,
                       (
                           SELECT COUNT(*) + 1
                           FROM voice_time vt2
                           WHERE vt2.guild_id = voice_time.guild_id
                             AND vt2.bucket = voice_time.bucket
                             AND vt2.seconds > voice_time.seconds
                       ) AS rank
                FROM voice_time
                WHERE guild_id = ? AND user_id = ? AND bucket = ?
                """,
                (guild_id, user_id, bucket),
            )
            row = await cursor.fetchone()
            return (row[0], row[1]) if row else None

    async def get_metadata(self, key: str) -> Optional[str]:
        assert self.db is not None
        async with self.db_lock:
            cursor = await self.db.execute("SELECT value FROM metadata WHERE key = ?", (key,))
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_metadata(self, key: str, value: str) -> None:
        assert self.db is not None
        async with self.db_lock:
            await self.db.execute(
                """
                INSERT INTO metadata (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await self.db.commit()

    async def handle_periodic_resets(self) -> None:
        await self.sync_active_sessions()
        now_local = datetime.now(self.config.timezone)
        await self._handle_weekly_reset(now_local)
        await self._handle_monthly_reset(now_local)
        await self._handle_yearly_reset(now_local)

    async def _handle_weekly_reset(self, now_local: datetime) -> None:
        if now_local.weekday() != 0:
            return
        week_label = now_local.date().isoformat()
        last_reset = await self.get_metadata("weekly_reset")
        if last_reset == week_label:
            return
        async with self.db_lock:
            assert self.db is not None
            await self.db.execute("DELETE FROM voice_time WHERE bucket = 'weekly'")
            await self.db.commit()
        await self.set_metadata("weekly_reset", week_label)
        log.info("Weekly stats reset at %s", week_label)

    async def _handle_monthly_reset(self, now_local: datetime) -> None:
        if now_local.day != 1:
            return
        month_label = now_local.strftime("%Y-%m")
        last_reset = await self.get_metadata("monthly_reset")
        if last_reset == month_label:
            return
        async with self.db_lock:
            assert self.db is not None
            await self.db.execute("DELETE FROM voice_time WHERE bucket = 'monthly'")
            await self.db.commit()
        await self.set_metadata("monthly_reset", month_label)
        log.info("Monthly stats reset at %s", month_label)

    async def _handle_yearly_reset(self, now_local: datetime) -> None:
        if now_local.month != 1 or now_local.day != 1:
            return
        year_label = now_local.strftime("%Y")
        last_reset = await self.get_metadata("yearly_reset")
        if last_reset == year_label:
            return
        async with self.db_lock:
            assert self.db is not None
            await self.db.execute("DELETE FROM voice_time WHERE bucket = 'yearly'")
            await self.db.commit()
        await self.set_metadata("yearly_reset", year_label)
        log.info("Yearly stats reset at %s", year_label)


def register_application_commands(
    bot: "VoiceTimeBot", service: VoiceTrackingService, timezone_obj: timezone
) -> None:
    async def _render_leaderboard(
        interaction: discord.Interaction,
        bucket: str,
        title: str,
        period_hint: str,
        target_member: Optional[discord.Member],
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("請在伺服器頻道中使用此指令。")
            return

        viewer = interaction.user if isinstance(interaction.user, discord.Member) else None
        subject = target_member or viewer
        if subject is None:
            await interaction.response.send_message("找不到查詢成員，請重新嘗試。")
            return

        await service.sync_active_sessions(interaction.guild.id)

        top_rows = await service.fetch_leaderboard(interaction.guild.id, bucket)
        subject_stats = await service.fetch_user_position(interaction.guild.id, subject.id, bucket)

        embed = discord.Embed(title=title, description=period_hint, color=discord.Color.blurple())
        field_name = "你的目前成績" if subject == viewer else "查詢對象成績"
        display_name = discord.utils.escape_markdown(subject.display_name if subject else f"User {subject.id}")

        if subject_stats:
            seconds, rank = subject_stats
            embed.add_field(
                name=field_name,
                value=f"`#{rank}` **{display_name}** — {humanize_duration(seconds)}",
                inline=False,
            )
        else:
            embed.add_field(
                name=field_name,
                value=f"**{display_name}** 目前沒有統計資料。",
                inline=False,
            )

        if top_rows:
            lines = []
            for idx, (user_id, seconds) in enumerate(top_rows, start=1):
                member = interaction.guild.get_member(user_id)
                member_name = member.display_name if member else f"User {user_id}"
                member_name = discord.utils.escape_markdown(member_name)
                marker = "⭐" if subject and user_id == subject.id else ""
                lines.append(f"{marker}`#{idx}` **{member_name}** — {humanize_duration(seconds)}")
            embed.add_field(name="排行榜", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="排行榜", value="目前沒有統計資料。", inline=False)

        embed.set_footer(text="數據由語音監測機器人提供")
        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="time", description="查看本週語音排行榜")
    async def weekly(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        hint = "📆 統計週期：週一至今"
        await _render_leaderboard(interaction, "weekly", "本週語音排行", hint, member)

    @bot.tree.command(name="timemonth", description="查看本月語音排行榜")
    async def monthly(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        now = datetime.now(timezone_obj)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        hint = f"統計範圍：{month_start.strftime('%Y-%m-%d')} 00:00 起"
        await _render_leaderboard(interaction, "monthly", "本月語音排行", hint, member)

    @bot.tree.command(name="timeyear", description="查看本年語音排行榜")
    async def yearly(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        now = datetime.now(timezone_obj)
        year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        hint = f"統計範圍：{year_start.strftime('%Y-%m-%d')} 00:00 起"
        await _render_leaderboard(interaction, "yearly", "本年語音排行", hint, member)

    @bot.tree.command(name="timeall", description="查看累積語音排行")
    async def alltime(interaction: discord.Interaction, member: Optional[discord.Member] = None) -> None:
        hint = "統計範圍：加入伺服器至今"
        await _render_leaderboard(interaction, "alltime", "累積語音排行", hint, member)

    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    @bot.tree.command(name="timeclean", description="維護員：清除本伺服器所有語音統計")
    async def clean(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("請在伺服器中使用此指令。", ephemeral=True)
            return

        if not bot.is_maintainer(interaction.user):
            await interaction.followup.send("沒有權限使用此指令。", ephemeral=True)
            return

        await service.clear_guild_stats(guild.id)
        await service.reconcile_active_sessions(bot)
        await interaction.followup.send("已清除本伺服器所有語音統計資料。", ephemeral=True)

    @discord.app_commands.guild_only()
    @discord.app_commands.default_permissions(administrator=True)
    @bot.tree.command(name="sync", description="維護員：同步斜線指令到此伺服器")
    async def sync_commands(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("請在伺服器中使用此指令。", ephemeral=True)
            return

        if not bot.is_maintainer(interaction.user):
            await interaction.followup.send("沒有權限使用此指令。", ephemeral=True)
            return

        try:
            # 將指令同步到當前伺服器（立即生效）
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            await interaction.followup.send(
                f"✅ 已成功同步 {len(synced)} 個斜線指令到此伺服器！", ephemeral=True
            )
            log.info("Synced %d commands to guild %s", len(synced), guild.id)
        except discord.HTTPException as exc:
            await interaction.followup.send(f"❌ 同步失敗：{exc}", ephemeral=True)
            log.error("Failed to sync commands to guild %s: %s", guild.id, exc)


class VoiceTimeBot(commands.Bot):
    """Discord bot wrapper that wires the voice tracking feature into discord.py."""

    def __init__(self, config: BotConfig) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

        self.config = config
        self.service = VoiceTrackingService(config)
        self._commands_registered = False

    def is_maintainer(self, user: discord.abc.User) -> bool:
        return bool(self.config.maintainer_id and user.id == self.config.maintainer_id)

    async def setup_hook(self) -> None:
        await self.service.connect()
        if not self._commands_registered:
            register_application_commands(self, self.service, self.config.timezone)
            self._commands_registered = True
        self.rollover_loop.start()
        self.session_flush_loop.start()
        await self._sync_commands()

    async def _sync_commands(self) -> None:
        try:
            # 全域同步（可能需要最多 1 小時才能在所有伺服器生效）
            synced = await self.tree.sync()
            log.info("Global slash commands synced: %d commands", len(synced))
        except discord.HTTPException as exc:  # pragma: no cover
            log.error("Failed to sync application commands: %s", exc)

    async def sync_to_guild(self, guild: discord.Guild) -> int:
        """將指令同步到指定伺服器（立即生效）"""
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Synced %d commands to guild %s (%s)", len(synced), guild.name, guild.id)
        return len(synced)

    async def close(self) -> None:
        if self.rollover_loop.is_running():
            self.rollover_loop.cancel()
            await self.rollover_loop.wait()
        if self.session_flush_loop.is_running():
            self.session_flush_loop.cancel()
            await self.session_flush_loop.wait()
        await self.service.sync_active_sessions()
        await self.service.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, getattr(self.user, "id", "?"))
        await self.service.reconcile_active_sessions(self)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if before.channel is None and after.channel is not None:
            await self.service.start_session(member.guild.id, member.id, after.channel.id)
        elif before.channel is not None and after.channel is None:
            await self.service.end_session(member.guild.id, member.id)
        elif before.channel and after.channel and before.channel.id != after.channel.id:
            await self.service.end_session(member.guild.id, member.id)
            await self.service.start_session(member.guild.id, member.id, after.channel.id)

    @tasks.loop(minutes=1)
    async def session_flush_loop(self) -> None:
        try:
            await self.service.sync_active_sessions()
        except Exception:  # pragma: no cover - diagnostic logging only
            log.exception("session_flush_loop encountered an error")

    @session_flush_loop.before_loop
    async def before_session_flush_loop(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def rollover_loop(self) -> None:
        try:
            await self.service.handle_periodic_resets()
        except Exception:  # pragma: no cover - diagnostic logging only
            log.exception("rollover_loop encountered an error")

    @rollover_loop.before_loop
    async def before_rollover_loop(self) -> None:
        await self.wait_until_ready()


def create_bot(config: BotConfig) -> VoiceTimeBot:
    return VoiceTimeBot(config)
