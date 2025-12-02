from __future__ import annotations

from typing import Optional

import discord

from currency_feature.currency import CurrencyMenuView, CurrencySelect, CURRENCY_GROUPS
from dinner_feature.dinner import DinnerLotteryView
from voice_tracker.voice_tracking import VoiceTrackingService, humanize_duration
from weather_feature.weather import get_weather_service, _build_weather_embed, TAIWAN_CITIES, WeatherError

MENTION_TARGET_ID = 1375818369344864317

# 獎牌 emoji
MEDAL_EMOJIS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

VOICE_BUCKET_META = {
    "weekly": ("📆 本週語音排行", "統計週期：週一 00:00 至今", "🗓️"),
    "monthly": ("📅 本月語音排行", "統計週期：本月 1 日 00:00 至今", "📆"),
    "yearly": ("📊 本年語音排行", "統計週期：今年 1/1 00:00 至今", "🗓️"),
    "alltime": ("🏆 累積語音排行", "自機器人啟用以來的總計", "👑"),
}

def generate_progress_bar(value: int, max_value: int, length: int = 10) -> str:
    """生成進度條"""
    if max_value <= 0:
        return "░" * length
    ratio = min(value / max_value, 1.0)
    filled = int(ratio * length)
    return "█" * filled + "░" * (length - filled)

def setup_menu_feature(bot: discord.Client) -> None:
    FeatureMenuController(bot)


