"""
匯率查詢功能模組
使用 exchangerate.host API 取得即時匯率與歷史數據
"""
from __future__ import annotations

import asyncio
import io
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

import discord
from discord import app_commands
import httpx
import certifi

# ============ 設定 ============

# 台灣常用的貨幣清單（分組）
CURRENCY_GROUPS = {
    "常用貨幣": {
        "USD": {"name": "美元", "emoji": "🇺🇸", "full_name": "美國美元"},
        "JPY": {"name": "日圓", "emoji": "🇯🇵", "full_name": "日本日圓"},
        "EUR": {"name": "歐元", "emoji": "🇪🇺", "full_name": "歐盟歐元"},
        "CNY": {"name": "人民幣", "emoji": "🇨🇳", "full_name": "中國人民幣"},
    },
    "亞洲貨幣": {
        "HKD": {"name": "港幣", "emoji": "🇭🇰", "full_name": "香港港幣"},
        "KRW": {"name": "韓元", "emoji": "🇰🇷", "full_name": "韓國韓元"},
        "SGD": {"name": "新加坡幣", "emoji": "🇸🇬", "full_name": "新加坡幣"},
        "THB": {"name": "泰銖", "emoji": "🇹🇭", "full_name": "泰國泰銖"},
        "VND": {"name": "越南盾", "emoji": "🇻🇳", "full_name": "越南越南盾"},
        "MYR": {"name": "馬來幣", "emoji": "🇲🇾", "full_name": "馬來西亞令吉"},
        "PHP": {"name": "披索", "emoji": "🇵🇭", "full_name": "菲律賓披索"},
        "IDR": {"name": "印尼盾", "emoji": "🇮🇩", "full_name": "印尼盾"},
    },
    "歐美貨幣": {
        "GBP": {"name": "英鎊", "emoji": "🇬🇧", "full_name": "英國英鎊"},
        "AUD": {"name": "澳幣", "emoji": "🇦🇺", "full_name": "澳洲澳幣"},
        "CAD": {"name": "加幣", "emoji": "🇨🇦", "full_name": "加拿大加幣"},
        "CHF": {"name": "法郎", "emoji": "🇨🇭", "full_name": "瑞士法郎"},
    },
}

# 扁平化貨幣字典（向後相容）
CURRENCIES = {}
for group in CURRENCY_GROUPS.values():
    CURRENCIES.update(group)

BASE_CURRENCY = "TWD"  # 基準貨幣：新台幣
CACHE_TTL = 300  # 快取 5 分鐘
HISTORY_DAYS = 90  # 歷史數據天數
HISTORY_CACHE_VERSION = "v2"

# ============ 服務類別 ============

class CurrencyError(Exception):
    """匯率查詢錯誤"""
    pass


