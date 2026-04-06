import aiohttp


class PZServerAgentClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> dict:
        return {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
        }

    async def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(url, headers=self._headers()) as resp:
                text = await resp.text()

                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status}: {text}")

                try:
                    return await resp.json()
                except Exception:
                    raise RuntimeError(f"Resposta não JSON em {url}: {text}")

    async def health(self) -> dict:
        return await self._get("/health")

    async def server_info(self) -> dict:
        return await self._get("/server/info")

    async def server_status(self) -> dict:
        return await self._get("/server/status")

    async def server_uptime(self) -> dict:
        return await self._get("/server/uptime")

    async def players_online(self) -> dict:
        return await self._get("/players/online")

    async def debug_sources(self) -> dict:
        return await self._get("/debug/sources")

    async def log_dates(self) -> dict:
        return await self._get("/logs/dates")

    async def logs_by_date(self, date_str: str) -> dict:
        return await self._get(f"/logs/list?date={date_str}")