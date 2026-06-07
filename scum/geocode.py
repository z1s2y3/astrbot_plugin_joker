import httpx
from astrbot.api import logger

PROXIES_LIST = [None]

async def reverse_geocode(lat: float, lon: float) -> str:
    url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"

    for proxy in PROXIES_LIST:
        try:
            timeout = httpx.Timeout(10.0, connect=5.0)
            client_kwargs = {"timeout": timeout}
            if proxy:
                client_kwargs["proxy"] = proxy
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()

                address = data.get("address", {})
                country = address.get("country", "")
                state = address.get("state", "")
                city = address.get("city", "")
                town = address.get("town", "")
                village = address.get("village", "")

                if city:
                    return f"{city} ({country})" if country else city
                elif town:
                    return f"{town} ({country})" if country else town
                elif village:
                    return f"{village} ({country})" if country else village
                elif state:
                    return f"{state} ({country})" if country else state
                elif country:
                    return country

                return "未知"

        except httpx.TimeoutException:
            continue
        except httpx.HTTPStatusError:
            continue
        except Exception as e:
            logger.error(f"地理编码失败: {e}")
            continue

    return "未知"