class CurrencyService:
    """匯率查詢服務（單例模式）"""
    
    _instance: Optional["CurrencyService"] = None
    
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._cache: Dict[str, Tuple[float, dict]] = {}  # {key: (timestamp, data)}
        self._lock = asyncio.Lock()
    
    @classmethod
    def get_instance(cls) -> "CurrencyService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15.0, verify=certifi.where())
        return self._client
    
    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    def _is_cache_valid(self, key: str) -> bool:
        if key not in self._cache:
            return False
        timestamp, _ = self._cache[key]
        return (datetime.now().timestamp() - timestamp) < CACHE_TTL
    
    def clear_rate_cache(self, currency: str) -> None:
        """清除指定貨幣的匯率快取"""
        cache_key = f"rate_{currency}"
        if cache_key in self._cache:
            del self._cache[cache_key]
    
    async def get_current_rate(self, currency: str, force_refresh: bool = False) -> dict:
        """取得目前匯率（1 外幣 = ? 台幣）"""
        cache_key = f"rate_{currency}"
        
        # 強制刷新時清除快取
        if force_refresh:
            self.clear_rate_cache(currency)
        
        async with self._lock:
            if self._is_cache_valid(cache_key):
                return self._cache[cache_key][1]
        
        client = await self._get_client()
        
        # 使用免費的 exchangerate-api.com
        url = f"https://api.exchangerate-api.com/v4/latest/{currency}"
        
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            
            twd_rate = data["rates"].get("TWD")
            if twd_rate is None:
                raise CurrencyError(f"找不到 {currency} 對 TWD 的匯率")
            
            result = {
                "currency": currency,
                "rate": twd_rate,  # 1 外幣 = ? TWD
                "inverse_rate": 1 / twd_rate if twd_rate else 0,  # 1 TWD = ? 外幣
                "updated_at": datetime.now(),
            }
            
            async with self._lock:
                self._cache[cache_key] = (datetime.now().timestamp(), result)
            
            return result
            
        except httpx.HTTPStatusError as e:
            raise CurrencyError(f"API 請求失敗：{e.response.status_code}")
        except Exception as e:
            raise CurrencyError(f"查詢失敗：{str(e)}")
    
    async def get_history_rates(self, currency: str, days: int = HISTORY_DAYS) -> List[Tuple[str, float]]:
        """取得歷史匯率數據"""
        cache_key = f"history_{HISTORY_CACHE_VERSION}_{currency}_{days}"
        
        async with self._lock:
            if self._is_cache_valid(cache_key):
                cached = self._cache[cache_key][1]
                if cached and len(cached) >= 2:
                    return cached
        
        client = await self._get_client()
        end_date = datetime.utcnow().date()
        start_date = end_date - timedelta(days=days)
        history: List[Tuple[str, float]] = []
        
        # 先嘗試直接查詢 1 {currency} = ? TWD，失敗再反向查詢
        url_candidates = [
            (
                f"https://api.exchangerate.host/timeseries?base={currency}&symbols=TWD"
                f"&start_date={start_date.isoformat()}&end_date={end_date.isoformat()}",
                False,
            ),
            (
                f"https://api.exchangerate.host/timeseries?base=TWD&symbols={currency}"
                f"&start_date={start_date.isoformat()}&end_date={end_date.isoformat()}",
                True,
            ),
        ]
        
        for url, need_inverse in url_candidates:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                if not data.get("success", False):
                    continue
                rates = data.get("rates", {})
                parsed: List[Tuple[str, float]] = []
                for date, values in sorted(rates.items()):
                    val = values.get("TWD" if not need_inverse else currency)
                    if not val:
                        continue
                    parsed.append((date, (1 / val) if need_inverse else val))
                if parsed:
                    history = parsed
                    break
            except Exception:
                continue
        
        if history and len(history) >= 2:
            async with self._lock:
                self._cache[cache_key] = (datetime.now().timestamp(), history)
            return history
        return []
    
        def build_monthly_history(self, history: List[Tuple[str, float]], months: int = 6) -> List[Tuple[str, float]]:
                """將每日歷史數據轉為月平均"""
                if not history:
                        return []
                monthly: OrderedDict[str, List[float]] = OrderedDict()
                for date_str, rate in history:
                        month = date_str[:7]
                        monthly.setdefault(month, []).append(rate)
                averaged = [
                        (month, sum(values) / len(values))
                        for month, values in monthly.items()
                ]
                return averaged[-months:]

        def generate_line_chart(
                self,
                history: List[Tuple[str, float]],
                title: str,
                width: int = 500,
                height: int = 220,
        ) -> Optional[bytes]:
                if not history or len(history) < 2:
                        return None

                labels = [label for label, _ in history]
                rates = [rate for _, rate in history]
                min_rate = min(rates)
                max_rate = max(rates)
                rate_range = max_rate - min_rate or 1e-9

                svg_width = width
                svg_height = height
                padding = 45
                chart_width = svg_width - padding * 2
                chart_height = svg_height - padding * 2

                points = []
                for idx, rate in enumerate(rates):
                        x = padding + (idx / (len(rates) - 1)) * chart_width
                        y = padding + chart_height - ((rate - min_rate) / rate_range) * chart_height
                        points.append(f"{x:.1f},{y:.1f}")

                first_rate = rates[0]
                last_rate = rates[-1]
                change_pct = ((last_rate - first_rate) / first_rate) * 100 if first_rate else 0
                trend_color = "#22c55e" if change_pct >= 0 else "#ef4444"

                # X 軸標籤（首、中、尾）
                label_positions = []
                if labels:
                        label_positions.append((padding, labels[0]))
                        mid_idx = len(labels) // 2
                        label_positions.append((padding + (mid_idx / (len(labels) - 1)) * chart_width, labels[mid_idx]))
                        label_positions.append((padding + chart_width, labels[-1]))

                svg = f'''<?xml version="1.0" encoding="UTF-8"?>
<svg width="{svg_width}" height="{svg_height}" xmlns="http://www.w3.org/2000/svg">
    <defs>
        <linearGradient id="lineGradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" style="stop-color:{trend_color};stop-opacity:0.6"/>
            <stop offset="100%" style="stop-color:{trend_color};stop-opacity:1"/>
        </linearGradient>
        <linearGradient id="areaGradient" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" style="stop-color:{trend_color};stop-opacity:0.25"/>
            <stop offset="100%" style="stop-color:{trend_color};stop-opacity:0.05"/>
        </linearGradient>
    </defs>
    <rect width="{svg_width}" height="{svg_height}" fill="#2b2d31" rx="10"/>
    <text x="{svg_width/2}" y="25" fill="#f8fafc" font-size="14" font-family="Arial" text-anchor="middle">{title}</text>
    <g stroke="#3f4147" stroke-width="1" stroke-dasharray="4,4">
        <line x1="{padding}" y1="{padding}" x2="{svg_width - padding}" y2="{padding}"/>
        <line x1="{padding}" y1="{padding + chart_height/2}" x2="{svg_width - padding}" y2="{padding + chart_height/2}"/>
        <line x1="{padding}" y1="{svg_height - padding}" x2="{svg_width - padding}" y2="{svg_height - padding}"/>
    </g>
    <path d="M {points[0]} L {' L '.join(points[1:])}" fill="none" stroke="url(#lineGradient)" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M {points[0]} L {' L '.join(points[1:])} L {svg_width - padding},{svg_height - padding} L {padding},{svg_height - padding} Z" fill="url(#areaGradient)" opacity="0.7"/>
    <circle cx="{points[-1].split(',')[0]}" cy="{points[-1].split(',')[1]}" r="4" fill="{trend_color}"/>
    <text x="{padding}" y="{padding - 10}" fill="#94a3b8" font-size="11" font-family="Arial">最高 {max_rate:.4f}</text>
    <text x="{padding}" y="{svg_height - padding + 20}" fill="#94a3b8" font-size="11" font-family="Arial">最低 {min_rate:.4f}</text>
    <text x="{svg_width - padding}" y="{padding - 10}" fill="{trend_color}" font-size="12" font-family="Arial" text-anchor="end">{'+' if change_pct >= 0 else ''}{change_pct:.2f}%</text>
    {''.join(f'<text x="{pos:.1f}" y="{svg_height - 10}" fill="#cbd5f5" font-size="11" font-family="Arial" text-anchor="middle">{label}</text>' for pos, label in label_positions)}
</svg>'''

                return svg.encode('utf-8')