class FeatureMenuController:
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        bot.add_listener(self._on_message, name="on_message")

    async def _on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if not self._should_trigger(message):
            return

        owner_id = message.author.id
        embed = self._build_function_menu_embed(owner_id)
        view = FunctionMenuView(self, owner_id=owner_id)
        sent = await message.channel.send(embed=embed, view=view)
        view.message = sent

    def _should_trigger(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        if self.bot.user and self.bot.user.id in {m.id for m in message.mentions}:
            return True
        return any(m.id == MENTION_TARGET_ID for m in message.mentions)

    def _build_function_menu_embed(self, requester_id: int) -> discord.Embed:
        embed = discord.Embed(
            title="🎛️ 和風牌監視器 – 功能中心",
            description=(
                f"歡迎 <@{requester_id}>！請選擇想使用的功能 👇\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.from_rgb(88, 101, 242),
        )
        embed.add_field(
            name="🎙️ 語音時數",
            value="查看伺服器成員語音活躍排行榜",
            inline=True,
        )
        embed.add_field(
            name="💱 匯率看板",
            value="查詢即時匯率與 90 天走勢圖",
            inline=True,
        )
        embed.add_field(
            name="🍽️ 晚餐抽獎",
            value="讓命運決定今晚吃什麼",
            inline=True,
        )
        embed.add_field(
            name="🌤️ 天氣預報",
            value="查詢臺灣各縣市即時天氣",
            inline=True,
        )
        embed.set_footer(text="⏰ 選單 2 分鐘後自動失效")
        return embed

    def _build_voice_menu_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎙️ 語音時數排行",
            description=(
                "選擇想查看的排行榜類別\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.from_rgb(59, 165, 93),
        )
        embed.add_field(
            name="📆 周榜",
            value="本週一開始至今",
            inline=True,
        )
        embed.add_field(
            name="📅 月榜",
            value="本月 1 日開始至今",
            inline=True,
        )
        embed.add_field(
            name="📊 年榜",
            value="今年 1/1 開始至今",
            inline=True,
        )
        embed.add_field(
            name="🏆 總排行",
            value="累積所有時數",
            inline=True,
        )
        embed.set_footer(text="點擊按鈕查看對應排行榜")
        return embed

    def _build_weather_menu_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🌤️ 天氣預報選單",
            description="選擇想查詢的縣市，即可查看即時天氣與未來 24 小時預報。",
            color=discord.Color.blue(),
        )
        # 分類顯示縣市
        north = "臺北市、新北市、基隆市、桃園市、新竹市、新竹縣、宜蘭縣"
        central = "臺中市、苗栗縣、彰化縣、南投縣、雲林縣"
        south = "臺南市、高雄市、嘉義市、嘉義縣、屏東縣"
        east_islands = "花蓮縣、臺東縣、澎湖縣、金門縣、連江縣"
        
        embed.add_field(name="🏙️ 北部", value=north, inline=False)
        embed.add_field(name="🏞️ 中部", value=central, inline=False)
        embed.add_field(name="🌴 南部", value=south, inline=False)
        embed.add_field(name="🏝️ 東部及離島", value=east_islands, inline=False)
        embed.set_footer(text="使用下拉選單選擇縣市，或返回功能清單")
        return embed

    async def build_voice_leaderboard_embed(
        self,
        guild: Optional[discord.Guild],
        bucket: str,
    ) -> discord.Embed:
        if guild is None:
            return self._build_error_embed("請在伺服器頻道中使用此功能。")
        service: VoiceTrackingService = getattr(self.bot, "service", None)
        if service is None:
            return self._build_error_embed("語音統計服務尚未就緒。")

        await service.sync_active_sessions(guild.id)
        rows = await service.fetch_leaderboard(guild.id, bucket)
        title, hint, icon = VOICE_BUCKET_META.get(bucket, ("語音排行榜", "", "📊"))
        
        embed = discord.Embed(
            title=title,
            description=f"{hint}\n━━━━━━━━━━━━━━━━━━━━━━",
            color=discord.Color.from_rgb(59, 165, 93),
        )
        
        if rows:
            # 計算總時數
            total_seconds = sum(seconds for _, seconds in rows)
            
            lines = []
            for idx, (user_id, seconds) in enumerate(rows, start=0):
                member = guild.get_member(user_id)
                display = member.display_name if member else f"User {user_id}"
                display = discord.utils.escape_markdown(display)
                
                # 獎牌 emoji
                medal = MEDAL_EMOJIS[idx] if idx < len(MEDAL_EMOJIS) else f"`{idx+1}.`"
                
                # 格式化時間
                time_str = humanize_duration(seconds)
                
                lines.append(f"{medal} **{display}**\n　　{time_str}")
            
            embed.add_field(
                name="🏅 排行榜",
                value="\n".join(lines),
                inline=False,
            )
            
            # 統計摘要
            embed.add_field(
                name="📊 統計摘要",
                value=(
                    f"👥 上榜人數：**{len(rows)}** 人\n"
                    f"⏱️ 總計時數：**{humanize_duration(total_seconds)}**\n"
                    f"📈 平均時數：**{humanize_duration(total_seconds // len(rows) if rows else 0)}**"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name="🏅 排行榜",
                value="📭 目前沒有任何資料\n快來語音頻道聊天吧！",
                inline=False,
            )
        
        embed.set_footer(text="🔄 點擊其他按鈕切換排行榜類型")
        return embed

    def _build_error_embed(self, message: str) -> discord.Embed:
        return discord.Embed(
            title="⚠️ 無法完成操作",
            description=f"```\n{message}\n```\n\n💡 如持續發生問題，請聯繫管理員",
            color=discord.Color.red(),
        )


class FunctionMenuView(discord.ui.View):
    def __init__(self, controller: FeatureMenuController, owner_id: int, message: Optional[discord.Message] = None) -> None:
        super().__init__(timeout=120)
        self.controller = controller
        self.owner_id = owner_id
        self.message = message

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 只有清單請求人可以操作這個選單，請自行 tag 機器人開啟新選單。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🎙️ 語音時數", style=discord.ButtonStyle.primary, row=0)
    async def voice_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self.controller._build_voice_menu_embed()
        new_view = VoiceMenuView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, view=new_view)

    @discord.ui.button(label="💱 匯率看板", style=discord.ButtonStyle.primary, row=0)
    async def currency_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = CurrencyMenuWrapper(self.controller, owner_id=self.owner_id, message=interaction.message)
        embed = view._build_menu_embed()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🍽️ 晚餐抽獎", style=discord.ButtonStyle.success, row=0)
    async def dinner_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = DinnerMenuWrapper(self.controller, owner_id=self.owner_id, message=interaction.message)
        embed = view._build_menu_embed()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🌤️ 天氣預報", style=discord.ButtonStyle.success, row=0)
    async def weather_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self.controller._build_weather_menu_embed()
        view = WeatherRegionView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, view=view)


