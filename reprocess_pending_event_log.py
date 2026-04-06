from __future__ import annotations

import asyncio
import json

from utils.db import get_db, mark_event_log_processed, mark_event_log_error
from utils.pz_bridge import _apply_event_projections


async def main():
    db = await get_db()

    cur = await db.execute(
        """
        SELECT
            event_id,
            event_type,
            ts,
            steam_id,
            payload_json,
            source,
            source_line
        FROM event_log
        WHERE process_status = 'pending'
        ORDER BY received_at ASC
        """
    )
    rows = await cur.fetchall()

    print(f"[reprocess] pendentes encontrados: {len(rows)}")

    ok_count = 0
    err_count = 0

    for row in rows:
        event_id = row[0]
        event_type = row[1]
        ts = row[2]
        steam_id = row[3]
        payload_json = row[4]
        source = row[5]
        source_line = row[6]

        try:
            payload = json.loads(payload_json or "{}")
            if not isinstance(payload, dict):
                payload = {}

            event = {
                "event_id": event_id,
                "event_type": event_type,
                "ts": ts,
                "steam_id": steam_id,
                "payload": payload,
                "meta": {
                    "source": source,
                    "line": source_line,
                },
            }

            await _apply_event_projections(event)
            await mark_event_log_processed(event_id)

            ok_count += 1
            print(f"[reprocess] OK   {event_id} | {event_type}")

        except Exception as exc:
            await mark_event_log_error(event_id, str(exc))

            err_count += 1
            print(f"[reprocess] ERRO {event_id} | {event_type} | {exc}")

    print(f"[reprocess] concluído | ok={ok_count} erro={err_count}")


if __name__ == "__main__":
    asyncio.run(main())