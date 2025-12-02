"""
天氣預報功能模組
使用中央氣象署開放資料 API:
- F-D0047-089: 臺灣各鄉鎮市區未來3天逐3小時預報
- O-A0003-001: 氣象觀測站10分鐘綜觀氣象資料（即時溫度、濕度、風速）
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
import httpx
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# 載入 .env 檔案
load_dotenv()

log = logging.getLogger(__name__)

# ============ 設定 ============

# 中央氣象署 API（從環境變數讀取）
CWA_API_KEY = os.getenv("CWA_API_KEY", "")
CWA_BASE_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore"

# API 端點
FORECAST_ENDPOINT = f"{CWA_BASE_URL}/F-D0047-089"   # 逐3小時預報
OBSERVATION_ENDPOINT = f"{CWA_BASE_URL}/O-A0003-001"  # 即時觀測

# 快取設定
FORECAST_CACHE_TTL = 600   # 預報快取 10 分鐘
OBS_CACHE_TTL = 120        # 觀測快取 2 分鐘

# HTTP 設定
HEADERS = {"User-Agent": "DiscordWeatherBot/1.0", "Accept": "application/json"}
TIMEOUT = httpx.Timeout(20.0)
MAX_RETRIES = 2

# 時區
try:
    from zoneinfo import ZoneInfo
    TAIWAN_TZ = ZoneInfo("Asia/Taipei")
except ImportError:
    TAIWAN_TZ = timezone(timedelta(hours=8))

# 天氣描述對應 emoji
WEATHER_EMOJI_MAP: Dict[str, str] = {
    "晴": "☀️",
    "晴時多雲": "🌤️",
    "多雲時晴": "🌤️",
    "多雲": "⛅",
    "多雲時陰": "🌥️",
    "陰時多雲": "🌥️",
    "陰": "☁️",
    "陰天": "☁️",
    "短暫雨": "🌦️",
    "短暫陣雨": "🌦️",
    "陰短暫雨": "🌧️",
    "多雲短暫雨": "🌧️",
    "多雲時陰短暫雨": "🌧️",
    "陰時多雲短暫雨": "🌧️",
    "陰短暫陣雨": "🌧️",
    "多雲短暫陣雨": "🌧️",
    "陣雨": "🌧️",
    "雨": "🌧️",
    "陰有雨": "🌧️",
    "多雲有雨": "🌧️",
    "短暫陣雨或雷雨": "⛈️",
    "雷雨": "⛈️",
    "午後短暫雷陣雨": "⛈️",
    "多雲午後短暫雷陣雨": "⛈️",
    "有霧": "🌫️",
    "霧": "🌫️",
}

# 天氣顏色對應
WEATHER_COLOR_MAP: Dict[str, int] = {
    "晴": 0xFFD93D,      # 金黃色
    "多雲": 0x87CEEB,    # 天藍色
    "陰": 0x708090,      # 灰色
    "雨": 0x4682B4,      # 鋼藍色
    "雷": 0x800080,      # 紫色
    "霧": 0xD3D3D3,      # 淺灰色
}

def get_weather_color(description: str) -> int:
    """根據天氣描述取得對應顏色"""
    if not description:
        return 0x87CEEB
    for key, color in WEATHER_COLOR_MAP.items():
        if key in description:
            return color
    return 0x87CEEB

# 台灣縣市列表
TAIWAN_CITIES = [
    "宜蘭縣", "桃園市", "新竹縣", "苗栗縣", "彰化縣", "南投縣",
    "雲林縣", "嘉義縣", "屏東縣", "臺東縣", "花蓮縣", "澎湖縣",
    "基隆市", "新竹市", "嘉義市", "臺北市", "高雄市", "新北市",
    "臺中市", "臺南市", "連江縣", "金門縣",
]


def get_weather_emoji(description: str) -> str:
    """根據天氣描述取得對應 emoji"""
    if not description:
        return "🌈"
    for key, emoji in WEATHER_EMOJI_MAP.items():
        if key in description:
            return emoji
    return "🌈"


# ============ 資料類別 ============

class WeatherError(Exception):
    """天氣查詢錯誤"""
    pass


@dataclass
class HourlyForecast:
    """逐3小時預報"""
    time_label: str        # 時間標籤
    weather: str           # 天氣描述
    emoji: str             # 天氣 emoji
    temperature: float     # 溫度
    feels_like: Optional[float]  # 體感溫度
    humidity: Optional[int]  # 濕度 %
    rain_prob: int         # 降雨機率 %


@dataclass
class Observation:
    """即時觀測資料"""
    station_name: str
    temperature: float        # 溫度 °C
    humidity: Optional[float]  # 濕度 %
    wind_speed: Optional[float]  # 風速 m/s
    weather_desc: str
    observed_at: datetime


@dataclass
class WeatherReport:
    """完整天氣報告"""
    location: str                      # 縣市
    timezone_name: str
    observation: Optional[Observation]  # 即時觀測
    forecasts: List[HourlyForecast]     # 逐3小時預報


@dataclass
class CacheEntry:
    """快取項目"""
    data: Any
    expires_at: float


# ============ 天氣服務 ============

class WeatherService:
    """中央氣象署天氣服務"""
    
    _instance: Optional["WeatherService"] = None
    
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._forecast_cache: Optional[CacheEntry] = None
        self._obs_cache: Optional[CacheEntry] = None
        self._lock = asyncio.Lock()
    
    @classmethod
    def get_instance(cls) -> "WeatherService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=TIMEOUT,
                headers=HEADERS,
                verify=False  # 跳過 SSL 驗證（Windows 憑證問題）
            )
        return self._client
    
    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    def _is_valid(self, entry: Optional[CacheEntry]) -> bool:
        return entry is not None and entry.expires_at > time.time()
    
    def _normalize(self, name: str) -> str:
        """台→臺"""
        return name.strip().replace("台", "臺")
    
    async def fetch_weather(self, location: str) -> WeatherReport:
        """取得天氣報告"""
        location = location.strip()
        if not location:
            raise WeatherError("請輸入縣市名稱")
        
        city = self._match_city(location)
        if not city:
            raise WeatherError(
                f"找不到「{location}」\n"
                f"支援的縣市：{', '.join(TAIWAN_CITIES)}"
            )
        
        # 並行取得預報和觀測
        forecasts, observation = await asyncio.gather(
            self._fetch_forecasts(city),
            self._fetch_observation(city),
            return_exceptions=True,
        )
        
        if isinstance(forecasts, Exception):
            log.error("Forecast error: %s", forecasts)
            forecasts = []
        if isinstance(observation, Exception):
            log.warning("Observation error: %s", observation)
            observation = None
        
        return WeatherReport(
            location=city,
            timezone_name="Asia/Taipei",
            observation=observation,
            forecasts=forecasts,
        )
    
    def _match_city(self, query: str) -> Optional[str]:
        """匹配縣市名稱"""
        normalized = self._normalize(query)
        
        # 精確匹配
        for city in TAIWAN_CITIES:
            if normalized == city:
                return city
        
        # 部分匹配
        for city in TAIWAN_CITIES:
            if normalized in city or city in normalized:
                return city
        
        return None
    
    async def _fetch_forecasts(self, city: str) -> List[HourlyForecast]:
        """取得逐3小時預報"""
        # 取得資料
        data = await self._get_forecast_data()
        
        # 尋找縣市資料 (注意：API 回傳的 key 是 PascalCase)
        locations_list = data.get("records", {}).get("Locations", [])
        if not locations_list:
            log.warning("No Locations in API response")
            return []
        
        # F-D0047-089 的資料結構
        all_locations = []
        for loc_group in locations_list:
            all_locations.extend(loc_group.get("Location", []))
        
        # 找到該縣市
        target = None
        normalized_city = self._normalize(city)
        for loc in all_locations:
            loc_name = self._normalize(loc.get("LocationName", ""))
            if loc_name == normalized_city:
                target = loc
                break
        
        if not target:
            log.warning("City not found in forecast: %s", city)
            return []
        
        return self._parse_forecasts(target)
    
    async def _get_forecast_data(self) -> Dict:
        """取得預報資料（有快取）"""
        if self._is_valid(self._forecast_cache):
            return self._forecast_cache.data
        
        client = await self._get_client()
        params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        
        response = await self._request(client, FORECAST_ENDPOINT, params)
        data = response.json()
        
        self._forecast_cache = CacheEntry(
            data=data,
            expires_at=time.time() + FORECAST_CACHE_TTL
        )
        return data
    
    def _parse_forecasts(self, location_data: Dict) -> List[HourlyForecast]:
        """解析預報資料 (PascalCase keys, 中文 ElementName)"""
        # 建立 element 索引 (中文名稱)
        elements = {
            elem.get("ElementName"): elem.get("Time", [])
            for elem in location_data.get("WeatherElement", [])
        }
        
        now = datetime.now(TAIWAN_TZ)
        forecasts: List[HourlyForecast] = []
        
        # 天氣現象
        wx_times = elements.get("天氣現象", [])
        t_times = elements.get("溫度", [])        # 溫度
        at_times = elements.get("體感溫度", [])   # 體感溫度
        rh_times = elements.get("相對濕度", [])    # 濕度
        pop_times = elements.get("3小時降雨機率", [])
        
        for idx, wx_item in enumerate(wx_times):
            start_str = wx_item.get("StartTime", "")
            end_str = wx_item.get("EndTime", "")
            
            try:
                # ISO 格式帶時區
                start = datetime.fromisoformat(start_str)
                end = datetime.fromisoformat(end_str)
            except ValueError:
                continue
            
            # 跳過已過去的時段
            if end <= now:
                continue
            
            # 天氣描述 (PascalCase)
            wx_vals = wx_item.get("ElementValue", [])
            weather = wx_vals[0].get("Weather", "-") if wx_vals else "-"
            emoji = get_weather_emoji(weather)
            
            # 溫度
            temp = self._get_value_at_pascal(t_times, idx, "Temperature")
            temperature = float(temp) if temp else 0.0
            
            # 體感溫度
            at = self._get_value_at_pascal(at_times, idx, "ApparentTemperature")
            feels_like = float(at) if at else None
            
            # 濕度
            rh = self._get_value_at_pascal(rh_times, idx, "RelativeHumidity")
            humidity = int(rh) if rh and str(rh).isdigit() else None
            
            # 降雨機率
            pop = self._get_value_at_pascal(pop_times, idx, "ProbabilityOfPrecipitation")
            rain_prob = int(pop) if pop and str(pop).isdigit() else 0
            
            # 時間標籤
            time_label = self._format_label(now, start)
            
            forecasts.append(HourlyForecast(
                time_label=time_label,
                weather=weather,
                emoji=emoji,
                temperature=temperature,
                feels_like=feels_like,
                humidity=humidity,
                rain_prob=rain_prob,
            ))
        
        return forecasts
    
    def _get_value_at_pascal(self, times: List[Dict], idx: int, key: str) -> Optional[str]:
        """取得指定索引的值 (PascalCase)"""
        if idx >= len(times):
            return None
        vals = times[idx].get("ElementValue", [])
        if vals and isinstance(vals, list) and vals[0]:
            return vals[0].get(key)
        return None
    
    def _format_label(self, now: datetime, target: datetime) -> str:
        """格式化時間標籤"""
        if target.date() == now.date():
            prefix = "今天"
        elif target.date() == (now + timedelta(days=1)).date():
            prefix = "明天"
        elif target.date() == (now + timedelta(days=2)).date():
            prefix = "後天"
        else:
            prefix = target.strftime("%m/%d")
        return f"{prefix} {target.strftime('%H:%M')}"
    
    async def _fetch_observation(self, city: str) -> Optional[Observation]:
        """取得即時觀測"""
        try:
            data = await self._get_obs_data()
            
            # 注意：觀測資料可能是不同的 key 結構
            stations = data.get("records", {}).get("Station", [])
            if not stations:
                # 嘗試其他可能的 key
                stations = data.get("records", {}).get("station", [])
            
            normalized = self._normalize(city)
            
            # 找該縣市的測站
            for station in stations:
                geo = station.get("GeoInfo", {}) or station.get("geoInfo", {})
                county = geo.get("CountyName", "") or geo.get("countyName", "")
                if self._normalize(county) == normalized:
                    return self._parse_observation(station)
            
            return None
        except Exception as e:
            log.warning("Observation fetch failed: %s", e)
            return None
    
    async def _get_obs_data(self) -> Dict:
        """取得觀測資料（有快取）"""
        if self._is_valid(self._obs_cache):
            return self._obs_cache.data
        
        client = await self._get_client()
        params = {"Authorization": CWA_API_KEY, "format": "JSON"}
        
        response = await self._request(client, OBSERVATION_ENDPOINT, params)
        data = response.json()
        
        self._obs_cache = CacheEntry(
            data=data,
            expires_at=time.time() + OBS_CACHE_TTL
        )
        return data
    
    def _parse_observation(self, station: Dict) -> Observation:
        """解析觀測站資料"""
        weather = station.get("WeatherElement", {}) or station.get("weatherElement", {})
        obs_time_data = station.get("ObsTime", {}) or station.get("obsTime", {})
        obs_time_str = obs_time_data.get("DateTime", "") or obs_time_data.get("dateTime", "")
        
        try:
            obs_time = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
            obs_time = obs_time.astimezone(TAIWAN_TZ)
        except ValueError:
            obs_time = datetime.now(TAIWAN_TZ)
        
        # 處理可能為 None 或 -99 的值 (支援 PascalCase 和 camelCase)
        temp = weather.get("AirTemperature") or weather.get("airTemperature")
        if temp is None or temp == -99:
            temp = 0.0
        
        humidity = weather.get("RelativeHumidity") or weather.get("relativeHumidity")
        if humidity is None or humidity == -99:
            humidity = None
        
        wind = weather.get("WindSpeed") or weather.get("windSpeed")
        if wind is None or wind == -99:
            wind = None
        
        weather_desc = weather.get("Weather") or weather.get("weather") or "-"
        station_name = station.get("StationName") or station.get("stationName") or ""
        
        return Observation(
            station_name=station_name,
            temperature=float(temp),
            humidity=float(humidity) if humidity is not None else None,
            wind_speed=float(wind) if wind is not None else None,
            weather_desc=weather_desc if weather_desc else "-",
            observed_at=obs_time,
        )
    
    async def _request(self, client: httpx.AsyncClient, url: str, params: Dict) -> httpx.Response:
        """HTTP 請求（含重試）"""
        last_exc: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response
            except (httpx.HTTPStatusError, httpx.RequestError) as e:
                last_exc = e
                log.warning("API retry %d: %s", attempt + 1, e)
        raise WeatherError(f"API 請求失敗：{last_exc}")


def get_weather_service() -> WeatherService:
    """取得天氣服務單例"""
    return WeatherService.get_instance()


# ============ Discord 整合 ============

def _build_weather_embed(report: WeatherReport) -> discord.Embed:
    """建立天氣預報 Embed"""
    # 即時觀測 or 第一筆預報
    obs = report.observation
    first_fc = report.forecasts[0] if report.forecasts else None
    
    if obs:
        current_emoji = get_weather_emoji(obs.weather_desc)
        current_desc = obs.weather_desc
        current_temp = obs.temperature
        feels_like = first_fc.feels_like if first_fc else None
        humidity = obs.humidity
        wind_speed = obs.wind_speed
        rain_prob = first_fc.rain_prob if first_fc else None
    elif first_fc:
        current_emoji = first_fc.emoji
        current_desc = first_fc.weather
        current_temp = first_fc.temperature
        feels_like = first_fc.feels_like
        humidity = first_fc.humidity
        wind_speed = None
        rain_prob = first_fc.rain_prob
    else:
        current_emoji = "🌈"
        current_desc = "-"
        current_temp = 0.0
        feels_like = None
        humidity = None
        wind_speed = None
        rain_prob = None
    
    # 根據天氣設定顏色
    embed_color = get_weather_color(current_desc)
    
    embed = discord.Embed(
        title=f"{current_emoji} {report.location} 天氣預報",
        color=embed_color,
    )
    
    # 主要天氣描述
    embed.description = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"　　　　**{current_desc}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 臺灣 {report.location} · {report.timezone_name}"
    )
    
    # 即時概況 - 使用 inline fields 更清晰
    temp_str = f"**{current_temp:.1f}°C**"
    feels_str = f"**{feels_like:.0f}°C**" if feels_like is not None else "-"
    humidity_str = f"**{humidity:.0f}%**" if humidity is not None else "-"
    rain_str = f"**{rain_prob}%**" if rain_prob is not None else "-"
    
    # 風速 m/s → km/h
    if wind_speed is not None:
        wind_str = f"**{wind_speed * 3.6:.1f}** km/h"
    else:
        wind_str = "-"
    
    embed.add_field(name="🌡️ 溫度", value=temp_str, inline=True)
    embed.add_field(name="🤒 體感", value=feels_str, inline=True)
    embed.add_field(name="💧 濕度", value=humidity_str, inline=True)
    embed.add_field(name="🌧️ 降雨", value=rain_str, inline=True)
    embed.add_field(name="💨 風速", value=wind_str, inline=True)
    
    # 穿衣建議
    if current_temp >= 30:
        clothing = "🩳 短袖短褲，注意防曬"
    elif current_temp >= 25:
        clothing = "👕 輕薄衣物，舒適透氣"
    elif current_temp >= 20:
        clothing = "🧥 薄外套，早晚較涼"
    elif current_temp >= 15:
        clothing = "🧣 外套毛衣，注意保暖"
    else:
        clothing = "🧥 厚外套，注意防寒"
    
    embed.add_field(name="👔 穿衣建議", value=clothing, inline=True)
    
    # 各時段預測
    if report.forecasts:
        forecast_lines = []
        for fc in report.forecasts[:8]:
            # 使用更緊湊的格式
            rain_indicator = "☔" if fc.rain_prob >= 50 else "　"
            line = f"`{fc.time_label:^12}` {fc.emoji} {fc.temperature:>4.0f}°C {rain_indicator}{fc.rain_prob:>2}%"
            forecast_lines.append(line)
        
        # 標題行
        header = "```\n時間          天氣   溫度   降雨\n" + "─" * 32 + "\n```"
        
        chunks = _chunk_lines(forecast_lines, max_len=900)
        total = len(chunks)
        
        for idx, chunk in enumerate(chunks, start=1):
            name = f"⏰ 未來預報 ({idx}/{total})" if total > 1 else "⏰ 未來 24 小時預報"
            embed.add_field(name=name, value=chunk, inline=False)
    else:
        embed.add_field(
            name="⏰ 未來預報",
            value="⚠️ 目前沒有可用的預報資料",
            inline=False,
        )
    
    # 提醒
    if rain_prob and rain_prob >= 50:
        embed.add_field(
            name="☔ 提醒",
            value="降雨機率較高，記得帶傘！",
            inline=False,
        )
    
    embed.set_footer(text="📡 資料來源：中央氣象署 · 祝你有個美好的一天！")
    
    return embed


def _chunk_lines(lines: List[str], max_len: int = 1000) -> List[str]:
    """分割文字"""
    chunks: List[str] = []
    buffer: List[str] = []
    current_len = 0
    
    for line in lines:
        line_len = len(line) + 1
        if buffer and current_len + line_len > max_len:
            chunks.append("\n".join(buffer))
            buffer = [line]
            current_len = line_len
        else:
            buffer.append(line)
            current_len += line_len
    
    if buffer:
        chunks.append("\n".join(buffer))
    
    return chunks


async def location_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    """地點自動完成"""
    normalized = current.strip().replace("台", "臺").lower()
    matches = [
        city for city in TAIWAN_CITIES
        if normalized in city.lower() or not current
    ]
    return [
        app_commands.Choice(name=city, value=city)
        for city in matches[:25]
    ]


def register_weather_commands(bot: commands.Bot) -> None:
    """註冊天氣指令"""
    service = get_weather_service()
    
    @bot.tree.command(name="weather", description="查詢臺灣縣市天氣預報（逐3小時、含即時觀測）")
    @app_commands.describe(location="輸入縣市名稱")
    @app_commands.autocomplete(location=location_autocomplete)
    async def weather(interaction: discord.Interaction, location: str) -> None:
        await interaction.response.defer(thinking=True)
        
        try:
            report = await service.fetch_weather(location)
            embed = _build_weather_embed(report)
            await interaction.followup.send(embed=embed)
        except WeatherError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        except Exception as exc:
            log.exception("Weather command error")
            await interaction.followup.send(
                "⚠️ 發生未預期的錯誤，請稍後再試",
                ephemeral=True
            )
