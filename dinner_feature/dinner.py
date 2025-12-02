"""晚餐抽獎系統：分類按鈕 + 隨機菜單"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

import discord

from .food_data import (
    ALL_CATEGORY_KEYS,
    DINNER_CATEGORIES,
    DINNER_TIPS,
    SIDE_OPTIONS,
)

# 抽獎動畫 emoji
LOTTERY_EMOJIS = ["🎰", "🎲", "🎯", "🎪", "✨", "🌟", "💫", "🔮"]

# 食物相關 emoji
FOOD_EMOJIS = {
    "rice": ["🍚", "🍛", "🍱", "🥢"],
    "noodle": ["🍜", "🍝", "🥡", "🥢"],
    "snack": ["🍢", "🍡", "🥟", "🧆"],
    "hotpot": ["🍲", "🫕", "🥘", "♨️"],
    "korean": ["🇰🇷", "🥬", "🌶️", "🥢"],
    "japanese": ["🇯🇵", "🍣", "🍙", "🥢"],
    "hongkong": ["🇭🇰", "🥡", "🫖", "🥢"],
}

# 飲料推薦
DRINK_OPTIONS = {
    "rice": ["🧋 珍珠奶茶", "🍵 無糖綠茶", "🥤 冬瓜茶", "🧃 檸檬紅茶"],
    "noodle": ["🍵 烏龍茶", "🥤 酸梅湯", "🧋 多多綠茶", "🍺 啤酒"],
    "snack": ["🧋 珍珠鮮奶", "🥤 可樂", "🍺 台啤", "🧃 蘋果汁"],
    "hotpot": ["🥤 可樂", "🍺 啤酒", "🧃 王老吉", "🍵 烏龍茶"],
    "korean": ["🍺 韓國燒酒", "🥤 可樂", "🧃 水蜜桃汁", "🍵 玄米茶"],
    "japanese": ["🍺 日本啤酒", "🍵 抹茶", "🧃 可爾必思", "🍶 清酒"],
    "hongkong": ["🧋 港式奶茶", "☕ 鴛鴦", "🍋 凍檸茶", "🥤 楊枝甘露"],
}


def draw_food(category_key: Optional[str] = None) -> tuple[str, str]:
    """根據指定類別（或隨機類別）抽一項食物"""
    key = category_key or random.choice(ALL_CATEGORY_KEYS)
    data = DINNER_CATEGORIES[key]
    food = random.choice(data["foods"])  # type: ignore[index]
    return key, food


class DinnerLotteryView(discord.ui.View):
    """互動式按鈕選單"""

    def __init__(self, owner_id: int, message: Optional[discord.Message] = None) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.message = message

    async def on_timeout(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "🍽️ 只有發起抽獎的人能操作這組按鈕，請自行輸入 `/dinner` 開始你的晚餐抽獎！",
                ephemeral=True,
            )
            return False
        return True

    def _build_menu_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🍽️ 今晚吃什麼？",
            description=(
                "選擇一個料理類型，讓命運決定今晚的晚餐！\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🎰 **抽獎規則**\n"
                "• 選擇喜歡的料理類型\n"
                "• 系統隨機抽出一道美食\n"
                "• 可重複抽獎直到滿意\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.from_rgb(251, 146, 60),
        )
        
        # 分組顯示類別
        category_info = []
        for key in ALL_CATEGORY_KEYS:
            info = DINNER_CATEGORIES[key]
            category_info.append(f"{info['emoji']} **{info['name']}** ({len(info['foods'])}道)")
        
        embed.add_field(
            name="📋 可選類型",
            value="\n".join(category_info[:4]),
            inline=True,
        )
        embed.add_field(
            name="​",
            value="\n".join(category_info[4:]),
            inline=True,
        )
        
        embed.add_field(
            name="🎲 隨便來",
            value="不知道吃什麼？讓命運來決定！",
            inline=False,
        )
        
        embed.set_footer(text="⏰ 選單 3 分鐘後失效 · 祝你用餐愉快！")
        return embed

    def _build_result_embed(self, category_key: str, food: str) -> discord.Embed:
        info = DINNER_CATEGORIES[category_key]
        tip = random.choice(DINNER_TIPS)
        side = random.choice(SIDE_OPTIONS)
        drink = random.choice(DRINK_OPTIONS.get(category_key, ["🧋 珍珠奶茶"]))
        food_emoji = random.choice(FOOD_EMOJIS.get(category_key, ["🍽️"]))
        
        embed = discord.Embed(
            title=f"🎉 晚餐抽獎結果",
            description=(
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"　　　　{food_emoji} **{food}** {food_emoji}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=info['color'],
        )
        
        # 類型標籤
        embed.add_field(
            name="📌 料理類型",
            value=f"{info['emoji']} {info['name']}",
            inline=True,
        )
        
        # 推薦飲料
        embed.add_field(
            name="🥤 推薦飲料",
            value=drink,
            inline=True,
        )
        
        # 搭配推薦
        embed.add_field(
            name="🍴 加點推薦",
            value=side,
            inline=True,
        )
        
        # 用餐小提示
        embed.add_field(
            name="💡 用餐小提示",
            value=f"```{tip}```",
            inline=False,
        )
        
        # 評分區（純裝飾）
        stars = "⭐" * random.randint(4, 5)
        embed.add_field(
            name="✨ 今日運勢",
            value=f"{stars} 這是個好選擇！",
            inline=False,
        )
        
        embed.set_footer(text="🔄 不滿意？再按一次按鈕重新抽獎！")
        return embed

    def _build_loading_embed(self) -> discord.Embed:
        """建立抽獎中的過渡 Embed"""
        emoji = random.choice(LOTTERY_EMOJIS)
        embed = discord.Embed(
            title=f"{emoji} 抽獎中...",
            description="🎰 命運的齒輪正在轉動...\n\n`[████████░░░░░░░░░░░░]` 40%",
            color=discord.Color.from_rgb(251, 191, 36),
        )
        return embed

    async def _handle_draw(self, interaction: discord.Interaction, category_key: Optional[str]) -> None:
        # 先顯示抽獎動畫
        loading_embed = self._build_loading_embed()
        await interaction.response.edit_message(embed=loading_embed, view=self)
        
        # 短暫延遲增加期待感
        await asyncio.sleep(0.8)
        
        # 更新進度
        loading_embed.description = "🎰 命運的齒輪正在轉動...\n\n`[████████████████░░░░]` 80%"
        await interaction.edit_original_response(embed=loading_embed)
        
        await asyncio.sleep(0.5)
        
        # 顯示結果
        key, food = draw_food(category_key)
        embed = self._build_result_embed(key, food)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="🍚 飯類", style=discord.ButtonStyle.primary, row=0)
    async def rice_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_draw(interaction, "rice")

    @discord.ui.button(label="🍜 麵類", style=discord.ButtonStyle.primary, row=0)
    async def noodle_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_draw(interaction, "noodle")

    @discord.ui.button(label="🍢 小吃", style=discord.ButtonStyle.primary, row=0)
    async def snack_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_draw(interaction, "snack")

    @discord.ui.button(label="🍲 鍋物", style=discord.ButtonStyle.primary, row=0)
    async def hotpot_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_draw(interaction, "hotpot")

    @discord.ui.button(label="🇰🇷 韓式", style=discord.ButtonStyle.secondary, row=1)
    async def korean_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_draw(interaction, "korean")

    @discord.ui.button(label="🇯🇵 日式", style=discord.ButtonStyle.secondary, row=1)
    async def japanese_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_draw(interaction, "japanese")

    @discord.ui.button(label="🇭🇰 港式", style=discord.ButtonStyle.secondary, row=1)
    async def hongkong_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_draw(interaction, "hongkong")

    @discord.ui.button(label="🎲 隨便來", style=discord.ButtonStyle.success, row=2)
    async def random_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._handle_draw(interaction, None)
    
    @discord.ui.button(label="📋 重新選擇", style=discord.ButtonStyle.secondary, row=2)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = self._build_menu_embed()
        await interaction.response.edit_message(embed=embed, view=self)


# --- 對外註冊 ---

def setup_dinner_feature(bot: discord.Client) -> None:
    @bot.tree.command(name="dinner", description="抽一份今晚要吃什麼")
    async def dinner_command(interaction: discord.Interaction) -> None:
        view = DinnerLotteryView(owner_id=interaction.user.id)
        embed = view._build_menu_embed()
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        view.message = message