def get_currency_service() -> CurrencyService:
    return CurrencyService.get_instance()


# ============ Discord UI ============

class CurrencySelect(discord.ui.Select):
    """貨幣下拉選單"""
    
    def __init__(self, parent_view: "CurrencyMenuView") -> None:
        self.parent_view = parent_view
        
        options = []
        for group_name, currencies in CURRENCY_GROUPS.items():
            for code, info in currencies.items():
                options.append(discord.SelectOption(
                    label=f"{info['name']} ({code})",
                    value=code,
                    emoji=info['emoji'],
                    description=f"查詢 {info['full_name']} 匯率",
                ))
        
        super().__init__(
            placeholder="🔍 選擇要查詢的貨幣...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
    
    async def callback(self, interaction: discord.Interaction) -> None:
        currency = self.values[0]
        await self.parent_view._show_currency(interaction, currency)


class CurrencyMenuView(discord.ui.View):
    """匯率選單主視圖"""
    
    def __init__(self, owner_id: int, message: Optional[discord.Message] = None) -> None:
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.message = message
        self.service = get_currency_service()
        self.current_currency: Optional[str] = None
        
        # 加入下拉選單
        self.add_item(CurrencySelect(self))
    
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
            await interaction.response.send_message(
                "❌ 只有指令使用者可以操作此選單，請自行使用 `/money` 指令。",
                ephemeral=True
            )
            return False
        return True
    
    def _build_menu_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="💱 匯率查詢中心",
            description=(
                "從下方選單選擇想查詢的貨幣，即可查看即時匯率與近期走勢！\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔄 **資料更新** 每 5 分鐘自動刷新\n"
                "📊 **走勢圖表** 顯示近 90 天變化趨勢\n"
                "💡 **快速換算** 提供常見金額換算參考\n"
                "━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=discord.Color.gold(),
        )
        
        # 分組顯示貨幣
        for group_name, currencies in CURRENCY_GROUPS.items():
            currencies_list = [f"{info['emoji']} {info['name']}" for code, info in currencies.items()]
            embed.add_field(
                name=f"📋 {group_name}",
                value=" · ".join(currencies_list),
                inline=False,
            )
        
        embed.set_footer(text="💡 使用下拉選單選擇貨幣 · 以新台幣 (TWD) 為基準")
        return embed
    
    @discord.ui.button(label="🔄 刷新匯率", style=discord.ButtonStyle.primary, row=1, disabled=True)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.current_currency:
            await self._show_currency(interaction, self.current_currency, force_refresh=True)
        else:
            await interaction.response.send_message("❌ 請先選擇一個貨幣", ephemeral=True)
    
    @discord.ui.button(label="📋 返回清單", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.current_currency = None
        self.refresh_button.disabled = True
        embed = self._build_menu_embed()
        await interaction.response.edit_message(embed=embed, attachments=[], view=self)
    
    async def _show_currency(self, interaction: discord.Interaction, currency: str, force_refresh: bool = False) -> None:
        """顯示指定貨幣的匯率資訊"""
        await interaction.response.defer()
        
        self.current_currency = currency
        self.refresh_button.disabled = False
        
        info = CURRENCIES.get(currency, {"name": currency, "emoji": "💰", "full_name": currency})
        
        try:
            # 取得即時匯率（force_refresh 時強制重新查詢）
            rate_data = await self.service.get_current_rate(currency, force_refresh=force_refresh)
            rate = rate_data["rate"]
            inverse_rate = rate_data["inverse_rate"]
            
            # 取得歷史數據
            history = await self.service.get_history_rates(currency)
            
            # 計算漲跌
            change_pct = 0.0
            if history and len(history) >= 2:
                first_rate = history[0][1]
                last_rate = history[-1][1]
                change = last_rate - first_rate
                change_pct = (change / first_rate) * 100 if first_rate else 0
                
                if change >= 0:
                    trend_emoji = "📈"
                    trend_text = f"+{change:.4f} (+{change_pct:.2f}%)"
                    trend_desc = "⚠️ 台幣貶值，換匯較不划算"
                    embed_color = discord.Color.from_rgb(239, 68, 68)  # 紅色
                else:
                    trend_emoji = "📉"
                    trend_text = f"{change:.4f} ({change_pct:.2f}%)"
                    trend_desc = "✅ 台幣升值，換匯較划算"
                    embed_color = discord.Color.from_rgb(34, 197, 94)  # 綠色
            else:
                trend_emoji = "➖"
                trend_text = "無資料"
                trend_desc = ""
                embed_color = discord.Color.gold()
            
            # 建立 Embed
            embed = discord.Embed(
                title=f"{info['emoji']} {info['full_name']} ({currency}/TWD)",
                color=embed_color,
            )
            
            # 匯率資訊區塊
            rate_info = (
                f"```fix\n"
                f"💵 1 {currency} = {rate:.4f} TWD\n"
                f"🇹🇼 1 TWD  = {inverse_rate:.6f} {currency}\n"
                f"```"
            )
            embed.add_field(
                name="📊 即時匯率",
                value=rate_info,
                inline=False,
            )
            
            # 趨勢資訊
            trend_info = f"**{trend_text}**"
            if trend_desc:
                trend_info += f"\n{trend_desc}"
            
            embed.add_field(
                name=f"{trend_emoji} 90 天變化",
                value=trend_info,
                inline=True,
            )
            
            embed.add_field(
                name="🕐 更新時間",
                value=f"<t:{int(rate_data['updated_at'].timestamp())}:R>",
                inline=True,
            )
            
            # 換算範例（使用表格格式）
            calc_lines = [
                f"```",
                f"{'金額':^10} │ {'台幣':^15}",
                f"{'─'*10}─┼─{'─'*15}",
            ]
            for amount in [100, 500, 1000, 5000, 10000]:
                twd_value = amount * rate
                calc_lines.append(f"{info['emoji']} {amount:>6,} │ 🇹🇼 {twd_value:>12,.2f}")
            calc_lines.append("```")
            
            embed.add_field(
                name="🔢 快速換算",
                value="\n".join(calc_lines),
                inline=False,
            )
            
            embed.set_footer(text="💡 選擇其他貨幣或點擊「刷新匯率」更新資料")
            
            # 生成走勢圖
            files: List[discord.File] = []
            embeds: List[discord.Embed] = [embed]
            if history:
                history_chart = self.service.generate_line_chart(history, f"{info['name']} 近 90 天走勢")
                if history_chart:
                    history_file = discord.File(io.BytesIO(history_chart), filename="history_chart.svg")
                    files.append(history_file)
                    embed.set_image(url="attachment://history_chart.svg")
            
            await interaction.edit_original_response(embeds=embeds, attachments=files, view=self)
            
        except CurrencyError as e:
            embed = discord.Embed(
                title="❌ 查詢失敗",
                description=f"```\n{str(e)}\n```\n\n💡 **可能原因：**\n• API 服務暫時無法使用\n• 網路連線問題\n• 請稍後再試",
                color=discord.Color.red(),
            )
            embed.set_footer(text="如持續發生問題，請聯繫管理員")
            await interaction.edit_original_response(embed=embed, attachments=[], view=self)
        except Exception as e:
            embed = discord.Embed(
                title="❌ 發生錯誤",
                description=f"無法取得匯率資訊\n```\n{str(e)}\n```",
                color=discord.Color.red(),
            )
            embed.set_footer(text="請稍後再試或聯繫管理員")
            await interaction.edit_original_response(embed=embed, attachments=[], view=self)


# ============ 指令註冊 ============

def setup_currency_feature(bot: discord.Client) -> None:
    """註冊匯率查詢指令"""
    
    @bot.tree.command(name="money", description="💱 查詢即時匯率與走勢圖")
    async def money_command(interaction: discord.Interaction) -> None:
        view = CurrencyMenuView(owner_id=interaction.user.id)
        embed = view._build_menu_embed()
        await interaction.response.send_message(embed=embed, view=view)
        
        # 儲存訊息參考
        msg = await interaction.original_response()
        view.message = msg
    
    # 關閉時清理資源
    original_close = getattr(bot, '_original_close_for_currency', None) or bot.close
    
    async def close_with_currency() -> None:
        await get_currency_service().close()
        await original_close()
    
    if not hasattr(bot, '_original_close_for_currency'):
        bot._original_close_for_currency = bot.close
        bot.close = close_with_currency
