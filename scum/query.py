import httpx
import time
from .geocode import reverse_geocode, PROXIES_LIST

# SCUM Steam App ID
SCUM_APP_ID = "585950"

class ServerQuery:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.proxy_index = 0

    async def _get_proxy_client(self):
        for _ in range(len(PROXIES_LIST)):
            proxy = PROXIES_LIST[self.proxy_index]
            self.proxy_index = (self.proxy_index + 1) % len(PROXIES_LIST)
            try:
                client_kwargs = {
                    "timeout": 20.0,
                    "follow_redirects": True,
                    "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
                }
                if proxy:
                    client_kwargs["proxy"] = proxy
                client = httpx.AsyncClient(**client_kwargs)
                return client
            except Exception:
                continue
        return httpx.AsyncClient(timeout=20.0)

    async def query_by_id_simple(self, server_id: str) -> str:
        try:
            async with await self._get_proxy_client() as client:
                response = await client.get(f"{self.base_url}/servers/{server_id}")
                response.raise_for_status()
                data = response.json()
            server = data.get("data", {})
            attrs = server.get("attributes", {})
            name = attrs.get("name", "未知")
            status = attrs.get("status", "unknown")
            players = attrs.get("players", 0)
            max_players = attrs.get("maxPlayers", 0)
            ip = attrs.get("ip", "未知")
            port = attrs.get("port", "未知")
            is_online = status == "online"
            rank = attrs.get("rank") or attrs.get("scoreRank") or attrs.get("score") or attrs.get("position") or "None"
            result = f"📡 服务器状态查询结果：\n\nRank: #{rank}\nID: {server_id}\n名称: {name}\n在线人数: {players}/{max_players}\n状态: {'在线 ✅' if is_online else '离线 ❌'}\nIP: {ip}:{port}"
            if not is_online:
                result += "\n⚠️ 服务器当前离线，请稍后再试。"
            return result
        except httpx.TimeoutException:
            return "⏰ 查询超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"❌ 未找到ID为 {server_id} 的服务器。"
            return f"❌ 查询失败: {e.response.status_code}"
        except Exception as e:
            return f"❌ 查询失败: {str(e)}"

    async def query_by_id_detailed(self, server_id: str) -> str:
        try:
            async with await self._get_proxy_client() as client:
                response = await client.get(f"{self.base_url}/servers/{server_id}")
                response.raise_for_status()
                data = response.json()
            server = data.get("data", {})
            attrs = server.get("attributes", {})
            server_id = server.get("id", "未知")
            name = attrs.get("name", "未知")
            status = attrs.get("status", "unknown")
            players = attrs.get("players", 0)
            max_players = attrs.get("maxPlayers", 0)
            ip = attrs.get("ip", "未知")
            port = attrs.get("port", "未知")
            country = attrs.get("country", "未知")
            location = attrs.get("location", [])
            is_online = status == "online"
            rank = attrs.get("rank") or attrs.get("scoreRank") or attrs.get("score") or attrs.get("position") or "None"
            location_str = "未知"
            region_name = "未知"
            if location and len(location) == 2:
                location_str = f"{location[0]}, {location[1]}"
                region_name = await reverse_geocode(location[1], location[0])
            game_time = "未知"
            details = attrs.get("details", {})
            if details:
                game_time = details.get("gameTime", "未知")
            result_lines = [
                f"📊 服务器详细信息：",
                f"\nRank: #{rank}",
                f"\nID: {server_id}",
                f"\n名称: {name}",
                f"\n在线人数: {players}/{max_players}",
                f"\n状态: {'在线 ✅' if is_online else '离线 ❌'}",
                f"\nIP: {ip}:{port}",
                f"\n地区: {country}",
                f"\n坐标: {location_str}",
                f"\n真实地区: {region_name}",
                f"\n游戏时间: {game_time}"
            ]
            return "\n".join(result_lines)
        except httpx.TimeoutException:
            return "⏰ 查询超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return f"❌ 未找到ID为 {server_id} 的服务器。"
            return f"❌ 查询失败: {e.response.status_code}"
        except Exception as e:
            return f"❌ 查询失败: {str(e)}"

    async def search_servers(self, keyword: str) -> str:
        try:
            async with await self._get_proxy_client() as client:
                params = {"filter[game]": "scum", "filter[search]": keyword, "page[size]": 10}
                response = await client.get(f"{self.base_url}/servers", params=params)
                response.raise_for_status()
                data = response.json()
            servers = data.get("data", [])
            if not servers:
                return f"❌ 未找到包含 '{keyword}' 的SCUM服务器。"
            result_lines = [f"🔍 找到 {len(servers)} 个服务器："]
            servers_with_rank = []
            for server in servers[:10]:
                attrs = server.get("attributes", {})
                rank = attrs.get("rank") or attrs.get("scoreRank") or attrs.get("score") or attrs.get("position")
                rank_int = int(rank) if rank and str(rank).isdigit() else float('inf')
                servers_with_rank.append((rank_int, server))
            servers_with_rank.sort(key=lambda x: x[0])
            for index, (rank_int, server) in enumerate(servers_with_rank, start=1):
                attrs = server.get("attributes", {})
                server_id = server.get("id", "未知")
                name = attrs.get("name", "未知")
                status = attrs.get("status", "unknown")
                players = attrs.get("players", 0)
                max_players = attrs.get("maxPlayers", 0)
                ip = attrs.get("ip", "未知")
                port = attrs.get("port", "未知")
                country = attrs.get("country", "未知")
                location = attrs.get("location", [])
                is_online = status == "online"
                rank = attrs.get("rank") or attrs.get("scoreRank") or attrs.get("score") or attrs.get("position") or "None"
                location_str = "未知"
                region_name = "未知"
                if location and len(location) == 2:
                    location_str = f"{location[0]}, {location[1]}"
                    region_name = await reverse_geocode(location[1], location[0])
                result_lines.append(
                    f"\n{index}. Rank: #{rank}"
                    f"\nID: {server_id}"
                    f"\n服务器名称: {name}"
                    f"\n在线人数: {players}/{max_players}"
                    f"\n地区: {country}"
                    f"\n是否开机: {'是 ✅' if is_online else '否 ❌'}"
                    f"\nIP地址: {ip}:{port}"
                    f"\n坐标位置: {location_str}"
                    f"\n真实地区: {region_name}"
                    f"\n状态: {status}"
                )
            return "\n".join(result_lines)
        except httpx.TimeoutException:
            return "⏰ 搜索超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            return f"❌ API请求失败: {e.response.status_code}"
        except httpx.ConnectError:
            return "❌ 网络连接失败，请检查网络设置或稍后重试。"
        except Exception as e:
            error_msg = str(e)
            if "connection" in error_msg.lower() or "failed" in error_msg.lower():
                return "❌ 网络连接失败，请检查网络设置或稍后重试。"
            return f"❌ 搜索失败: {error_msg}"

    async def get_server_ranking(self) -> str:
        try:
            async with await self._get_proxy_client() as client:
                params = {"filter[game]": "scum", "page[size]": 10, "sort": "-players"}
                response = await client.get(f"{self.base_url}/servers", params=params)
                response.raise_for_status()
                data = response.json()
            servers = data.get("data", [])
            if not servers:
                return "❌ 未能获取服务器排名。"
            result_lines = ["🏆 SCUM 服务器排名（按在线人数）："]
            servers_with_rank = []
            for server in servers[:10]:
                attrs = server.get("attributes", {})
                rank = attrs.get("rank") or attrs.get("scoreRank") or attrs.get("score") or attrs.get("position")
                rank_int = int(rank) if rank and str(rank).isdigit() else float('inf')
                servers_with_rank.append((rank_int, server))
            servers_with_rank.sort(key=lambda x: x[0])
            for index, (rank_int, server) in enumerate(servers_with_rank, start=1):
                attrs = server.get("attributes", {})
                server_id = server.get("id", "未知")
                name = attrs.get("name", "未知")
                status = attrs.get("status", "unknown")
                players = attrs.get("players", 0)
                max_players = attrs.get("maxPlayers", 0)
                ip = attrs.get("ip", "未知")
                port = attrs.get("port", "未知")
                country = attrs.get("country", "未知")
                location = attrs.get("location", [])
                is_online = status == "online"
                rank = attrs.get("rank") or attrs.get("scoreRank") or attrs.get("score") or attrs.get("position") or "None"
                location_str = "未知"
                region_name = "未知"
                if location and len(location) == 2:
                    location_str = f"{location[0]}, {location[1]}"
                    region_name = await reverse_geocode(location[1], location[0])
                result_lines.append(
                    f"\n{index}. Rank: #{rank}"
                    f"\nID: {server_id}"
                    f"\n服务器名称: {name}"
                    f"\n在线人数: {players}/{max_players}"
                    f"\n地区: {country}"
                    f"\n是否开机: {'是 ✅' if is_online else '否 ❌'}"
                    f"\nIP地址: {ip}:{port}"
                    f"\n坐标位置: {location_str}"
                    f"\n真实地区: {region_name}"
                    f"\n状态: {status}"
                )
            return "\n".join(result_lines)
        except httpx.TimeoutException:
            return "⏰ 获取排名超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            return f"❌ API请求失败: {e.response.status_code}"
        except httpx.ConnectError:
            return "❌ 网络连接失败，请检查网络设置或稍后重试。"
        except Exception as e:
            error_msg = str(e)
            if "connection" in error_msg.lower() or "failed" in error_msg.lower():
                return "❌ 网络连接失败，请检查网络设置或稍后重试。"
            return f"❌ 获取排名失败: {error_msg}"

    async def get_news(self, count: int = 5) -> str:
        """获取SCUM新闻/更新"""
        try:
            async with await self._get_proxy_client() as client:
                params = {
                    "appid": SCUM_APP_ID,
                    "count": count,
                    "maxlength": 500,
                    "format": "json"
                }
                response = await client.get(
                    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/",
                    params=params
                )
                response.raise_for_status()
                data = response.json()
            
            news_items = data.get("appnews", {}).get("newsitems", [])
            if not news_items:
                return "❌ 暂时没有SCUM新闻。"
            
            result_lines = ["📰 SCUM 最新资讯：\n"]
            
            for i, item in enumerate(news_items[:count], start=1):
                title = item.get("title", "无标题")
                # 清理HTML标签
                import re
                content = re.sub(r'<[^>]+>', '', item.get("contents", ""))
                # 限制内容长度
                if len(content) > 300:
                    content = content[:300] + "..."
                # 时间戳转换
                timestamp = item.get("date", 0)
                if timestamp:
                    date_str = time.strftime("%Y-%m-%d", time.localtime(timestamp))
                else:
                    date_str = "未知日期"
                
                result_lines.append(f"{i}. {title}")
                result_lines.append(f"📅 {date_str}")
                result_lines.append(f"📝 {content}\n")
            
            return "\n".join(result_lines)
        except httpx.TimeoutException:
            return "⏰ 获取新闻超时，请稍后重试。"
        except httpx.HTTPStatusError as e:
            return f"❌ 获取新闻失败: {e.response.status_code}"
        except httpx.ConnectError:
            return "❌ 网络连接失败，请检查网络设置或稍后重试。"
        except Exception as e:
            return f"❌ 获取新闻失败: {str(e)}"
