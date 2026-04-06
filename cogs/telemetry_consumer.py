from __future__ import annotations

import asyncio
import logging
from typing import Any

from discord.ext import commands, tasks

from utils.pz_api import pz_get_pending_events, pz_ack_events
from utils.pz_bridge import process_one_event


log = logging.getLogger(__name__)


class TelemetryConsumer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()
        self.poll_telemetry.start()

    def cog_unload(self):
        self.poll_telemetry.cancel()

    @tasks.loop(seconds=5)
    async def poll_telemetry(self):
        if self._lock.locked():
            return

        async with self._lock:
            await self._run_once()

    @poll_telemetry.before_loop
    async def before_poll_telemetry(self):
        await self.bot.wait_until_ready()

    async def _run_once(self) -> None:
        try:
            response = await pz_get_pending_events(limit=100)
        except Exception as exc:
            log.exception("[telemetry_consumer] Falha ao buscar /telemetry/pending: %s", exc)
            return

        if not isinstance(response, dict):
            log.error("[telemetry_consumer] Resposta inválida de /telemetry/pending: %r", response)
            return

        if not response.get("ok", False):
            log.error("[telemetry_consumer] API retornou ok=false em /telemetry/pending: %r", response)
            return

        events = response.get("events") or []
        if not isinstance(events, list) or not events:
            return

        ack_event_ids: list[str] = []
        processed_count = 0
        error_count = 0

        for index, raw_event in enumerate(events, start=1):
            if not isinstance(raw_event, dict):
                error_count += 1
                log.error("[telemetry_consumer] Evento inválido na fila (não é dict): %r", raw_event)
                continue

            try:
                result = await process_one_event(raw_event, source_line=index)
            except Exception as exc:
                error_count += 1
                event_id = str(raw_event.get("event_id") or "").strip() or None
                log.exception(
                    "[telemetry_consumer] Exceção processando evento event_id=%s: %s",
                    event_id,
                    exc,
                )
                continue

            event_id = str(result.get("event_id") or "").strip()
            status = str(result.get("status") or "").strip().lower()
            ok = bool(result.get("ok"))

            if not event_id:
                error_count += 1
                log.error("[telemetry_consumer] Resultado sem event_id: %r", result)
                continue
            
            if ok and status in {
                "processed",
                "already_processed",
                "reprocessed_existing",
                "duplicate_no_state_check",
                "processed_rejected_link_code",
            }:
                ack_event_ids.append(event_id)
                processed_count += 1
            else:
                error_count += 1
                log.error(
                    "[telemetry_consumer] Evento não confirmado para ACK | event_id=%s | status=%s | result=%r",
                    event_id,
                    status,
                    result,
                )

        if ack_event_ids:
            try:
                ack_response = await pz_ack_events(ack_event_ids)
                if not isinstance(ack_response, dict) or not ack_response.get("ok", False):
                    log.error(
                        "[telemetry_consumer] Falha no ACK dos eventos. ack_response=%r ids=%r",
                        ack_response,
                        ack_event_ids,
                    )
                else:
                    log.info(
                        "[telemetry_consumer] ACK enviado com sucesso. total=%s acked=%s errors=%s",
                        len(ack_event_ids),
                        ack_response.get("acked"),
                        error_count,
                    )
            except Exception as exc:
                log.exception(
                    "[telemetry_consumer] Exceção ao enviar ACK de %s eventos: %s",
                    len(ack_event_ids),
                    exc,
                )
        elif processed_count or error_count:
            log.info(
                "[telemetry_consumer] Ciclo concluído sem ACK. processed=%s errors=%s",
                processed_count,
                error_count,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(TelemetryConsumer(bot))