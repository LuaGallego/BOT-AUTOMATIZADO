from __future__ import annotations

from typing import Any
import time
from . import db
import asyncio
from utils.pz_rcon import enviar_comando_rcon

from utils.db import (
    insert_raw_event_log,
    get_event_log_by_id,
    mark_event_log_processed,
    mark_event_log_error,
    update_server_state_from_heartbeat,
    upsert_player_identity_from_event,
    upsert_player_profile_from_event,
    upsert_player_state_from_event,
    upsert_player_lifetime_stats_from_kill_delta,
    apply_profile_progress_to_lifetime_stats,
    upsert_faction_state_from_event,
    insert_player_death_from_event,
    open_player_session_from_event,
    close_player_session_from_event,
    consume_link_code_submit_event,
    get_active_player_link_status_by_pz_steam_id,
    sync_player_identity_link_status,
    deactivate_player_link_by_steam_id,
    deactivate_player_link_by_discord_id,
)

from utils.pz_api import pz_post_link_result
from utils.db import set_config

LAST_PULSE = 0

def _payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    return payload if isinstance(payload, dict) else {}

def _extract_steam_id(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return str(
        event.get("steam_id")
        or (event.get("player") or {}).get("steam_id")
        or payload.get("steam_id")
        or ""
    ).strip()

def _extract_username(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return str(
        event.get("username")
        or (event.get("player") or {}).get("username")
        or payload.get("username")
        or ""
    ).strip()

def _extract_reason(event: dict[str, Any]) -> str:
    payload = _payload(event)
    return str(
        payload.get("reason")
        or event.get("reason")
        or ""
    ).strip()

def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None

def _extract_session_seconds_from_payload(event: dict[str, Any]) -> float | None:
    payload = _payload(event)

    # Prioriza campos já em segundos
    for key in ("session_seconds", "duration_seconds", "played_seconds", "online_seconds"):
        v = _to_float(payload.get(key))
        if v is not None and v > 0:
            return v

    # Fallback para campos em ms
    for key in ("session_ms", "duration_ms", "played_ms", "online_ms"):
        v = _to_float(payload.get(key))
        if v is not None and v > 0:
            return v / 1000.0

    return None

def _normalize_session_seconds(value: Any) -> float | None:
    v = _to_float(value)
    if v is None or v <= 0:
        return None

    # Se vier muito grande, provavelmente está em milissegundos
    if v >= 86400:
        return v / 1000.0

    return v 

def _normalize_event(raw_event: dict[str, Any], source_line: int | None = None) -> dict[str, Any]:
    raw_event = raw_event or {}

    event_id = str(raw_event.get("event_id") or "").strip()
    event_type = str(raw_event.get("event_type") or "").strip().lower()

    payload = raw_event.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    player_block = raw_event.get("player")
    if not isinstance(player_block, dict):
        player_block = {}

    if not payload.get("steam_id") and player_block.get("steam_id"):
        payload["steam_id"] = player_block.get("steam_id")

    if not payload.get("username") and player_block.get("username"):
        payload["username"] = player_block.get("username")

    if not payload.get("display_name") and player_block.get("display_name"):
        payload["display_name"] = player_block.get("display_name")

    if not payload.get("character_name") and player_block.get("character_name"):
        payload["character_name"] = player_block.get("character_name")

    if payload.get("online_id") is None and player_block.get("online_id") is not None:
        payload["online_id"] = player_block.get("online_id")

    if payload.get("player_id") is None and player_block.get("player_id") is not None:
        payload["player_id"] = player_block.get("player_id")

    ts = raw_event.get("ts")
    if ts is None:
        ts = raw_event.get("timestamp")
    if ts is None:
        ts = raw_event.get("server_timestamp")
    if ts is None:
        ts = payload.get("ts")

    source = raw_event.get("source") or payload.get("source") or "doomtelemetry_mod"
    server_id = (
        raw_event.get("server_id")
        or payload.get("server_id")
        or raw_event.get("server_name")
        or payload.get("server_name")
        or "main"
    )

    return {
        "event_id": event_id,
        "event_type": event_type,
        "ts": ts,
        "server_id": server_id,
        "schema_version": raw_event.get("schema_version"),
        "source": source,
        "mod_version": raw_event.get("mod_version"),
        "timestamp": raw_event.get("timestamp"),
        "server_timestamp": raw_event.get("server_timestamp"),
        "payload": payload,
        "meta": {
            "source": source,
            "line": source_line,
        },
        "_raw": raw_event,
    }

# Adiciona esta variável "cadeado" fora da função (pode ser logo acima dela)
_entregas_em_andamento = set()

async def _processar_fila_de_loot(steam_id: str, username: str):
    """Lê a fila de espera do jogador e entrega via RCON assim que ele loga."""
    if not steam_id or not username: return
    
    # 🔒 O CADEADO: Se já estivermos a entregar itens a este jogador, ignora os eventos duplicados do jogo!
    if steam_id in _entregas_em_andamento:
        return
        
    _entregas_em_andamento.add(steam_id)
    
    try:
        print(f"[Loot] O jogador {username} conectou! Aguardando 15 segundos para carregar o mapa...")
        
        # O SEGREDO: Esperar ANTES de ir procurar os itens na base de dados!
        await asyncio.sleep(15) 
        
        # Só agora é que vamos ver o que há na fila
        pendentes = await db.get_pending_loot_rewards(steam_id)
        if not pendentes:
            return 
            
        print(f"[Loot] Tentando entregar {len(pendentes)} lotes de itens pendentes para {username}...")
        
        for item in pendentes:
            request_id = item["request_id"]
            item_id = item["item_id"]
            qtd = item["quantity"]
            
            comando = f'additem "{username}" "{item_id}"'
            sucesso = True
            
            # Envia o comando as vezes necessárias conforme a quantidade
            for _ in range(qtd):
                resposta = await enviar_comando_rcon(comando)
                if "not found" in resposta.lower() or "error" in resposta.lower() or resposta == "ERROR":
                    sucesso = False
                    break
                    
            if sucesso:
                await db.mark_pending_loot_delivered(request_id)
                print(f"[Loot] Entregue {qtd}x {item_id} com sucesso para {username}!")
            else:
                print(f"[Loot] RCON falhou ao entregar item {item_id} para {username}. Mantido na fila de segurança.")
    finally:
        # 🔓 Tira o cadeado no final para permitir futuras entregas noutros logins
        _entregas_em_andamento.discard(steam_id)
async def _consume_link_status_request_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    pz_reported_steam_id = str(payload.get("steam_id") or "").strip()
    server_id = str(event.get("server_id") or payload.get("server_id") or "").strip() or None

    status = await get_active_player_link_status_by_pz_steam_id(
        pz_reported_steam_id=pz_reported_steam_id,
        server_id=server_id,
    )

    return {
        "ok": True,
        "linked": bool(status.get("linked", False)),
        "discord_id": status.get("discord_id"),
        "message": str(status.get("message") or ""),
        "code": status.get("link_code"),
    }


async def _consume_link_code_submit_bridge_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    pz_reported_steam_id = str(payload.get("steam_id") or "").strip()
    server_id = str(event.get("server_id") or payload.get("server_id") or "").strip() or None

    result = await consume_link_code_submit_event(event)

    if pz_reported_steam_id:
        await sync_player_identity_link_status(
            pz_reported_steam_id=pz_reported_steam_id,
            server_id=server_id,
        )

    return {
        "ok": bool(result.get("ok", False)),
        "linked": bool(result.get("linked", False)),
        "discord_id": result.get("discord_id"),
        "message": str(result.get("message") or ""),
        "code": result.get("code") or str(payload.get("code") or "").strip() or None,
    }


async def _consume_link_unlink_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    pz_reported_steam_id = str(payload.get("steam_id") or "").strip()
    server_id = str(event.get("server_id") or payload.get("server_id") or "").strip() or None

    result = await deactivate_player_link_by_steam_id(
        pz_reported_steam_id=pz_reported_steam_id,
        server_id=server_id,
    )

    if pz_reported_steam_id:
        await sync_player_identity_link_status(
            pz_reported_steam_id=pz_reported_steam_id,
            server_id=server_id,
        )

    return {
        "ok": bool(result.get("ok", False)),
        "linked": False,
        "discord_id": result.get("discord_id"),
        "message": str(result.get("message") or "Conta desvinculada com sucesso."),
        "code": None,
    }


async def _apply_event_projections(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "").strip().lower()

    if event_type == "server_boot":
        await set_config("last_pulse_unix", str(int(time.time())))
        await update_server_state_from_heartbeat(event)
        return None

    if event_type == "server_heartbeat":
        await set_config("last_pulse_unix", str(int(time.time())))
        await update_server_state_from_heartbeat(event)
        return None

    if event_type == "link_code_submit":
        return await _consume_link_code_submit_bridge_event(event)

    if event_type == "link_status_request":
        return await _consume_link_status_request_event(event)

    if event_type == "link_unlink":
        return await _consume_link_unlink_event(event)

    if event_type == "player_identity":
        await upsert_player_identity_from_event(event)
        return None

    if event_type == "player_profile":
        await upsert_player_profile_from_event(event)
        await upsert_player_identity_from_event(event)
        await apply_profile_progress_to_lifetime_stats(event)
        return None

    if event_type == "player_state":
        await upsert_player_state_from_event(event)
        await upsert_player_identity_from_event(event)
        return None

    if event_type == "player_kill_delta":
        await upsert_player_identity_from_event(event)
        await upsert_player_lifetime_stats_from_kill_delta(event)

        payload = event.get("payload") or {}
        steam_id = str(
            event.get("steam_id")
            or (event.get("player") or {}).get("steam_id")
            or payload.get("steam_id")
            or ""
        ).strip()

        zombie_delta = int(payload.get("zombie_kills_delta") or 0)
        survivor_delta = int(payload.get("survivor_kills_delta") or 0)

        if steam_id and (zombie_delta > 0 or survivor_delta > 0):
            from utils.db import get_player_identity_link_status, add_discord_balance

            status = await get_player_identity_link_status(steam_id)
            discord_id = status.get("discord_id")

            if status.get("linked") and discord_id:
                coins_zombies = zombie_delta * 100
                coins_survivors = survivor_delta * 300
                coins_total = coins_zombies + coins_survivors

                novo_saldo = await add_discord_balance(discord_id, coins_total)
                print(
                    f"[ECONOMIA] +{coins_total} coins para ID {discord_id} "
                    f"(Z: {zombie_delta}, S: {survivor_delta}). Saldo: {novo_saldo}"
                )
        return None

    if event_type == "player_session_start":
        await upsert_player_identity_from_event(event)
        await open_player_session_from_event(event)
        
        # --- LÓGICA DE ENTREGA DE LOOT PENDENTE ---
        payload = event.get("payload") or {}
        steam_id = str(
            event.get("steam_id")
            or (event.get("player") or {}).get("steam_id")
            or payload.get("steam_id")
            or ""
        ).strip()
        username = str(
            event.get("username")
            or (event.get("player") or {}).get("username")
            or payload.get("username")
            or ""
        ).strip()
        
        if steam_id and username:
            # Usamos create_task para o bot não travar a leitura de eventos enquanto envia os itens
            asyncio.create_task(_processar_fila_de_loot(steam_id, username))
        # ------------------------------------------
        
        return None

    if event_type == "player_session_end":
        print("[SESSION_END] evento recebido")

        await upsert_player_identity_from_event(event)

        session_seconds_db = await close_player_session_from_event(event)
        session_seconds_payload = _extract_session_seconds_from_payload(event)

        steam_id = _extract_steam_id(event)
        username = _extract_username(event)
        reason = _extract_reason(event)

        session_seconds = _normalize_session_seconds(session_seconds_db)

        # fallback: se o close da sessão não devolveu duração válida, tenta usar o payload
        if not session_seconds:
            session_seconds = _normalize_session_seconds(session_seconds_payload)

        print(
            f"[SESSION_END] username={username!r} steam_id={steam_id!r} "
            f"reason={reason!r} session_seconds_db={session_seconds_db!r} "
            f"session_seconds_payload={session_seconds_payload!r} "
            f"session_seconds_final={session_seconds!r}"
        )

        if not steam_id:
            print("[SESSION_END] steam_id vazio -> nao foi possivel checar vinculo/pagar coins")
            return None

        if not session_seconds:
            print("[SESSION_END] session_seconds vazio/None -> sessao nao fechou como esperado e payload nao ajudou")
            return None

        if session_seconds < 60:
            print(f"[SESSION_END] sessao muito curta ({session_seconds}s) -> sem coins")
            return None

        from utils.db import get_player_identity_link_status, add_discord_balance

        status = await get_player_identity_link_status(steam_id)
        discord_id = status.get("discord_id")
        linked = status.get("linked")

        print(
            f"[SESSION_END] status_vinculo linked={linked!r} "
            f"discord_id={discord_id!r}"
        )

        if not linked or not discord_id:
            print("[SESSION_END] jogador nao vinculado -> sem pagamento de coins")
            return None

        minutos_jogados = int(session_seconds // 60)
        if minutos_jogados <= 0:
            print(f"[SESSION_END] minutos_jogados zerado -> sem pagamento")
            return None

        coins_tempo = minutos_jogados * 10

        print(
            f"[SESSION_END] minutos_jogados={minutos_jogados} "
            f"coins_tempo={coins_tempo}"
        )

        novo_saldo = await add_discord_balance(discord_id, coins_tempo)
        print(
            f"[ECONOMIA] +{coins_tempo} coins para ID {discord_id} "
            f"por {minutos_jogados} mins online. Saldo: {novo_saldo}"
        )
        return None

    if event_type == "player_death":
        await upsert_player_identity_from_event(event)
        await insert_player_death_from_event(event)
        return None

    if event_type == "faction_snapshot":
        await upsert_player_identity_from_event(event)
        await upsert_faction_state_from_event(event)
        return None

    return None

async def _post_link_result_callback(event: dict[str, Any], link_result: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    body = {
        "event_id": str(event.get("event_id") or "").strip() or None,
        "discord_id": link_result.get("discord_id"),
        "steam_id": str(payload.get("steam_id") or "").strip() or None,
        "code": link_result.get("code") or str(payload.get("code") or "").strip() or None,
        "linked": bool(link_result.get("linked", False)),
        "message": str(link_result.get("message") or ""),
        "username": payload.get("username"),
        "display_name": payload.get("display_name"),
        "character_name": payload.get("character_name"),
        "server_id": event.get("server_id"),
        "extra": {
            "event_type": event.get("event_type"),
        },
    }

    print(f"[pz_bridge] POST /link/result body={body}")
    result = await pz_post_link_result(body)
    print(f"[pz_bridge] POST /link/result retorno={result}")
    return result


async def process_one_event(raw_event: dict[str, Any], source_line: int | None = None) -> dict[str, Any]:
    event = _normalize_event(raw_event, source_line=source_line)

    event_id = str(event.get("event_id") or "").strip()
    event_type = str(event.get("event_type") or "").strip().lower()

    if not event_id:
        return {
            "ok": False,
            "event_id": None,
            "event_type": event_type or None,
            "status": "error",
            "error": "event_id ausente",
        }

    if not event_type:
        return {
            "ok": False,
            "event_id": event_id,
            "event_type": None,
            "status": "error",
            "error": "event_type ausente",
        }

    try:
        inserted = await insert_raw_event_log(event)
    except Exception as exc:
        return {
            "ok": False,
            "event_id": event_id,
            "event_type": event_type,
            "status": "error",
            "error": f"Falha ao gravar event_log: {exc}",
        }

    async def _handle_projection_and_callback(inserted_flag: bool) -> dict[str, Any]:
        projection_result = await _apply_event_projections(event)

        if event_type in {"link_code_submit", "link_status_request", "link_unlink"} and isinstance(projection_result, dict):
            callback_warning = None
            try:
                print(f"[pz_bridge] {event_type} event_id={event_id} projection_result={projection_result}")
                callback_result = await _post_link_result_callback(event, projection_result)
                print(f"[pz_bridge] /link/result callback_result={callback_result}")
            except Exception as cb_exc:
                callback_warning = str(cb_exc)
                print(f"[pz_bridge] ERRO ao chamar /link/result event_id={event_id}: {repr(cb_exc)}")

            await mark_event_log_processed(event_id)

            if projection_result.get("ok"):
                return {
                    "ok": True,
                    "event_id": event_id,
                    "event_type": event_type,
                    "status": "processed",
                    "inserted": inserted_flag,
                    "linked": bool(projection_result.get("linked", False)),
                    "message": projection_result.get("message"),
                    "callback_warning": callback_warning,
                }

            return {
                "ok": True,
                "event_id": event_id,
                "event_type": event_type,
                "status": "processed_rejected_link_code",
                "inserted": inserted_flag,
                "linked": bool(projection_result.get("linked", False)),
                "message": projection_result.get("message"),
                "callback_warning": callback_warning,
            }

        await mark_event_log_processed(event_id)
        return {
            "ok": True,
            "event_id": event_id,
            "event_type": event_type,
            "status": "processed",
            "inserted": inserted_flag,
        }

    if inserted:
        try:
            return await _handle_projection_and_callback(True)
        except Exception as exc:
            await mark_event_log_error(event_id, str(exc))
            return {
                "ok": False,
                "event_id": event_id,
                "event_type": event_type,
                "status": "error",
                "inserted": True,
                "error": str(exc),
            }

    try:
        existing = await get_event_log_by_id(event_id)
    except Exception as exc:
        return {
            "ok": False,
            "event_id": event_id,
            "event_type": event_type,
            "status": "error",
            "inserted": False,
            "error": f"Falha ao consultar event_log existente: {exc}",
        }

    if existing:
        process_status = str(existing.get("process_status") or "").strip().lower()
        processed_at = existing.get("processed_at")

        if process_status == "processed" or processed_at:
            return {
                "ok": True,
                "event_id": event_id,
                "event_type": event_type,
                "status": "already_processed",
                "inserted": False,
            }

        try:
            return await _handle_projection_and_callback(False)
        except Exception as exc:
            await mark_event_log_error(event_id, str(exc))
            return {
                "ok": False,
                "event_id": event_id,
                "event_type": event_type,
                "status": "error",
                "inserted": False,
                "error": str(exc),
            }

    return {
        "ok": False,
        "event_id": event_id,
        "event_type": event_type,
        "status": "error",
        "inserted": False,
        "error": "Evento duplicado não encontrado no event_log após falha de insert",
    }