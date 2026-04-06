from __future__ import annotations

import aiohttp
import os

PZ_AGENT_BASE_URL = os.getenv("PZ_AGENT_BASE_URL", "http://127.0.0.1:9000").rstrip("/")
PZ_AGENT_API_KEY = os.getenv("PZ_AGENT_API_KEY", "").strip()


def _headers() -> dict[str, str]:
    return {
        "x-api-key": PZ_AGENT_API_KEY,
        "Content-Type": "application/json",
    }


async def fetch_telemetry_pending(limit: int = 50) -> dict:
    url = f"{PZ_AGENT_BASE_URL}/telemetry/pending?limit={limit}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_headers()) as resp:
            resp.raise_for_status()
            return await resp.json()


async def ack_telemetry_events(event_ids: list[str]) -> dict:
    url = f"{PZ_AGENT_BASE_URL}/telemetry/ack"
    body = {"event_ids": event_ids}

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=_headers(), json=body) as resp:
            resp.raise_for_status()
            return await resp.json()