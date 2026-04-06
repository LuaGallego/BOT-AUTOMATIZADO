from __future__ import annotations

import os
from typing import Any

import aiohttp


async def get_api_base() -> str:
    base = os.getenv("PZ_AGENT_BASE_URL", "").strip()
    if not base:
        raise RuntimeError("PZ_AGENT_BASE_URL nao configurado")
    print(f"[pz_api] usando PZ_AGENT_BASE_URL={base}")
    return base.rstrip("/")


async def build_headers() -> dict[str, str]:
    api_key = os.getenv("PZ_AGENT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("PZ_AGENT_API_KEY nao configurado")

    if len(api_key) >= 8:
        masked = api_key[:4] + "**********" + api_key[-4:]
    else:
        masked = "********"

    print(f"[pz_api] usando PZ_AGENT_API_KEY={masked}")

    return {
        "x-api-key": api_key,
    }


async def pz_get_pending_events(limit: int = 100) -> dict[str, Any]:
    base_url = await get_api_base()
    headers = await build_headers()
    url = f"{base_url}/telemetry/pending?limit={int(limit)}"

    print(f"[pz_api] GET {url}")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            print(f"[pz_api] GET /telemetry/pending status={resp.status}")
            text = await resp.text()

            if resp.status >= 400:
                raise RuntimeError(f"GET /telemetry/pending falhou: {resp.status} - {text}")

            try:
                return await resp.json()
            except Exception as exc:
                raise RuntimeError(f"Resposta invalida de /telemetry/pending: {exc} | body={text}")


async def pz_ack_events(event_ids: list[str]) -> dict[str, Any]:
    base_url = await get_api_base()
    headers = await build_headers()
    headers["Content-Type"] = "application/json"
    url = f"{base_url}/telemetry/ack"

    body = {"event_ids": event_ids or []}

    print(f"[pz_api] POST {url} | qtd_event_ids={len(body['event_ids'])}")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=body) as resp:
            print(f"[pz_api] POST /telemetry/ack status={resp.status}")
            text = await resp.text()

            if resp.status >= 400:
                raise RuntimeError(f"POST /telemetry/ack falhou: {resp.status} - {text}")

            try:
                return await resp.json()
            except Exception as exc:
                raise RuntimeError(f"Resposta invalida de /telemetry/ack: {exc} | body={text}")


async def pz_post_link_result(result_data: dict[str, Any]) -> dict[str, Any]:
    base_url = await get_api_base()
    headers = await build_headers()
    headers["Content-Type"] = "application/json"
    url = f"{base_url}/link/result"

    print(f"[pz_api] POST {url} result_data={result_data}")

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=result_data) as resp:
            text = await resp.text()
            print(f"[pz_api] POST /link/result status={resp.status} body={text}")

            if resp.status >= 400:
                raise RuntimeError(f"POST /link/result falhou: {resp.status} - {text}")

            try:
                return await resp.json()
            except Exception as exc:
                raise RuntimeError(f"Resposta invalida de /link/result: {exc} | body={text}")