class WeatherRegionView(discord.ui.View):
    """天氣選單 - 選擇地區"""
    def __init__(self, controller: FeatureMenuController, owner_id: int, message: Optional[discord.Message] = None) -> None:
        super().__init__(timeout=120)
        self.controller = controller
        self.owner_id = owner_id
        self.message = message

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 只有清單請求人可以操作這個選單，請自行 tag 機器人開啟新選單。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🏙️ 北部", style=discord.ButtonStyle.primary, row=0)
    async def north(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cities = ["臺北市", "新北市", "基隆市", "桃園市", "新竹市", "新竹縣", "宜蘭縣"]
        embed = self._build_city_embed("北部", cities)
        view = WeatherCityView(self.controller, self.owner_id, cities, self.message)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🏞️ 中部", style=discord.ButtonStyle.primary, row=0)
    async def central(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cities = ["臺中市", "苗栗縣", "彰化縣", "南投縣", "雲林縣"]
        embed = self._build_city_embed("中部", cities)
        view = WeatherCityView(self.controller, self.owner_id, cities, self.message)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🌴 南部", style=discord.ButtonStyle.primary, row=0)
    async def south(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cities = ["臺南市", "高雄市", "嘉義市", "嘉義縣", "屏東縣"]
        embed = self._build_city_embed("南部", cities)
        view = WeatherCityView(self.controller, self.owner_id, cities, self.message)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="🏝️ 東部離島", style=discord.ButtonStyle.primary, row=0)
    async def east(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cities = ["花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣"]
        embed = self._build_city_embed("東部及離島", cities)
        view = WeatherCityView(self.controller, self.owner_id, cities, self.message)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="⬅️ 返回", style=discord.ButtonStyle.danger, row=1)
    async def go_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self.controller._build_function_menu_embed(self.owner_id)
        new_view = FunctionMenuView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, view=new_view)

    def _build_city_embed(self, region: str, cities: list[str]) -> discord.Embed:
        embed = discord.Embed(
            title=f"🌤️ 天氣預報 - {region}",
            description=(
                "選擇要查詢的縣市\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="📍 可選縣市",
            value=" · ".join(cities),
            inline=False,
        )
        embed.set_footer(text="點擊縣市按鈕查看天氣預報")
        return embed


class WeatherCityView(discord.ui.View):
    """天氣選單 - 選擇縣市"""
    def __init__(self, controller: FeatureMenuController, owner_id: int, cities: list[str], message: Optional[discord.Message] = None) -> None:
        super().__init__(timeout=120)
        self.controller = controller
        self.owner_id = owner_id
        self.message = message
        
        # 動態新增縣市按鈕
        for idx, city in enumerate(cities):
            btn = discord.ui.Button(label=city, style=discord.ButtonStyle.success, row=idx // 4)
            btn.callback = self._make_callback(city)
            self.add_item(btn)
        
        # 返回按鈕（放在最後一排）
        back_row = (len(cities) - 1) // 4 + 1
        back_btn = discord.ui.Button(label="⬅️ 返回", style=discord.ButtonStyle.danger, row=min(back_row, 4))
        back_btn.callback = self._go_back
        self.add_item(back_btn)

    def _make_callback(self, city: str):
        async def callback(interaction: discord.Interaction):
            await self.show_weather(interaction, city)
        return callback

    async def _go_back(self, interaction: discord.Interaction) -> None:
        embed = self.controller._build_weather_menu_embed()
        new_view = WeatherRegionView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, view=new_view)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 只有清單請求人可以操作這個選單，請自行 tag 機器人開啟新選單。", ephemeral=True)
            return False
        return True

    async def show_weather(self, interaction: discord.Interaction, city: str) -> None:
        """查詢並顯示天氣"""
        await interaction.response.defer()
        
        try:
            service = get_weather_service()
            report = await service.fetch_weather(city)
            embed = _build_weather_embed(report)
        except WeatherError as e:
            embed = self.controller._build_error_embed(str(e))
        except Exception as e:
            embed = self.controller._build_error_embed(f"查詢失敗：{e}")
        
        new_view = WeatherResultView(self.controller, owner_id=self.owner_id, message=self.message)
        await interaction.followup.edit_message(interaction.message.id, embed=embed, view=new_view)


class WeatherResultView(discord.ui.View):
    """天氣結果 View，只有返回按鈕"""
    def __init__(self, controller: FeatureMenuController, owner_id: int, message: Optional[discord.Message] = None) -> None:
        super().__init__(timeout=120)
        self.controller = controller
        self.owner_id = owner_id
        self.message = message

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 只有清單請求人可以操作這個選單，請自行 tag 機器人開啟新選單。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔄 重新選擇", style=discord.ButtonStyle.primary, row=0)
    async def select_again(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self.controller._build_weather_menu_embed()
        new_view = WeatherRegionView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, view=new_view)

    @discord.ui.button(label="⬅️ 返回主選單", style=discord.ButtonStyle.danger, row=0)
    async def go_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self.controller._build_function_menu_embed(self.owner_id)
        new_view = FunctionMenuView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, view=new_view)


class VoiceMenuView(discord.ui.View):
    def __init__(self, controller: FeatureMenuController, owner_id: int, message: Optional[discord.Message] = None) -> None:
        super().__init__(timeout=120)
        self.controller = controller
        self.owner_id = owner_id
        self.message = message

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ 只有清單請求人可以操作這個選單，請自行 tag 機器人開啟新選單。", ephemeral=True)
            return False
        return True

    async def _show_bucket(self, interaction: discord.Interaction, bucket: str) -> None:
        try:
            embed = await self.controller.build_voice_leaderboard_embed(interaction.guild, bucket)
        except Exception as e:
            embed = self.controller._build_error_embed(f"發生錯誤：{e}")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="📆 周榜", style=discord.ButtonStyle.primary, row=0)
    async def weekly(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_bucket(interaction, "weekly")

    @discord.ui.button(label="📅 月榜", style=discord.ButtonStyle.primary, row=0)
    async def monthly(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_bucket(interaction, "monthly")

    @discord.ui.button(label="📊 年榜", style=discord.ButtonStyle.primary, row=0)
    async def yearly(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_bucket(interaction, "yearly")

    @discord.ui.button(label="🏆 總排行", style=discord.ButtonStyle.success, row=0)
    async def alltime(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._show_bucket(interaction, "alltime")

    @discord.ui.button(label="⬅️ 返回", style=discord.ButtonStyle.danger, row=1)
    async def go_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self.controller._build_function_menu_embed(self.owner_id)
        new_view = FunctionMenuView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, view=new_view)



class CurrencyMenuWrapper(CurrencyMenuView):
    def __init__(self, controller: FeatureMenuController, owner_id: int, message: Optional[discord.Message] = None) -> None:
        super().__init__(owner_id=owner_id, message=message)
        self.controller = controller

    @discord.ui.button(label="⬅️ 返回主選單", style=discord.ButtonStyle.danger, row=2)
    async def go_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self.controller._build_function_menu_embed(self.owner_id)
        new_view = FunctionMenuView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, attachments=[], view=new_view)


class DinnerMenuWrapper(DinnerLotteryView):
    def __init__(self, controller: FeatureMenuController, owner_id: int, message: Optional[discord.Message] = None) -> None:
        super().__init__(owner_id=owner_id, message=message)
        self.controller = controller

    @discord.ui.button(label="⬅️ 返回主選單", style=discord.ButtonStyle.danger, row=3)
    async def go_back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self.controller._build_function_menu_embed(self.owner_id)
        new_view = FunctionMenuView(self.controller, owner_id=self.owner_id, message=interaction.message)
        await interaction.response.edit_message(embed=embed, view=new_view)
