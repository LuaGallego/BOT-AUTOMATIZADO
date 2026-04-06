import asyncio
import json
import time
import aiosqlite
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Any
import uuid

# 👉 Variável global para o Bot saber se o servidor está vivo
LAST_PULSE = 0

DB_PATH = Path("doom.db")

_db: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default

async def _connect() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.execute("PRAGMA journal_mode = WAL;")
    await db.execute("PRAGMA synchronous = NORMAL;")
    await db.execute("PRAGMA temp_store = MEMORY;")
    await db.execute("PRAGMA busy_timeout = 5000;")
    return db

async def get_event_log_by_id(event_id: str) -> dict[str, Any] | None:
    event_id = str(event_id or "").strip()
    if not event_id:
        return None

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT
                event_id,
                event_type,
                ts,
                steam_id,
                payload_json,
                source,
                source_line,
                received_at,
                processed_at,
                process_status,
                process_error
            FROM event_log
            WHERE event_id = ?
            LIMIT 1
            """,
            (event_id,),
        )
        row = await cur.fetchone()

    if not row:
        return None

    try:
        payload = json.loads(row[4] or "{}")
    except Exception:
        payload = {}

    return {
        "event_id": row[0],
        "event_type": row[1],
        "ts": row[2],
        "steam_id": row[3],
        "payload": payload,
        "source": row[5],
        "source_line": row[6],
        "received_at": row[7],
        "processed_at": row[8],
        "process_status": row[9],
        "process_error": row[10],
    }

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await _connect()
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None

async def get_pending_loot_rewards(steam_id: str) -> list[dict[str, Any]]:
    """Busca os itens que estão na fila de espera para este jogador."""
    db = await get_db()
    cur = await db.execute(
        """
        SELECT request_id, item_id, quantity 
        FROM pending_loot_rewards 
        WHERE steam_id = ? AND status = 'pending'
        """,
        (steam_id,)
    )
    rows = await cur.fetchall()
    return [{"request_id": r[0], "item_id": r[1], "quantity": r[2]} for r in rows]

async def mark_pending_loot_delivered(request_id: str) -> None:
    """Marca o item como entregue para não ser duplicado depois."""
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            UPDATE pending_loot_rewards 
            SET status = 'delivered', delivered_at = CURRENT_TIMESTAMP 
            WHERE request_id = ?
            """,
            (request_id,)
        )
        await db.commit()

async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    row = await cur.fetchone()
    return row is not None


async def _ensure_columns(db: aiosqlite.Connection, table: str, columns_sql: list[str]) -> None:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    existing = {r[1] for r in rows}

    for coldef in columns_sql:
        colname = coldef.split()[0].strip()
        if colname not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")

async def execute(sql, parameters=None):
    """Executa comandos SQL (INSERT, UPDATE, DELETE)"""
    async with aiosqlite.connect("doom.db") as db: 
        try:
            if parameters:
                await db.execute(sql, parameters)
            else:
                await db.execute(sql)
            await db.commit()
        except Exception as e:
            print(f"[DB ERROR] Falha ao executar SQL: {e}")

# =========================================================
# MIGRATIONS
# =========================================================

async def _get_schema_version(db: aiosqlite.Connection) -> int:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER NOT NULL
        );
        """
    )
    cur = await db.execute("SELECT version FROM schema_migrations LIMIT 1;")
    row = await cur.fetchone()
    if row is None:
        await db.execute("INSERT INTO schema_migrations(version) VALUES (0);")
        await db.commit()
        return 0
    return int(row[0])


async def _set_schema_version(db: aiosqlite.Connection, version: int) -> None:
    await db.execute("UPDATE schema_migrations SET version=?;", (int(version),))
    await db.commit()


async def _migration_1_base_tables(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS whitelists (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          discord_id INTEGER NOT NULL,
          steam_id TEXT NOT NULL,
          ingame_name TEXT NOT NULL,
          status TEXT NOT NULL,
          ban_info TEXT,
          created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_whitelists_discord_id
        ON whitelists(discord_id);

        CREATE TABLE IF NOT EXISTS bot_config (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS warnings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          guild_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL,
          staff_id INTEGER NOT NULL,
          reason TEXT NOT NULL,
          evidence TEXT,
          points INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_warnings_user_id
        ON warnings(user_id);

        CREATE TABLE IF NOT EXISTS punishments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          guild_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL,
          staff_id INTEGER NOT NULL,
          type TEXT NOT NULL,
          reason TEXT NOT NULL,
          evidence TEXT,
          duration_seconds INTEGER,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_punishments_user_id
        ON punishments(user_id);

        CREATE INDEX IF NOT EXISTS idx_punishments_type
        ON punishments(type);

        CREATE TABLE IF NOT EXISTS staff_dashboard (
          guild_id   INTEGER PRIMARY KEY,
          channel_id INTEGER NOT NULL,
          message_id INTEGER NOT NULL
        );
        """
    )


async def _migration_2_revokes(db: aiosqlite.Connection) -> None:
    await _ensure_columns(db, "warnings", [
        "revoked_by INTEGER",
        "revoked_at TEXT",
        "revoked_reason TEXT",
    ])
    await _ensure_columns(db, "punishments", [
        "revoked_by INTEGER",
        "revoked_at TEXT",
        "revoked_reason TEXT",
    ])


async def _migration_3_whitelist_metrics(db: aiosqlite.Connection) -> None:
    await _ensure_columns(db, "whitelists", [
        "submitted_at TEXT",
        "decided_at TEXT",
    ])

    await db.execute(
        "UPDATE whitelists SET submitted_at = created_at WHERE submitted_at IS NULL;"
    )

    await db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_whitelists_status
        ON whitelists(status);

        CREATE INDEX IF NOT EXISTS idx_whitelists_status_submitted_at
        ON whitelists(status, submitted_at);
        """
    )


async def _migration_4_ticket_events(db: aiosqlite.Connection) -> None:
    if await _table_exists(db, "ticket_events"):
        cur = await db.execute("PRAGMA table_info(ticket_events);")
        rows = await cur.fetchall()
        cols = {r[1] for r in rows}
        if "thread_id" not in cols:
            await db.execute("DROP TABLE IF EXISTS ticket_events;")
            await db.commit()

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ticket_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          guild_id INTEGER NOT NULL,
          thread_id INTEGER NOT NULL,
          ticket_type TEXT NOT NULL,
          action TEXT NOT NULL,
          created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_ticket_events_thread_action
        ON ticket_events(thread_id, action);

        CREATE INDEX IF NOT EXISTS idx_ticket_events_guild_time
        ON ticket_events(guild_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_ticket_events_guild_type_action
        ON ticket_events(guild_id, ticket_type, action);
        """
    )


async def _migration_5_optimize(db: aiosqlite.Connection) -> None:
    await db.execute("PRAGMA optimize;")


async def _migration_6_legacy_pz_tables(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS player_profiles (
          steam_id TEXT PRIMARY KEY,
          username TEXT,
          current_character_name TEXT,
          current_profession TEXT,
          online INTEGER NOT NULL DEFAULT 0,
          is_alive INTEGER NOT NULL DEFAULT 1,
          x REAL,
          y REAL,
          z REAL,
          total_zombie_kills INTEGER NOT NULL DEFAULT 0,
          total_player_kills INTEGER NOT NULL DEFAULT 0,
          total_deaths INTEGER NOT NULL DEFAULT 0,
          total_survival_minutes INTEGER NOT NULL DEFAULT 0,
          current_run_minutes INTEGER NOT NULL DEFAULT 0,
          best_run_minutes INTEGER NOT NULL DEFAULT 0,
          first_seen_at TEXT,
          last_seen_at TEXT,
          last_login_at TEXT,
          last_logout_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_player_profiles_online
        ON player_profiles(online);

        CREATE INDEX IF NOT EXISTS idx_player_profiles_last_seen_at
        ON player_profiles(last_seen_at);

        CREATE TABLE IF NOT EXISTS game_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id TEXT NOT NULL UNIQUE,
          steam_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          processed_at TEXT,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_game_events_steam_id
        ON game_events(steam_id);

        CREATE INDEX IF NOT EXISTS idx_game_events_event_type
        ON game_events(event_type);

        CREATE INDEX IF NOT EXISTS idx_game_events_occurred_at
        ON game_events(occurred_at);

        CREATE TABLE IF NOT EXISTS player_snapshots (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          steam_id TEXT NOT NULL,
          username TEXT,
          character_name TEXT,
          profession TEXT,
          online INTEGER NOT NULL DEFAULT 1,
          is_alive INTEGER NOT NULL DEFAULT 1,
          x REAL,
          y REAL,
          z REAL,
          zombie_kills_total INTEGER NOT NULL DEFAULT 0,
          player_kills_total INTEGER NOT NULL DEFAULT 0,
          deaths_total INTEGER NOT NULL DEFAULT 0,
          survival_minutes_total INTEGER NOT NULL DEFAULT 0,
          current_run_minutes INTEGER NOT NULL DEFAULT 0,
          captured_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_player_snapshots_steam_id
        ON player_snapshots(steam_id);

        CREATE INDEX IF NOT EXISTS idx_player_snapshots_captured_at
        ON player_snapshots(captured_at);

        CREATE TABLE IF NOT EXISTS sync_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          action TEXT NOT NULL,
          status TEXT NOT NULL,
          details TEXT,
          created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sync_logs_source
        ON sync_logs(source);

        CREATE INDEX IF NOT EXISTS idx_sync_logs_status
        ON sync_logs(status);

        CREATE INDEX IF NOT EXISTS idx_sync_logs_created_at
        ON sync_logs(created_at);
        """
    )


async def _migration_7_noop(db: aiosqlite.Connection) -> None:
    return


async def _migration_8_event_log_table(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS event_log (
          event_id TEXT PRIMARY KEY,
          event_type TEXT NOT NULL,
          ts REAL,
          steam_id TEXT,
          payload_json TEXT NOT NULL,
          source TEXT,
          source_line INTEGER,
          received_at TEXT NOT NULL,
          processed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_event_log_event_type
        ON event_log(event_type);

        CREATE INDEX IF NOT EXISTS idx_event_log_steam_id
        ON event_log(steam_id);

        CREATE INDEX IF NOT EXISTS idx_event_log_ts
        ON event_log(ts);

        CREATE INDEX IF NOT EXISTS idx_event_log_received_at
        ON event_log(received_at);
        """
    )


async def _migration_9_server_state(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS server_state (
            server_id TEXT PRIMARY KEY,
            online_count INTEGER DEFAULT 0,
            online_players_json TEXT NOT NULL DEFAULT '[]',
            game_time TEXT,
            world_age_days REAL,
            global_temperature REAL,
            is_game_paused INTEGER DEFAULT 0,
            night INTEGER DEFAULT 0,
            weather_json TEXT NOT NULL DEFAULT '{}',
            last_event_id TEXT,
            last_event_ts REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


async def _migration_10_player_state(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS player_state (
            steam_id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            character_name TEXT,
            online_id INTEGER,
            x REAL,
            y REAL,
            z REAL,
            is_alive INTEGER DEFAULT 1,
            hours_survived REAL,
            zombie_kills INTEGER DEFAULT 0,
            survivor_kills INTEGER DEFAULT 0,
            last_event_id TEXT,
            last_event_ts REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


async def _migration_11_player_lifetime_stats(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS player_lifetime_stats (
            steam_id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            character_name TEXT,
            zombie_kills_total INTEGER DEFAULT 0,
            survivor_kills_total INTEGER DEFAULT 0,
            last_event_id TEXT,
            last_event_ts REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


async def _migration_12_player_identity(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS player_identity (
            steam_id TEXT PRIMARY KEY,
            discord_id INTEGER,
            username TEXT,
            display_name TEXT,
            character_name TEXT,
            last_whitelist_name TEXT,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_event_id TEXT,
            last_event_ts REAL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

async def _migration_14_player_death_log(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS player_death_log (
            death_id TEXT PRIMARY KEY,
            steam_id TEXT NOT NULL,
            discord_id INTEGER,
            username TEXT,
            display_name TEXT,
            character_name TEXT,
            x REAL,
            y REAL,
            z REAL,
            hours_survived REAL,
            zombie_kills INTEGER NOT NULL DEFAULT 0,
            survivor_kills INTEGER NOT NULL DEFAULT 0,
            cause TEXT,
            event_ts REAL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_player_death_log_steam_id
        ON player_death_log(steam_id);

        CREATE INDEX IF NOT EXISTS idx_player_death_log_discord_id
        ON player_death_log(discord_id);

        CREATE INDEX IF NOT EXISTS idx_player_death_log_event_ts
        ON player_death_log(event_ts);
        """
    )


async def _migration_15_player_sessions(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS player_sessions_open (
            steam_id TEXT PRIMARY KEY,
            discord_id INTEGER,
            username TEXT,
            display_name TEXT,
            character_name TEXT,
            started_event_id TEXT,
            started_ts REAL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS player_sessions_log (
            session_id TEXT PRIMARY KEY,
            steam_id TEXT NOT NULL,
            discord_id INTEGER,
            username TEXT,
            display_name TEXT,
            character_name TEXT,
            started_ts REAL,
            ended_ts REAL,
            session_seconds REAL,
            start_event_id TEXT,
            end_event_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_player_sessions_log_steam_id
        ON player_sessions_log(steam_id);

        CREATE INDEX IF NOT EXISTS idx_player_sessions_log_discord_id
        ON player_sessions_log(discord_id);

        CREATE INDEX IF NOT EXISTS idx_player_sessions_log_started_ts
        ON player_sessions_log(started_ts);
        """
    )


async def _migration_16_doomtelemetry_expansion(db: aiosqlite.Connection) -> None:
    await _ensure_columns(db, "event_log", [
        "process_status TEXT DEFAULT 'pending'",
        "process_error TEXT",
    ])

    await _ensure_columns(db, "server_state", [
        "online_details_json TEXT NOT NULL DEFAULT '[]'",
        "uptime_seconds REAL DEFAULT 0",
        "debug_enabled INTEGER DEFAULT 0",
        "mod_version TEXT",
        "server_time TEXT",
    ])

    await _ensure_columns(db, "player_state", [
        "inventory_weight REAL",
        "carry_capacity REAL",
        "is_in_vehicle INTEGER DEFAULT 0",
        "is_asleep INTEGER DEFAULT 0",
        "is_outdoors INTEGER DEFAULT 0",
        "building_name TEXT",
        "vehicle_id TEXT",
        "vehicle_script TEXT",
        "vehicle_speed REAL",
        "bleeding INTEGER DEFAULT 0",
        "overall_body_damage REAL",
    ])

    await _ensure_columns(db, "player_lifetime_stats", [
        "death_count INTEGER DEFAULT 0",
        "total_hours_survived REAL DEFAULT 0",
        "last_profile_hours_survived REAL DEFAULT 0",
    ])

    await _ensure_columns(db, "player_profiles", [
        "display_name TEXT",
        "online_id INTEGER",
        "hours_survived REAL",
        "zombie_kills INTEGER DEFAULT 0",
        "survivor_kills INTEGER DEFAULT 0",
        "traits_json TEXT NOT NULL DEFAULT '[]'",
        "traits_string TEXT",
        "perks_json TEXT NOT NULL DEFAULT '{}'",
        "inventory_weight REAL",
        "carry_capacity REAL",
        "is_in_vehicle INTEGER DEFAULT 0",
        "is_asleep INTEGER DEFAULT 0",
        "is_outdoors INTEGER DEFAULT 0",
        "building_name TEXT",
        "vehicle_id TEXT",
        "vehicle_script TEXT",
        "vehicle_speed REAL",
        "bleeding INTEGER DEFAULT 0",
        "overall_body_damage REAL",
        "faction_name TEXT",
        "faction_tag TEXT",
        "faction_owner TEXT",
        "faction_members_json TEXT NOT NULL DEFAULT '[]'",
        "last_event_id TEXT",
        "last_event_ts REAL",
        "updated_at TEXT DEFAULT CURRENT_TIMESTAMP",
    ])

    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS faction_state (
            steam_id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            character_name TEXT,
            faction_name TEXT,
            faction_tag TEXT,
            faction_owner TEXT,
            faction_members_json TEXT NOT NULL DEFAULT '[]',
            last_event_id TEXT,
            last_event_ts REAL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_faction_state_name
        ON faction_state(faction_name);
        """
    )


async def _migration_17_link_codes_and_player_links(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS link_codes (
            discord_id INTEGER PRIMARY KEY,
            link_code TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_sent_at TEXT,
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_link_codes_code
        ON link_codes(link_code);

        CREATE INDEX IF NOT EXISTS idx_link_codes_active
        ON link_codes(is_active);

        CREATE TABLE IF NOT EXISTS player_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id INTEGER NOT NULL,
            official_steam_id TEXT,
            pz_reported_steam_id TEXT NOT NULL,
            username TEXT,
            display_name TEXT,
            character_name TEXT,
            server_id TEXT,
            linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            linked_via TEXT NOT NULL DEFAULT 'link_code',
            is_active INTEGER NOT NULL DEFAULT 1
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_player_links_active_pz
        ON player_links(pz_reported_steam_id, server_id, is_active);

        CREATE INDEX IF NOT EXISTS idx_player_links_discord_id
        ON player_links(discord_id);

        CREATE INDEX IF NOT EXISTS idx_player_links_official_steam_id
        ON player_links(official_steam_id);

        CREATE INDEX IF NOT EXISTS idx_player_links_pz_reported_steam_id
        ON player_links(pz_reported_steam_id);
        """
    )

async def _migration_18_player_links_add_link_code(db: aiosqlite.Connection) -> None:
    await _ensure_columns(db, "player_links", [
        "link_code TEXT",
    ])

    await db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_player_links_link_code
        ON player_links(link_code);
        """
    )

async def _migration_19_player_identity_link_status(db: aiosqlite.Connection) -> None:
    await _ensure_columns(db, "player_identity", [
        "is_linked INTEGER NOT NULL DEFAULT 0",
        "active_link_code TEXT",
        "linked_at TEXT",
    ])

    await db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_player_identity_is_linked
        ON player_identity(is_linked);

        CREATE INDEX IF NOT EXISTS idx_player_identity_active_link_code
        ON player_identity(active_link_code);
        """
    )

    await db.execute(
        """
        UPDATE player_identity
           SET is_linked = 1,
               linked_at = COALESCE(
                   linked_at,
                   (
                       SELECT pl.linked_at
                         FROM player_links pl
                        WHERE pl.pz_reported_steam_id = player_identity.steam_id
                          AND pl.is_active = 1
                        ORDER BY pl.linked_at DESC, pl.id DESC
                        LIMIT 1
                   )
               ),
               active_link_code = COALESCE(
                   active_link_code,
                   (
                       SELECT pl.link_code
                         FROM player_links pl
                        WHERE pl.pz_reported_steam_id = player_identity.steam_id
                          AND pl.is_active = 1
                        ORDER BY pl.linked_at DESC, pl.id DESC
                        LIMIT 1
                   )
               ),
               discord_id = COALESCE(
                   discord_id,
                   (
                       SELECT pl.discord_id
                         FROM player_links pl
                        WHERE pl.pz_reported_steam_id = player_identity.steam_id
                          AND pl.is_active = 1
                        ORDER BY pl.linked_at DESC, pl.id DESC
                        LIMIT 1
                   )
               )
         WHERE EXISTS (
               SELECT 1
                 FROM player_links pl
                WHERE pl.pz_reported_steam_id = player_identity.steam_id
                  AND pl.is_active = 1
         )
        """
    )

async def _migration_20_discord_economy(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS discord_economy (
            discord_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

async def _migration_shop_and_cleanup(db: aiosqlite.Connection) -> None:
    # 1. Remover tabelas inúteis
    await db.execute("DROP TABLE IF EXISTS redeem_requests")
    await db.execute("DROP TABLE IF EXISTS deliveries")
    
    # 2. Criar tabela de Log de Resgates com Username do Jogo
    await db.execute("""
        CREATE TABLE IF NOT EXISTS shop_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id INTEGER,
            steam_id TEXT,
            game_username TEXT,
            item_name TEXT,
            item_price INTEGER,
            status TEXT, -- 'SUCCESS' ou 'FAILED'
            error_message TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 3. Zerar Tickets e Punições
    await db.execute("DELETE FROM warnings")
    await db.execute("DELETE FROM punishments")
    await db.execute("DROP TABLE IF EXISTS ticket_events")
    
    await db.commit()

async def _migration_21_whitelist_steam_index(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_whitelists_steam_id
        ON whitelists(steam_id);

        CREATE INDEX IF NOT EXISTS idx_whitelists_steam_status
        ON whitelists(steam_id, status);
        """
    )

async def _migration_22_offline_loot(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS offline_loot_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id INTEGER NOT NULL,
            claim_date TEXT NOT NULL,
            claim_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(discord_id, claim_date)
        );

        CREATE TABLE IF NOT EXISTS pending_loot_rewards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            discord_id INTEGER NOT NULL,
            steam_id TEXT NOT NULL,
            reward_type TEXT NOT NULL,
            item_id TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            fail_reason TEXT
        );
        """
    )

async def _migration_23_expedition_loot_pool(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS expedition_loot_pool (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            name TEXT NOT NULL,
            emoji TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            rarity_weight INTEGER NOT NULL DEFAULT 10
        );
        """
    )
    
    # Vamos inserir alguns itens iniciais de exemplo apenas se a tabela estiver vazia
    cur = await db.execute("SELECT COUNT(*) FROM expedition_loot_pool")
    row = await cur.fetchone()
    
    if row and row[0] == 0:
        await db.executescript(
            """
            INSERT INTO expedition_loot_pool (item_id, name, emoji, quantity, rarity_weight) VALUES
            ('Base.CannedSoup', 'Sopa Enlatada', '🥫', 2, 60),
            ('Base.Bandage', 'Bandagem Esterilizada', '🩹', 3, 60),
            ('Base.Bullets9mm', 'Munição 9mm', '🔫', 15, 25),
            ('Base.Axe', 'Machado de Bombeiro', '🪓', 1, 10),
            ('Base.Katana', 'Katana', '⚔️', 1, 5);
            """
        )

import uuid

# =========================================================
# SISTEMA DE LOOTBOXES
# =========================================================

async def _migration_24_lootboxes(db: aiosqlite.Connection) -> None:
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_lootboxes (
            discord_id INTEGER PRIMARY KEY,
            quantity INTEGER NOT NULL DEFAULT 0
        );
        """
    )
# Adiciona o '_migration_24_lootboxes' na lista _MIGRATIONS no db.py!

async def add_user_lootbox(discord_id: int, quantity: int) -> None:
    """Adiciona lootboxes à conta do jogador."""
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO user_lootboxes (discord_id, quantity)
            VALUES (?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET 
                quantity = quantity + excluded.quantity
            """, 
            (int(discord_id), int(quantity))
        )
        await db.commit()

async def get_user_lootbox_count(discord_id: int) -> int:
    """Verifica quantas lootboxes o jogador tem por abrir."""
    db = await get_db()
    cur = await db.execute("SELECT quantity FROM user_lootboxes WHERE discord_id = ?", (int(discord_id),))
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def consume_user_lootbox(discord_id: int) -> bool:
    """Consome 1 lootbox. Retorna True se sucesso, False se não tiver saldo."""
    db = await get_db()
    async with _db_lock:
        cur = await db.execute("SELECT quantity FROM user_lootboxes WHERE discord_id = ?", (int(discord_id),))
        row = await cur.fetchone()
        if not row or int(row[0]) <= 0:
            return False # Não tem caixas
            
        await db.execute("UPDATE user_lootboxes SET quantity = quantity - 1 WHERE discord_id = ?", (int(discord_id),))
        await db.commit()
        return True
    
# ATUALIZE A LISTA _MIGRATIONS QUE FICA LOGO ABAIXO:
_MIGRATIONS = [
    _migration_1_base_tables,
    _migration_2_revokes,
    _migration_3_whitelist_metrics,
    _migration_4_ticket_events,
    _migration_5_optimize,
    _migration_6_legacy_pz_tables,
    _migration_7_noop,
    _migration_8_event_log_table,
    _migration_9_server_state,
    _migration_10_player_state,
    _migration_11_player_lifetime_stats,
    _migration_12_player_identity,
    _migration_14_player_death_log,
    _migration_15_player_sessions,
    _migration_16_doomtelemetry_expansion,
    _migration_17_link_codes_and_player_links,
    _migration_18_player_links_add_link_code,
    _migration_19_player_identity_link_status,
    _migration_20_discord_economy, 
    _migration_shop_and_cleanup,
    _migration_21_whitelist_steam_index,
    _migration_22_offline_loot,
    _migration_23_expedition_loot_pool,
    _migration_24_lootboxes,
]

async def init_db() -> None:
    db = await get_db()
    async with _db_lock:
        v = await _get_schema_version(db)
        target = len(_MIGRATIONS)

        while v < target:
            await _MIGRATIONS[v](db)
            v += 1
            await _set_schema_version(db, v)

        await db.commit()
    
    # ADICIONE ESTA LINHA AQUI:
    await fix_player_links_index()


# =========================================================
# LOOT OFFLINE & EXPEDIÇÃO
# =========================================================

async def get_expedition_items() -> list[dict[str, Any]]:
    """Busca todos os itens da pool de expedição com os seus pesos."""
    db = await get_db()
    cur = await db.execute("SELECT item_id, name, emoji, quantity, rarity_weight FROM expedition_loot_pool")
    rows = await cur.fetchall()
    
    return [
        {
            "item_id": r[0], 
            "name": r[1], 
            "emoji": r[2], 
            "quantity": r[3], 
            "rarity_weight": r[4]
        }
        for r in rows
    ]

async def get_today_loot_claim_count(discord_id: int) -> int:
    """Verifica quantas vezes o jogador já resgatou loot hoje."""
    hoje = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    db = await get_db()
    cur = await db.execute(
        "SELECT claim_count FROM offline_loot_claims WHERE discord_id = ? AND claim_date = ?", 
        (int(discord_id), hoje)
    )
    row = await cur.fetchone()
    return int(row[0]) if row else 0

async def consume_daily_loot_claim(discord_id: int) -> None:
    """Adiciona +1 na contagem de resgates de hoje do jogador."""
    hoje = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO offline_loot_claims (discord_id, claim_date, claim_count, updated_at)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(discord_id, claim_date) DO UPDATE SET
                claim_count = claim_count + 1, 
                updated_at = CURRENT_TIMESTAMP
            """, 
            (int(discord_id), hoje)
        )
        await db.commit()

async def create_pending_loot_reward(request_id: str, discord_id: int, steam_id: str, reward_type: str, item_id: str, quantity: int, source: str) -> None:
    """Guarda o item na fila de espera para quando o jogador entrar."""
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO pending_loot_rewards (
                request_id, discord_id, steam_id, reward_type, 
                item_id, quantity, source, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)
            """, 
            (request_id, int(discord_id), steam_id, reward_type, item_id, int(quantity), source)
        )
        await db.commit()

# =========================================================
# RAW EVENT LOG (FONTE DA VERDADE DA TELEMETRIA NO BOT)
# =========================================================

async def insert_raw_event_log(event: dict[str, Any]) -> bool:
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("event_id ausente")

    event_type = str(event.get("event_type") or "unknown").strip()
    ts = event.get("ts")

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    meta = event.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}

    steam_id = payload.get("steam_id")
    source = meta.get("source")
    source_line = meta.get("line")

    now = _utc_now_iso()
    db = await get_db()
    async with _db_lock:
        try:
            await db.execute(
                """
                INSERT INTO event_log (
                    event_id,
                    event_type,
                    ts,
                    steam_id,
                    payload_json,
                    source,
                    source_line,
                    received_at,
                    processed_at,
                    process_status,
                    process_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    ts,
                    steam_id,
                    json.dumps(payload, ensure_ascii=False),
                    source,
                    source_line,
                    now,
                    None,
                    "pending",
                    None,
                ),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def mark_event_log_processed(event_id: str) -> None:
    event_id = str(event_id or "").strip()
    if not event_id:
        return

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            UPDATE event_log
               SET processed_at = CURRENT_TIMESTAMP,
                   process_status = 'processed',
                   process_error = NULL
             WHERE event_id = ?
            """,
            (event_id,),
        )
        await db.commit()


async def mark_event_log_error(event_id: str, error_message: str) -> None:
    event_id = str(event_id or "").strip()
    if not event_id:
        return

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            UPDATE event_log
               SET processed_at = CURRENT_TIMESTAMP,
                   process_status = 'error',
                   process_error = ?
             WHERE event_id = ?
            """,
            ((error_message or "")[:1000], event_id),
        )
        await db.commit()


# =========================================================
# TELEMETRY PROJECTIONS
# =========================================================

# --- FUNÇÃO DO SERVIDOR ATUALIZADA ---
async def update_server_state_from_heartbeat(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    event_id = str(event.get("event_id") or "").strip() or None
    ts = event.get("ts")
    server_id = str(event.get("server_id") or payload.get("server_id") or "main").strip() or "main"

    # Extração de dados do mundo Zomboid
    game_time = payload.get("game_time")          # Ex: "6.8.1993 05:27"
    temp = _safe_float(payload.get("global_temperature"))
    is_paused = 1 if payload.get("is_game_paused") else 0
    weather_data = payload.get("weather") or {}
    
    # Transforma o clima em texto simples ou JSON
    weather_json = json.dumps(weather_data, ensure_ascii=False)
    now_unix = int(time.time())

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO server_state (
                server_id, online_count, game_time, world_age_days,
                global_temperature, is_game_paused, weather_json,
                last_event_id, last_event_ts, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id) DO UPDATE SET
                online_count = excluded.online_count,
                game_time = excluded.game_time,
                world_age_days = excluded.world_age_days,
                global_temperature = excluded.global_temperature,
                is_game_paused = excluded.is_game_paused,
                weather_json = excluded.weather_json,
                last_event_id = excluded.last_event_id,
                last_event_ts = excluded.last_event_ts,
                updated_at = excluded.updated_at
            """,
            (
                server_id, _safe_int(payload.get("online_count"), 0),
                game_time, _safe_float(payload.get("world_age_days")),
                temp, is_paused, weather_json,
                event_id, ts, now_unix
            ),
        )
        await db.commit()
async def upsert_player_lifetime_stats_from_kill_delta(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return

    zombie_delta = max(0, _safe_int(payload.get("zombie_kills_delta"), 0) or 0)
    survivor_delta = max(0, _safe_int(payload.get("survivor_kills_delta"), 0) or 0)
    
    # 👉 [CORREÇÃO 2.1] Pega os totais exatos
    current_z = _safe_int(payload.get("zombie_kills_total"), 0) or 0
    current_s = _safe_int(payload.get("survivor_kills_total"), 0) or 0

    event_id = str(event.get("event_id") or "").strip() or None
    ts = event.get("ts")

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT
                steam_id, username, display_name, character_name,
                zombie_kills_total, survivor_kills_total, death_count,
                total_hours_survived, last_profile_hours_survived,
                last_event_id, last_event_ts
            FROM player_lifetime_stats
            WHERE steam_id = ?
            LIMIT 1
            """,
            (steam_id,),
        )
        row = await cur.fetchone()

        if row is not None:
            last_event_id = str(row[9] or "").strip()
            if event_id and last_event_id == event_id:
                return

        if row is None:
            await db.execute(
                """
                INSERT INTO player_lifetime_stats (
                    steam_id, username, display_name, character_name,
                    zombie_kills_total, survivor_kills_total, death_count,
                    total_hours_survived, last_profile_hours_survived,
                    last_event_id, last_event_ts, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, CURRENT_TIMESTAMP)
                """,
                (steam_id, payload.get("username"), payload.get("display_name"), payload.get("character_name"), zombie_delta, survivor_delta, event_id, ts),
            )
        else:
            new_zombie_total = int(row[4] or 0) + zombie_delta
            new_survivor_total = int(row[5] or 0) + survivor_delta

            await db.execute(
                """
                UPDATE player_lifetime_stats
                SET username = COALESCE(?, username), display_name = COALESCE(?, display_name),
                    character_name = COALESCE(?, character_name), zombie_kills_total = ?,
                    survivor_kills_total = ?, last_event_id = ?, last_event_ts = ?, updated_at = CURRENT_TIMESTAMP
                WHERE steam_id = ?
                """,
                (payload.get("username"), payload.get("display_name"), payload.get("character_name"), new_zombie_total, new_survivor_total, event_id, ts, steam_id),
            )

        # 👉 [CORREÇÃO 2.2] Atualiza a vitrine de profiles para o Discord ler na hora
        if current_z > 0 or current_s > 0:
            await db.execute(
                "UPDATE player_profiles SET zombie_kills = ?, survivor_kills = ? WHERE steam_id = ?",
                (current_z, current_s, steam_id)
            )

        await db.commit()


async def get_whitelist_by_steam_id(steam_id: str) -> dict[str, Any] | None:
    steam_id = str(steam_id or "").strip()
    if not steam_id:
        return None

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'table'
               AND name = 'whitelists'
             LIMIT 1
            """
        )
        exists = await cur.fetchone()
        if not exists:
            return None

        cur = await db.execute("PRAGMA table_info(whitelists)")
        cols_rows = await cur.fetchall()
        cols = {row[1] for row in cols_rows}

        if "steam_id" not in cols:
            return None

        select_parts = []

        if "discord_id" in cols:
            select_parts.append("discord_id")
        else:
            select_parts.append("NULL AS discord_id")

        select_parts.append("steam_id")

        if "ingame_name" in cols:
            select_parts.append("ingame_name")
        elif "username" in cols:
            select_parts.append("username AS ingame_name")
        elif "display_name" in cols:
            select_parts.append("display_name AS ingame_name")
        else:
            select_parts.append("NULL AS ingame_name")

        if "status" in cols:
            select_parts.append("status")
        else:
            select_parts.append("NULL AS status")

        if "notes" in cols:
            select_parts.append("notes")
        else:
            select_parts.append("NULL AS notes")

        if "created_at" in cols:
            select_parts.append("created_at")
        else:
            select_parts.append("NULL AS created_at")

        where_parts = ["steam_id = ?"]
        if "status" in cols:
            where_parts.append(
                "LOWER(TRIM(COALESCE(status, ''))) IN ('aprovado', 'approved', 'ativo', 'active', 'whitelisted')"
            )

        order_parts = []
        if "decided_at" in cols:
            order_parts.append("COALESCE(decided_at, '') DESC")
        if "submitted_at" in cols:
            order_parts.append("COALESCE(submitted_at, '') DESC")
        if "created_at" in cols:
            order_parts.append("COALESCE(created_at, '') DESC")
        if "id" in cols:
            order_parts.append("id DESC")

        order_clause = ", ".join(order_parts) if order_parts else "steam_id DESC"

        query = f"""
            SELECT {", ".join(select_parts)}
              FROM whitelists
             WHERE {' AND '.join(where_parts)}
             ORDER BY {order_clause}
             LIMIT 1
        """

        cur = await db.execute(query, (steam_id,))
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "discord_id": row[0],
        "steam_id": row[1],
        "ingame_name": row[2],
        "status": row[3],
        "notes": row[4],
        "created_at": row[5],
    }

async def get_active_whitelist_conflict_by_steam_id(steam_id: str, discord_id: int) -> dict[str, Any] | None:
    steam_id = str(steam_id or "").strip()
    if not steam_id:
        return None

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT
                id,
                discord_id,
                steam_id,
                ingame_name,
                status,
                ban_info,
                created_at
            FROM whitelists
            WHERE steam_id = ?
              AND discord_id != ?
              AND LOWER(TRIM(COALESCE(status, ''))) IN ('pendente', 'aprovado')
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (steam_id, int(discord_id)),
        )
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "id": row[0],
        "discord_id": row[1],
        "steam_id": row[2],
        "ingame_name": row[3],
        "status": row[4],
        "ban_info": row[5],
        "created_at": row[6],
    }


async def reject_whitelist_by_discord_id(discord_id: int, reason: str) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            UPDATE whitelists
               SET status = 'rejeitado',
                   ban_info = ?
             WHERE discord_id = ?
            """,
            (str(reason or "").strip(), int(discord_id)),
        )
        await db.commit()


async def transfer_whitelist_to_new_discord(
    *,
    old_discord_id: int,
    new_discord_id: int,
    steam_id: str,
    ingame_name: str,
    reason: str,
) -> None:
    steam_id = str(steam_id or "").strip()
    ingame_name = str(ingame_name or "").strip()
    reason = str(reason or "").strip()

    db = await get_db()
    async with _db_lock:
        # 1) desativa/rejeita a whitelist antiga que usava esse steam
        await db.execute(
            """
            UPDATE whitelists
               SET status = 'rejeitado',
                   ban_info = ?
             WHERE discord_id = ?
               AND steam_id = ?
               AND LOWER(TRIM(COALESCE(status, ''))) IN ('pendente', 'aprovado')
            """,
            (f"Transferido para outro Discord. {reason}", int(old_discord_id), steam_id),
        )

        # 2) vê se já existe registro para o novo discord
        cur = await db.execute(
            """
            SELECT id
            FROM whitelists
            WHERE discord_id = ?
            LIMIT 1
            """,
            (int(new_discord_id),),
        )
        existing = await cur.fetchone()

        if existing:
            await db.execute(
                """
                UPDATE whitelists
                   SET steam_id = ?,
                       ingame_name = ?,
                       status = 'aprovado',
                       ban_info = ?
                 WHERE discord_id = ?
                """,
                (
                    steam_id,
                    ingame_name,
                    f"Transferido manualmente do Discord {old_discord_id}. {reason}",
                    int(new_discord_id),
                ),
            )
        else:
            await db.execute(
                """
                INSERT INTO whitelists (
                    discord_id,
                    steam_id,
                    ingame_name,
                    status,
                    ban_info,
                    created_at
                ) VALUES (?, ?, ?, 'aprovado', ?, ?)
                """,
                (
                    int(new_discord_id),
                    steam_id,
                    ingame_name,
                    f"Transferido manualmente do Discord {old_discord_id}. {reason}",
                    _utc_now_iso(),
                ),
            )

        await db.commit()

async def upsert_player_identity_from_event(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return

    event_id = str(event.get("event_id") or "").strip() or None
    ts = event.get("ts")

    username = payload.get("username")
    display_name = payload.get("display_name")
    character_name = payload.get("character_name")

    # [CORREÇÃO APLICADA]: Procurar a whitelist ANTES de trancar o banco de dados (Evita o Deadlock)
    wl = await get_whitelist_by_steam_id(steam_id)

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT
                discord_id,
                link_code,
                linked_at
            FROM player_links
            WHERE pz_reported_steam_id = ?
              AND is_active = 1
            ORDER BY linked_at DESC, id DESC
            LIMIT 1
            """,
            (steam_id,),
        )
        link_row = await cur.fetchone()

        discord_id = None
        is_linked = 0
        active_link_code = None
        linked_at = None

        if link_row:
            discord_id = link_row[0]
            active_link_code = link_row[1]
            linked_at = link_row[2]
            is_linked = 1
        else:
            # Puxamos o discord_id da consulta segura que fizemos lá em cima
            discord_id = wl.get("discord_id") if wl else None

        await db.execute(
            """
            INSERT INTO player_identity (
                steam_id,
                discord_id,
                username,
                display_name,
                character_name,
                last_whitelist_name,
                is_linked,
                active_link_code,
                linked_at,
                first_seen_at,
                last_seen_at,
                last_event_id,
                last_event_ts,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(steam_id) DO UPDATE SET
                discord_id = COALESCE(excluded.discord_id, player_identity.discord_id),
                username = COALESCE(excluded.username, player_identity.username),
                display_name = COALESCE(excluded.display_name, player_identity.display_name),
                character_name = COALESCE(excluded.character_name, player_identity.character_name),
                is_linked = excluded.is_linked,
                active_link_code = excluded.active_link_code,
                linked_at = excluded.linked_at,
                last_seen_at = CURRENT_TIMESTAMP,
                last_event_id = COALESCE(excluded.last_event_id, player_identity.last_event_id),
                last_event_ts = COALESCE(excluded.last_event_ts, player_identity.last_event_ts),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                steam_id,
                discord_id,
                username,
                display_name,
                character_name,
                is_linked,
                active_link_code,
                linked_at,
                event_id,
                ts,
            ),
        )
        await db.commit()

async def upsert_player_profile_from_event(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return

    event_id = str(event.get("event_id") or "").strip() or None
    ts = event.get("ts")

    traits = payload.get("traits") or []
    perks = payload.get("perks") or {}
    faction = payload.get("faction") or {}

    if not isinstance(traits, list):
        traits = []
    if not isinstance(perks, dict):
        perks = {}
    if not isinstance(faction, dict):
        faction = {}

    faction_members = faction.get("members") or []
    if not isinstance(faction_members, list):
        faction_members = []

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO player_profiles (
                steam_id,
                username,
                display_name,
                current_character_name,
                current_profession,
                online,
                is_alive,
                x,
                y,
                z,
                total_zombie_kills,
                total_player_kills,
                total_deaths,
                total_survival_minutes,
                current_run_minutes,
                best_run_minutes,
                first_seen_at,
                last_seen_at,
                last_login_at,
                last_logout_at,
                online_id,
                hours_survived,
                zombie_kills,
                survivor_kills,
                traits_json,
                traits_string,
                perks_json,
                inventory_weight,
                carry_capacity,
                is_in_vehicle,
                is_asleep,
                is_outdoors,
                building_name,
                vehicle_id,
                vehicle_script,
                vehicle_speed,
                bleeding,
                overall_body_damage,
                faction_name,
                faction_tag,
                faction_owner,
                faction_members_json,
                last_event_id,
                last_event_ts,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL, NULL,
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(steam_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                current_character_name = excluded.current_character_name,
                current_profession = excluded.current_profession,
                online = excluded.online,
                is_alive = excluded.is_alive,
                x = excluded.x,
                y = excluded.y,
                z = excluded.z,
                last_seen_at = CURRENT_TIMESTAMP,
                online_id = excluded.online_id,
                hours_survived = excluded.hours_survived,
                zombie_kills = excluded.zombie_kills,
                survivor_kills = excluded.survivor_kills,
                traits_json = excluded.traits_json,
                traits_string = excluded.traits_string,
                perks_json = excluded.perks_json,
                inventory_weight = excluded.inventory_weight,
                carry_capacity = excluded.carry_capacity,
                is_in_vehicle = excluded.is_in_vehicle,
                is_asleep = excluded.is_asleep,
                is_outdoors = excluded.is_outdoors,
                building_name = excluded.building_name,
                vehicle_id = excluded.vehicle_id,
                vehicle_script = excluded.vehicle_script,
                vehicle_speed = excluded.vehicle_speed,
                bleeding = excluded.bleeding,
                overall_body_damage = excluded.overall_body_damage,
                faction_name = excluded.faction_name,
                faction_tag = excluded.faction_tag,
                faction_owner = excluded.faction_owner,
                faction_members_json = excluded.faction_members_json,
                last_event_id = excluded.last_event_id,
                last_event_ts = excluded.last_event_ts,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                steam_id,
                payload.get("username"),
                payload.get("display_name"),
                payload.get("character_name"),
                payload.get("profession"),
                1,
                1 if payload.get("is_alive", True) else 0,
                _safe_float(payload.get("x")),
                _safe_float(payload.get("y")),
                _safe_float(payload.get("z")),
                _safe_int(payload.get("online_id")),
                _safe_float(payload.get("hours_survived")),
                _safe_int(payload.get("zombie_kills"), 0) or 0,
                _safe_int(payload.get("survivor_kills"), 0) or 0,
                json.dumps(traits, ensure_ascii=False),
                payload.get("traits_string"),
                json.dumps(perks, ensure_ascii=False),
                _safe_float(payload.get("inventory_weight")),
                _safe_float(payload.get("carry_capacity")),
                1 if payload.get("is_in_vehicle") else 0,
                1 if payload.get("is_asleep") else 0,
                1 if payload.get("is_outdoors") else 0,
                payload.get("building_name"),
                payload.get("vehicle_id"),
                payload.get("vehicle_script"),
                _safe_float(payload.get("vehicle_speed")),
                1 if payload.get("bleeding") else 0,
                _safe_float(payload.get("overall_body_damage")),
                faction.get("name"),
                faction.get("tag"),
                faction.get("owner"),
                json.dumps(faction_members, ensure_ascii=False),
                event_id,
                ts,
            ),
        )
        await db.commit()


async def upsert_faction_state_from_event(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return

    members = payload.get("faction_members") or []
    if not isinstance(members, list):
        members = []

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO faction_state (
                steam_id,
                username,
                display_name,
                character_name,
                faction_name,
                faction_tag,
                faction_owner,
                faction_members_json,
                last_event_id,
                last_event_ts,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(steam_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                character_name = excluded.character_name,
                faction_name = excluded.faction_name,
                faction_tag = excluded.faction_tag,
                faction_owner = excluded.faction_owner,
                faction_members_json = excluded.faction_members_json,
                last_event_id = excluded.last_event_id,
                last_event_ts = excluded.last_event_ts,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                steam_id,
                payload.get("username"),
                payload.get("display_name"),
                payload.get("character_name"),
                payload.get("faction_name"),
                payload.get("faction_tag"),
                payload.get("faction_owner"),
                json.dumps(members, ensure_ascii=False),
                str(event.get("event_id") or "").strip() or None,
                event.get("ts"),
            ),
        )
        await db.commit()

async def apply_profile_progress_to_lifetime_stats(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return

    current_hours = _safe_float(payload.get("hours_survived"), 0.0) or 0.0
    event_id = str(event.get("event_id") or "").strip() or None
    ts = event.get("ts")

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT total_hours_survived, last_profile_hours_survived
              FROM player_lifetime_stats
             WHERE steam_id = ?
             LIMIT 1
            """,
            (steam_id,),
        )
        row = await cur.fetchone()

        total_hours = float(row[0] or 0.0) if row else 0.0
        last_profile_hours = float(row[1] or 0.0) if row else 0.0

        if current_hours >= last_profile_hours:
            total_hours += current_hours - last_profile_hours

        new_last_profile_hours = current_hours

        await db.execute(
            """
            INSERT INTO player_lifetime_stats (
                steam_id,
                username,
                display_name,
                character_name,
                zombie_kills_total,
                survivor_kills_total,
                death_count,
                total_hours_survived,
                last_profile_hours_survived,
                last_event_id,
                last_event_ts,
                updated_at
            ) VALUES (?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(steam_id) DO UPDATE SET
                username = COALESCE(excluded.username, player_lifetime_stats.username),
                display_name = COALESCE(excluded.display_name, player_lifetime_stats.display_name),
                character_name = COALESCE(excluded.character_name, player_lifetime_stats.character_name),
                total_hours_survived = excluded.total_hours_survived,
                last_profile_hours_survived = excluded.last_profile_hours_survived,
                last_event_id = excluded.last_event_id,
                last_event_ts = excluded.last_event_ts,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                steam_id,
                payload.get("username"),
                payload.get("display_name"),
                payload.get("character_name"),
                total_hours,
                new_last_profile_hours,
                event_id,
                ts,
            ),
        )
        await db.commit()

async def upsert_player_state_from_event(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return

    event_id = str(event.get("event_id") or "").strip() or None
    ts = event.get("ts")

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO player_state (
                steam_id,
                username,
                display_name,
                character_name,
                online_id,
                x,
                y,
                z,
                is_alive,
                hours_survived,
                zombie_kills,
                survivor_kills,
                inventory_weight,
                carry_capacity,
                is_in_vehicle,
                is_asleep,
                is_outdoors,
                building_name,
                vehicle_id,
                vehicle_script,
                vehicle_speed,
                bleeding,
                overall_body_damage,
                last_event_id,
                last_event_ts,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(steam_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                character_name = excluded.character_name,
                online_id = excluded.online_id,
                x = excluded.x,
                y = excluded.y,
                z = excluded.z,
                is_alive = excluded.is_alive,
                hours_survived = excluded.hours_survived,
                zombie_kills = excluded.zombie_kills,
                survivor_kills = excluded.survivor_kills,
                inventory_weight = excluded.inventory_weight,
                carry_capacity = excluded.carry_capacity,
                is_in_vehicle = excluded.is_in_vehicle,
                is_asleep = excluded.is_asleep,
                is_outdoors = excluded.is_outdoors,
                building_name = excluded.building_name,
                vehicle_id = excluded.vehicle_id,
                vehicle_script = excluded.vehicle_script,
                vehicle_speed = excluded.vehicle_speed,
                bleeding = excluded.bleeding,
                overall_body_damage = excluded.overall_body_damage,
                last_event_id = excluded.last_event_id,
                last_event_ts = excluded.last_event_ts,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                steam_id,
                payload.get("username"),
                payload.get("display_name"),
                payload.get("character_name"),
                _safe_int(payload.get("online_id")),
                _safe_float(payload.get("x")),
                _safe_float(payload.get("y")),
                _safe_float(payload.get("z")),
                1 if payload.get("is_alive", True) else 0,
                _safe_float(payload.get("hours_survived")),
                _safe_int(payload.get("zombie_kills"), 0) or 0,
                _safe_int(payload.get("survivor_kills"), 0) or 0,
                _safe_float(payload.get("inventory_weight")),
                _safe_float(payload.get("carry_capacity")),
                1 if payload.get("is_in_vehicle") else 0,
                1 if payload.get("is_asleep") else 0,
                1 if payload.get("is_outdoors") else 0,
                payload.get("building_name"),
                payload.get("vehicle_id"),
                payload.get("vehicle_script"),
                _safe_float(payload.get("vehicle_speed")),
                1 if payload.get("bleeding") else 0,
                _safe_float(payload.get("overall_body_damage")),
                event_id,
                ts,
            ),
        )

        await db.execute(
            """
            UPDATE player_profiles 
            SET is_alive = ?, hours_survived = ?, zombie_kills = ?, survivor_kills = ?, updated_at = CURRENT_TIMESTAMP
            WHERE steam_id = ?
            """,
            (
                1 if payload.get("is_alive", True) else 0,
                _safe_float(payload.get("hours_survived")),
                _safe_int(payload.get("zombie_kills"), 0) or 0,
                _safe_int(payload.get("survivor_kills"), 0) or 0,
                steam_id
            )
        )

        await db.commit()

async def increment_player_death_counter(
    steam_id: str,
    *,
    username: Any = None,
    display_name: Any = None,
    character_name: Any = None,
    event_id: Any = None,
    ts: Any = None,
) -> None:
    steam_id = str(steam_id or "").strip()
    if not steam_id:
        return

    event_id = str(event_id or "").strip() or None

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT
                steam_id,
                death_count,
                last_event_id
            FROM player_lifetime_stats
            WHERE steam_id = ?
            LIMIT 1
            """,
            (steam_id,),
        )
        row = await cur.fetchone()

        # Se já aplicou esse mesmo evento de morte, não incrementa de novo
        if row is not None:
            last_event_id = str(row[2] or "").strip()
            if event_id and last_event_id == event_id:
                return

        if row is None:
            await db.execute(
                """
                INSERT INTO player_lifetime_stats (
                    steam_id,
                    username,
                    display_name,
                    character_name,
                    zombie_kills_total,
                    survivor_kills_total,
                    death_count,
                    total_hours_survived,
                    last_profile_hours_survived,
                    last_event_id,
                    last_event_ts,
                    updated_at
                ) VALUES (?, ?, ?, ?, 0, 0, 1, 0, 0, ?, ?, CURRENT_TIMESTAMP)
                """,
                (steam_id, username, display_name, character_name, event_id, ts),
            )
        else:
            new_death_count = int(row[1] or 0) + 1

            await db.execute(
                """
                UPDATE player_lifetime_stats
                SET
                    username = COALESCE(?, username),
                    display_name = COALESCE(?, display_name),
                    character_name = COALESCE(?, character_name),
                    death_count = ?,
                    last_event_id = ?,
                    last_event_ts = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE steam_id = ?
                """,
                (
                    username,
                    display_name,
                    character_name,
                    new_death_count,
                    event_id,
                    ts,
                    steam_id,
                ),
            )

        await db.commit()

async def insert_player_death_from_event(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return

    death_id = str(event.get("event_id") or "").strip()
    if not death_id:
        return

    identity = await get_whitelist_by_steam_id(steam_id)
    discord_id = identity.get("discord_id") if identity else None

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT OR REPLACE INTO player_death_log (
                death_id,
                steam_id,
                discord_id,
                username,
                display_name,
                character_name,
                x,
                y,
                z,
                hours_survived,
                zombie_kills,
                survivor_kills,
                cause,
                event_ts,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                death_id,
                steam_id,
                discord_id,
                payload.get("username"),
                payload.get("display_name"),
                payload.get("character_name"),
                _safe_float(payload.get("x")),
                _safe_float(payload.get("y")),
                _safe_float(payload.get("z")),
                _safe_float(payload.get("hours_survived")),
                _safe_int(payload.get("zombie_kills"), 0) or 0,
                _safe_int(payload.get("survivor_kills"), 0) or 0,
                payload.get("cause"),
                event.get("ts"),
            ),
        )
        await db.commit()

    await increment_player_death_counter(
        steam_id,
        username=payload.get("username"),
        display_name=payload.get("display_name"),
        character_name=payload.get("character_name"),
        event_id=death_id,
        ts=event.get("ts"),
    )


async def open_player_session_from_event(event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return

    identity = await get_whitelist_by_steam_id(steam_id)
    discord_id = identity.get("discord_id") if identity else None

    event_id = str(event.get("event_id") or "").strip() or None
    ts = event.get("ts")

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO player_sessions_open (
                steam_id,
                discord_id,
                username,
                display_name,
                character_name,
                started_event_id,
                started_ts,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(steam_id) DO UPDATE SET
                discord_id = excluded.discord_id,
                username = excluded.username,
                display_name = excluded.display_name,
                character_name = excluded.character_name,
                started_event_id = excluded.started_event_id,
                started_ts = excluded.started_ts,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                steam_id,
                discord_id,
                payload.get("username"),
                payload.get("display_name"),
                payload.get("character_name"),
                event_id,
                ts,
            ),
        )
        await db.commit()


async def close_player_session_from_event(event: dict[str, Any]) -> float:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    steam_id = str(payload.get("steam_id") or "").strip()
    if not steam_id:
        return 0.0

    end_event_id = str(event.get("event_id") or "").strip() or None
    ended_ts = event.get("ts")
    now_unix = int(time.time()) # 👉 Momento exato em segundos

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT steam_id, discord_id, username, display_name, character_name, started_event_id, started_ts
            FROM player_sessions_open
            WHERE steam_id = ?
            LIMIT 1
            """,
            (steam_id,),
        )
        row = await cur.fetchone()

        if not row:
            # Mesmo sem sessão aberta, garantimos que o perfil fica offline
            await db.execute(
                "UPDATE player_profiles SET online = 0, last_logout_at = ?, updated_at = ? WHERE steam_id = ?",
                (now_unix, now_unix, steam_id)
            )
            await db.commit()
            return 0.0

        started_ts = row[6]
        session_id = f"{steam_id}:{row[5] or 'start'}:{end_event_id or 'end'}"
        session_seconds = max(0, float(ended_ts) - float(started_ts)) if started_ts and ended_ts else 0.0

        # Grava o log da sessão
        await db.execute(
            """
            INSERT OR REPLACE INTO player_sessions_log (
                session_id, steam_id, discord_id, username, display_name, character_name,
                started_ts, ended_ts, session_seconds, start_event_id, end_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, row[0], row[1], row[2], row[3], row[4], row[6], ended_ts, session_seconds, row[5], end_event_id, now_unix),
        )

        # Remove a sessão aberta
        await db.execute("DELETE FROM player_sessions_open WHERE steam_id = ?", (steam_id,))

        # 👉 [CORREÇÃO] Atualiza o perfil com tempo Unix absoluto para o Discord ler
        await db.execute(
            "UPDATE player_profiles SET online = 0, last_logout_at = ?, updated_at = ? WHERE steam_id = ?",
            (now_unix, now_unix, steam_id)
        )

        await db.commit()
        
    return session_seconds

# =========================================================
# REDEEM REQUESTS
# =========================================================

async def create_redeem_request(
    request_id: str,
    discord_id: int,
    steam_id: str | None,
    redeem_type: str,
    payload_json: str,
    requires_online: bool = False,
    requires_safe_position: bool = False,
) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO redeem_requests (
                request_id,
                discord_id,
                steam_id,
                redeem_type,
                payload_json,
                status,
                requires_online,
                requires_safe_position,
                api_result_json,
                error_message,
                created_at,
                updated_at,
                executed_at
            ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)
            """,
            (
                request_id,
                discord_id,
                steam_id,
                redeem_type,
                payload_json,
                1 if requires_online else 0,
                1 if requires_safe_position else 0,
            ),
        )
        await db.commit()


async def get_redeem_request(request_id: str) -> dict[str, Any] | None:
    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT
                request_id,
                discord_id,
                steam_id,
                redeem_type,
                payload_json,
                status,
                requires_online,
                requires_safe_position,
                api_result_json,
                error_message,
                created_at,
                updated_at,
                executed_at
            FROM redeem_requests
            WHERE request_id = ?
            LIMIT 1
            """,
            (request_id,),
        )
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "request_id": row[0],
        "discord_id": row[1],
        "steam_id": row[2],
        "redeem_type": row[3],
        "payload_json": row[4],
        "status": row[5],
        "requires_online": row[6],
        "requires_safe_position": row[7],
        "api_result_json": row[8],
        "error_message": row[9],
        "created_at": row[10],
        "updated_at": row[11],
        "executed_at": row[12],
    }

async def get_active_player_link_status_by_pz_steam_id(
    pz_reported_steam_id: str,
    server_id: str | None = None,
) -> dict[str, Any]:
    pz_reported_steam_id = str(pz_reported_steam_id or "").strip()
    server_id = str(server_id or "").strip()

    if not pz_reported_steam_id:
        return {
            "ok": True,
            "linked": False,
            "discord_id": None,
            "message": "Conta nao vinculada.",
            "link_code": None,
        }

    identity_status = await get_player_identity_link_status(pz_reported_steam_id)
    if identity_status.get("linked"):
        return {
            "ok": True,
            "linked": True,
            "discord_id": identity_status.get("discord_id"),
            "official_steam_id": None,
            "pz_reported_steam_id": pz_reported_steam_id,
            "username": None,
            "display_name": None,
            "character_name": None,
            "server_id": server_id or None,
            "linked_at": identity_status.get("linked_at"),
            "linked_via": "player_identity",
            "is_active": 1,
            "link_code": identity_status.get("link_code"),
            "message": "Conta ja vinculada.",
        }

    db = await get_db()
    async with _db_lock:
        if server_id:
            cur = await db.execute(
                """
                SELECT
                    discord_id,
                    official_steam_id,
                    pz_reported_steam_id,
                    username,
                    display_name,
                    character_name,
                    server_id,
                    linked_at,
                    linked_via,
                    is_active,
                    link_code
                FROM player_links
                WHERE pz_reported_steam_id = ?
                  AND server_id = ?
                  AND is_active = 1
                ORDER BY linked_at DESC, id DESC
                LIMIT 1
                """,
                (pz_reported_steam_id, server_id),
            )
        else:
            cur = await db.execute(
                """
                SELECT
                    discord_id,
                    official_steam_id,
                    pz_reported_steam_id,
                    username,
                    display_name,
                    character_name,
                    server_id,
                    linked_at,
                    linked_via,
                    is_active,
                    link_code
                FROM player_links
                WHERE pz_reported_steam_id = ?
                  AND is_active = 1
                ORDER BY linked_at DESC, id DESC
                LIMIT 1
                """,
                (pz_reported_steam_id,),
            )

        row = await cur.fetchone()

    if not row:
        return {
            "ok": True,
            "linked": False,
            "discord_id": None,
            "message": "Conta nao vinculada.",
            "link_code": None,
        }

    await sync_player_identity_link_status(
        pz_reported_steam_id=pz_reported_steam_id,
        server_id=server_id or None,
    )

    return {
        "ok": True,
        "linked": True,
        "discord_id": row[0],
        "official_steam_id": row[1],
        "pz_reported_steam_id": row[2],
        "username": row[3],
        "display_name": row[4],
        "character_name": row[5],
        "server_id": row[6],
        "linked_at": row[7],
        "linked_via": row[8],
        "is_active": row[9],
        "link_code": row[10],
        "message": "Conta ja vinculada.",
    }

async def update_redeem_request_status(
    request_id: str,
    status: str,
    api_result_json: str | None = None,
    error_message: str | None = None,
    executed: bool = False,
) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            UPDATE redeem_requests
               SET status = ?,
                   api_result_json = ?,
                   error_message = ?,
                   updated_at = CURRENT_TIMESTAMP,
                   executed_at = CASE WHEN ? = 1 THEN CURRENT_TIMESTAMP ELSE executed_at END
             WHERE request_id = ?
            """,
            (
                status,
                api_result_json,
                error_message,
                1 if executed else 0,
                request_id,
            ),
        )
        await db.commit()


async def get_redeem_requests_by_status(status: str, limit: int = 100) -> list[dict[str, Any]]:
    db = await get_db()
    cur = await db.execute(
        """
        SELECT request_id, discord_id, steam_id, redeem_type, payload_json, status,
               requires_online, requires_safe_position, api_result_json,
               error_message, created_at, updated_at, executed_at
          FROM redeem_requests
         WHERE status = ?
         ORDER BY created_at ASC
         LIMIT ?
        """,
        (status, int(limit)),
    )
    rows = await cur.fetchall()

    result = []
    for row in rows:
        result.append({
            "request_id": row[0],
            "discord_id": row[1],
            "steam_id": row[2],
            "redeem_type": row[3],
            "payload": json.loads(row[4] or "{}"),
            "status": row[5],
            "requires_online": bool(row[6]),
            "requires_safe_position": bool(row[7]),
            "api_result": json.loads(row[8]) if row[8] else None,
            "error_message": row[9],
            "created_at": row[10],
            "updated_at": row[11],
            "executed_at": row[12],
        })
    return result


async def get_player_identity_by_discord_id(discord_id: int) -> dict[str, Any] | None:
    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT
                steam_id,
                discord_id,
                username,
                display_name,
                character_name,
                last_whitelist_name,
                first_seen_at,
                last_seen_at,
                last_event_id,
                last_event_ts,
                updated_at
              FROM player_identity
             WHERE discord_id = ?
             LIMIT 1
            """,
            (discord_id,),
        )
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "steam_id": row[0],
        "discord_id": row[1],
        "username": row[2],
        "display_name": row[3],
        "character_name": row[4],
        "last_whitelist_name": row[5],
        "first_seen_at": row[6],
        "last_seen_at": row[7],
        "last_event_id": row[8],
        "last_event_ts": row[9],
        "updated_at": row[10],
    }


# =========================================================
# LEGACY HELPERS (mantidos para compatibilidade)
# =========================================================

async def upsert_player_profile(
    steam_id: str,
    username: Optional[str] = None,
    current_character_name: Optional[str] = None,
    current_profession: Optional[str] = None,
    online: Optional[bool] = None,
    is_alive: Optional[bool] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    z: Optional[float] = None,
    total_zombie_kills: Optional[int] = None,
    total_player_kills: Optional[int] = None,
    total_deaths: Optional[int] = None,
    total_survival_minutes: Optional[int] = None,
    current_run_minutes: Optional[int] = None,
    best_run_minutes: Optional[int] = None,
    first_seen_at: Optional[str] = None,
    last_seen_at: Optional[str] = None,
    last_login_at: Optional[str] = None,
    last_logout_at: Optional[str] = None,
) -> None:
    now = _utc_now_iso()
    db = await get_db()

    async with _db_lock:
        cur = await db.execute(
            "SELECT 1 FROM player_profiles WHERE steam_id = ?",
            (steam_id,),
        )
        existing = await cur.fetchone()

        if existing is None:
            await db.execute(
                """
                INSERT INTO player_profiles (
                    steam_id, username, current_character_name, current_profession,
                    online, is_alive, x, y, z,
                    total_zombie_kills, total_player_kills, total_deaths,
                    total_survival_minutes, current_run_minutes, best_run_minutes,
                    first_seen_at, last_seen_at, last_login_at, last_logout_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    steam_id,
                    username,
                    current_character_name,
                    current_profession,
                    int(online) if online is not None else 0,
                    int(is_alive) if is_alive is not None else 1,
                    x,
                    y,
                    z,
                    total_zombie_kills or 0,
                    total_player_kills or 0,
                    total_deaths or 0,
                    total_survival_minutes or 0,
                    current_run_minutes or 0,
                    best_run_minutes or 0,
                    first_seen_at or now,
                    last_seen_at or now,
                    last_login_at,
                    last_logout_at,
                ),
            )
        else:
            await db.execute(
                """
                UPDATE player_profiles
                   SET username = COALESCE(?, username),
                       current_character_name = COALESCE(?, current_character_name),
                       current_profession = COALESCE(?, current_profession),
                       online = COALESCE(?, online),
                       is_alive = COALESCE(?, is_alive),
                       x = COALESCE(?, x),
                       y = COALESCE(?, y),
                       z = COALESCE(?, z),
                       total_zombie_kills = COALESCE(?, total_zombie_kills),
                       total_player_kills = COALESCE(?, total_player_kills),
                       total_deaths = COALESCE(?, total_deaths),
                       total_survival_minutes = COALESCE(?, total_survival_minutes),
                       current_run_minutes = COALESCE(?, current_run_minutes),
                       best_run_minutes = COALESCE(?, best_run_minutes),
                       first_seen_at = COALESCE(?, first_seen_at),
                       last_seen_at = COALESCE(?, last_seen_at),
                       last_login_at = COALESCE(?, last_login_at),
                       last_logout_at = COALESCE(?, last_logout_at)
                 WHERE steam_id = ?
                """,
                (
                    username,
                    current_character_name,
                    current_profession,
                    int(online) if online is not None else None,
                    int(is_alive) if is_alive is not None else None,
                    x,
                    y,
                    z,
                    total_zombie_kills,
                    total_player_kills,
                    total_deaths,
                    total_survival_minutes,
                    current_run_minutes,
                    best_run_minutes,
                    first_seen_at,
                    last_seen_at or now,
                    last_login_at,
                    last_logout_at,
                    steam_id,
                ),
            )

        await db.commit()

async def sync_player_identity_link_status(
    pz_reported_steam_id: str,
    *,
    server_id: str | None = None,
) -> None:
    pz_reported_steam_id = str(pz_reported_steam_id or "").strip()
    server_id = str(server_id or "").strip()

    if not pz_reported_steam_id:
        return

    db = await get_db()
    async with _db_lock:
        if server_id:
            cur = await db.execute(
                """
                SELECT discord_id, link_code, linked_at
                FROM player_links
                WHERE pz_reported_steam_id = ?
                  AND server_id = ?
                  AND is_active = 1
                ORDER BY linked_at DESC, id DESC
                LIMIT 1
                """,
                (pz_reported_steam_id, server_id),
            )
        else:
            cur = await db.execute(
                """
                SELECT discord_id, link_code, linked_at
                FROM player_links
                WHERE pz_reported_steam_id = ?
                  AND is_active = 1
                ORDER BY linked_at DESC, id DESC
                LIMIT 1
                """,
                (pz_reported_steam_id,),
            )

        row = await cur.fetchone()

        if row:
            await db.execute(
                """
                INSERT INTO player_identity (
                    steam_id,
                    discord_id,
                    is_linked,
                    active_link_code,
                    linked_at,
                    first_seen_at,
                    last_seen_at,
                    updated_at
                ) VALUES (?, ?, 1, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(steam_id) DO UPDATE SET
                    discord_id = excluded.discord_id,
                    is_linked = 1,
                    active_link_code = excluded.active_link_code,
                    linked_at = excluded.linked_at,
                    last_seen_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    pz_reported_steam_id,
                    row[0],
                    row[1],
                    row[2],
                ),
            )
        else:
            await db.execute(
                """
                INSERT INTO player_identity (
                    steam_id,
                    is_linked,
                    active_link_code,
                    linked_at,
                    first_seen_at,
                    last_seen_at,
                    updated_at
                ) VALUES (?, 0, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(steam_id) DO UPDATE SET
                    is_linked = 0,
                    active_link_code = NULL,
                    linked_at = NULL,
                    last_seen_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (pz_reported_steam_id,),
            )

        await db.commit()

async def get_player_identity_link_status(steam_id: str) -> dict[str, Any]:
    steam_id = str(steam_id or "").strip()
    if not steam_id:
        return {
            "ok": True,
            "linked": False,
            "discord_id": None,
            "message": "Conta nao vinculada.",
            "link_code": None,
        }

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT
                discord_id,
                is_linked,
                active_link_code,
                linked_at
            FROM player_identity
            WHERE steam_id = ?
            LIMIT 1
            """,
            (steam_id,),
        )
        row = await cur.fetchone()

    if not row or int(row[1] or 0) != 1:
        return {
            "ok": True,
            "linked": False,
            "discord_id": None,
            "message": "Conta nao vinculada.",
            "link_code": None,
        }

    return {
        "ok": True,
        "linked": True,
        "discord_id": row[0],
        "link_code": row[2],
        "linked_at": row[3],
        "message": "Conta ja vinculada.",
    }

async def insert_game_event(
    event_id: str,
    steam_id: str,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: Optional[str] = None,
    processed_at: Optional[str] = None,
) -> None:
    now = _utc_now_iso()
    db = await get_db()

    async with _db_lock:
        await db.execute(
            """
            INSERT INTO game_events (
                event_id, steam_id, event_type, payload_json,
                occurred_at, processed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                steam_id,
                event_type,
                json.dumps(payload, ensure_ascii=False),
                occurred_at or now,
                processed_at,
                now,
            ),
        )
        await db.commit()


async def insert_player_snapshot(
    steam_id: str,
    username: Optional[str] = None,
    character_name: Optional[str] = None,
    profession: Optional[str] = None,
    online: bool = True,
    is_alive: bool = True,
    x: Optional[float] = None,
    y: Optional[float] = None,
    z: Optional[float] = None,
    zombie_kills_total: int = 0,
    player_kills_total: int = 0,
    deaths_total: int = 0,
    survival_minutes_total: int = 0,
    current_run_minutes: int = 0,
    captured_at: Optional[str] = None,
) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO player_snapshots (
                steam_id, username, character_name, profession,
                online, is_alive, x, y, z,
                zombie_kills_total, player_kills_total, deaths_total,
                survival_minutes_total, current_run_minutes, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                steam_id,
                username,
                character_name,
                profession,
                int(online),
                int(is_alive),
                x,
                y,
                z,
                zombie_kills_total,
                player_kills_total,
                deaths_total,
                survival_minutes_total,
                current_run_minutes,
                captured_at or _utc_now_iso(),
            ),
        )
        await db.commit()


async def create_delivery(
    delivery_id: str,
    steam_id: str,
    delivery_type: str,
    payload: dict[str, Any],
    discord_id: Optional[int] = None,
    status: str = "pending",
) -> None:
    now = _utc_now_iso()
    db = await get_db()

    async with _db_lock:
        await db.execute(
            """
            INSERT INTO deliveries (
                delivery_id, discord_id, steam_id, delivery_type,
                payload_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                discord_id,
                steam_id,
                delivery_type,
                json.dumps(payload, ensure_ascii=False),
                status,
                now,
            ),
        )
        await db.commit()


async def mark_delivery_sent(delivery_id: str) -> None:
    now = _utc_now_iso()
    db = await get_db()

    async with _db_lock:
        await db.execute(
            """
            UPDATE deliveries
               SET status = 'sent_to_server',
                   sent_at = ?
             WHERE delivery_id = ?
            """,
            (now, delivery_id),
        )
        await db.commit()


async def mark_delivery_delivered(delivery_id: str) -> None:
    now = _utc_now_iso()
    db = await get_db()

    async with _db_lock:
        await db.execute(
            """
            UPDATE deliveries
               SET status = 'delivered',
                   delivered_at = ?
             WHERE delivery_id = ?
            """,
            (now, delivery_id),
        )
        await db.commit()


async def mark_delivery_failed(delivery_id: str, fail_reason: str) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            UPDATE deliveries
               SET status = 'failed',
                   fail_reason = ?
             WHERE delivery_id = ?
            """,
            (fail_reason, delivery_id),
        )
        await db.commit()


async def get_pending_deliveries(steam_id: str) -> list[dict[str, Any]]:
    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT delivery_id, discord_id, steam_id, delivery_type,
                   payload_json, status, created_at, sent_at,
                   delivered_at, fail_reason
              FROM deliveries
             WHERE steam_id = ?
               AND status IN ('pending', 'sent_to_server')
             ORDER BY created_at ASC
            """,
            (steam_id,),
        )
        rows = await cur.fetchall()

    result = []
    for row in rows:
        result.append(
            {
                "delivery_id": row["delivery_id"],
                "discord_id": row["discord_id"],
                "steam_id": row["steam_id"],
                "delivery_type": row["delivery_type"],
                "payload": json.loads(row["payload_json"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "sent_at": row["sent_at"],
                "delivered_at": row["delivered_at"],
                "fail_reason": row["fail_reason"],
            }
        )
    return result


async def log_sync_event(
    source: str,
    action: str,
    status: str,
    details: Optional[str] = None,
) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO sync_logs (source, action, status, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source, action, status, details, _utc_now_iso()),
        )
        await db.commit()


# =========================================================
# WHITELIST
# =========================================================

async def upsert_whitelist(
    discord_id: int,
    steam_id: str,
    ingame_name: str,
    status: str,
    ban_info: str | None,
) -> None:
    status = (status or "").strip().lower()
    if status not in {"pendente", "aprovado", "rejeitado"}:
        status = "pendente"

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            "SELECT status, submitted_at, decided_at FROM whitelists WHERE discord_id=?;",
            (discord_id,),
        )
        row = await cur.fetchone()

        now_iso = _utc_now_iso()

        if row is None:
            submitted_at = now_iso
            decided_at = now_iso if status in {"aprovado", "rejeitado"} else None

            await db.execute(
                """
                INSERT INTO whitelists(
                    discord_id, steam_id, ingame_name, status, ban_info,
                    created_at, submitted_at, decided_at
                )
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (discord_id, steam_id, ingame_name, status, ban_info, now_iso, submitted_at, decided_at),
            )
        else:
            old_status = (row[0] or "").strip().lower()
            submitted_at = row[1] or now_iso
            old_decided = row[2]

            decided_at = old_decided
            if status in {"aprovado", "rejeitado"} and old_status != status:
                decided_at = now_iso

            await db.execute(
                """
                UPDATE whitelists
                   SET steam_id=?,
                       ingame_name=?,
                       status=?,
                       ban_info=?,
                       created_at=?,
                       submitted_at=?,
                       decided_at=?
                 WHERE discord_id=?
                """,
                (steam_id, ingame_name, status, ban_info, now_iso, submitted_at, decided_at, discord_id),
            )

        await db.commit()


async def get_whitelist_status(discord_id: int) -> tuple[str, str] | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT status, created_at FROM whitelists WHERE discord_id=?",
        (discord_id,),
    )
    row = await cur.fetchone()
    return (row[0], row[1]) if row else None


async def count_whitelist_pending() -> int:
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM whitelists WHERE status='pendente'")
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def count_whitelist_by_status(status: str, since_utc: datetime | None = None) -> int:
    status = (status or "").strip().lower()
    if status not in {"pendente", "aprovado", "rejeitado"}:
        raise ValueError("status inválido (use: pendente, aprovado, rejeitado)")

    db = await get_db()
    params: list[Any] = [status]
    where = "status=?"

    if since_utc is not None:
        if status in {"aprovado", "rejeitado"}:
            where += " AND decided_at IS NOT NULL AND decided_at >= ?"
            params.append(since_utc.astimezone(timezone.utc).isoformat())
        else:
            where += " AND submitted_at IS NOT NULL AND submitted_at >= ?"
            params.append(since_utc.astimezone(timezone.utc).isoformat())

    cur = await db.execute(f"SELECT COUNT(*) FROM whitelists WHERE {where}", tuple(params))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def count_whitelist_approved(since_utc: datetime | None = None) -> int:
    return await count_whitelist_by_status("aprovado", since_utc)


async def count_whitelist_rejected(since_utc: datetime | None = None) -> int:
    return await count_whitelist_by_status("rejeitado", since_utc)


# =========================================================
# CONFIG
# =========================================================

async def set_config(key: str, value: str) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO bot_config(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def get_config(key: str) -> str | None:
    db = await get_db()
    cur = await db.execute("SELECT value FROM bot_config WHERE key=?", (key,))
    row = await cur.fetchone()
    return row[0] if row else None


# =========================================================
# MODERATION
# =========================================================

async def revoke_warning(warning_id: int, staff_id: int, reason: str) -> bool:
    revoked_at = _utc_now_iso()
    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            UPDATE warnings
               SET revoked_by=?, revoked_at=?, revoked_reason=?
             WHERE id=? AND revoked_at IS NULL
            """,
            (staff_id, revoked_at, reason, warning_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def revoke_punishment(punishment_id: int, staff_id: int, reason: str) -> bool:
    revoked_at = _utc_now_iso()
    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            UPDATE punishments
               SET revoked_by=?, revoked_at=?, revoked_reason=?
             WHERE id=? AND revoked_at IS NULL
            """,
            (staff_id, revoked_at, reason, punishment_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_warning(warning_id: int):
    db = await get_db()
    cur = await db.execute(
        """
        SELECT id, guild_id, user_id, staff_id, reason, evidence, points, created_at,
               revoked_by, revoked_at, revoked_reason
          FROM warnings
         WHERE id=?
        """,
        (warning_id,),
    )
    return await cur.fetchone()


async def get_punishment(punishment_id: int):
    db = await get_db()
    cur = await db.execute(
        """
        SELECT id, guild_id, user_id, staff_id, type, reason, evidence,
               duration_seconds, created_at, revoked_by, revoked_at, revoked_reason
          FROM punishments
         WHERE id=?
        """,
        (punishment_id,),
    )
    return await cur.fetchone()


# =========================================================
# STAFF DASHBOARD
# =========================================================

async def set_staff_dashboard(guild_id: int, channel_id: int, message_id: int) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT INTO staff_dashboard(guild_id, channel_id, message_id)
            VALUES(?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
              channel_id=excluded.channel_id,
              message_id=excluded.message_id
            """,
            (guild_id, channel_id, message_id),
        )
        await db.commit()


async def get_staff_dashboard(guild_id: int) -> tuple[int, int] | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT channel_id, message_id FROM staff_dashboard WHERE guild_id=?",
        (guild_id,),
    )
    row = await cur.fetchone()
    return (int(row[0]), int(row[1])) if row else None


async def update_staff_dashboard_message_id(guild_id: int, message_id: int) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            "UPDATE staff_dashboard SET message_id=? WHERE guild_id=?",
            (message_id, guild_id),
        )
        await db.commit()


async def clear_staff_dashboard(guild_id: int) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute("DELETE FROM staff_dashboard WHERE guild_id=?", (guild_id,))
        await db.commit()


# =========================================================
# TICKET EVENTS
# =========================================================

async def log_ticket_event(
    guild_id: int,
    thread_id: int,
    ticket_type: str,
    action: str,
    created_at: datetime | None = None,
) -> None:
    ticket_type = (ticket_type or "outros").strip().lower()
    if ticket_type not in {"ajuda", "denuncia", "outros"}:
        ticket_type = "outros"

    action = (action or "").strip().lower()
    if action not in {"opened", "closed"}:
        raise ValueError("action inválida (use: opened, closed)")

    dt = created_at or datetime.now(timezone.utc)
    dt_iso = dt.astimezone(timezone.utc).isoformat()

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            INSERT OR IGNORE INTO ticket_events (
                guild_id, thread_id, ticket_type, action, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (int(guild_id), int(thread_id), ticket_type, action, dt_iso),
        )
        await db.commit()


async def count_ticket_events(
    guild_id: int,
    action: str,
    since_utc: datetime,
    ticket_type: str | None = None,
) -> int:
    action = (action or "").strip().lower()
    if action not in {"opened", "closed"}:
        raise ValueError("action inválida (use: opened, closed)")

    db = await get_db()
    params: list[Any] = [int(guild_id), action, since_utc.astimezone(timezone.utc).isoformat()]
    where = "guild_id=? AND action=? AND created_at>=?"

    if ticket_type:
        t = ticket_type.strip().lower()
        params.append(t)
        where += " AND ticket_type=?"

    cur = await db.execute(f"SELECT COUNT(*) FROM ticket_events WHERE {where}", tuple(params))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


# =========================================================
# MANUTENÇÃO
# =========================================================

async def optimize_db() -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute("PRAGMA optimize;")
        await db.commit()

# =========================================================
# LINK CODES / VÍNCULO DISCORD <-> JOGO
# =========================================================

def _generate_link_code(length: int = 8) -> str:
    import secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def _link_code_exists(db: aiosqlite.Connection, code: str) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM link_codes WHERE link_code=? LIMIT 1",
        (str(code or "").strip().upper(),),
    )
    row = await cur.fetchone()
    return row is not None


async def delete_link_code_by_discord_id(discord_id: int) -> None:
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            DELETE FROM link_codes
            WHERE discord_id = ?
            """,
            (int(discord_id),),
        )
        await db.commit()

async def get_link_code_by_discord_id(discord_id: int) -> dict[str, Any] | None:
    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT discord_id, link_code, created_at, updated_at, last_sent_at, is_active
              FROM link_codes
             WHERE discord_id=?
             LIMIT 1
            """,
            (int(discord_id),),
        )
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "discord_id": int(row[0]),
        "link_code": row[1],
        "created_at": row[2],
        "updated_at": row[3],
        "last_sent_at": row[4],
        "is_active": int(row[5] or 0),
    }


async def get_link_code_by_code(code: str) -> dict[str, Any] | None:
    code = str(code or "").strip().upper()
    if not code:
        return None

    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT discord_id, link_code, created_at, updated_at, last_sent_at, is_active
              FROM link_codes
             WHERE link_code=?
             LIMIT 1
            """,
            (code,),
        )
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "discord_id": int(row[0]),
        "link_code": row[1],
        "created_at": row[2],
        "updated_at": row[3],
        "last_sent_at": row[4],
        "is_active": int(row[5] or 0),
    }


async def touch_link_code_sent(discord_id: int) -> None:
    now_iso = _utc_now_iso()
    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            UPDATE link_codes
               SET last_sent_at=?,
                   updated_at=?
             WHERE discord_id=?
            """,
            (now_iso, now_iso, int(discord_id)),
        )
        await db.commit()


async def get_or_create_link_code(discord_id: int) -> dict[str, Any]:
    existing = await get_link_code_by_discord_id(discord_id)
    if existing and int(existing.get("is_active") or 0) == 1:
        return existing

    db = await get_db()
    async with _db_lock:
        # Double-check dentro do lock para evitar corrida
        cur = await db.execute(
            """
            SELECT discord_id, link_code, created_at, updated_at, last_sent_at, is_active
              FROM link_codes
             WHERE discord_id=?
             LIMIT 1
            """,
            (int(discord_id),),
        )
        row = await cur.fetchone()
        if row and int(row[5] or 0) == 1:
            return {
                "discord_id": int(row[0]),
                "link_code": row[1],
                "created_at": row[2],
                "updated_at": row[3],
                "last_sent_at": row[4],
                "is_active": int(row[5] or 0),
            }

        code = None
        for _ in range(20):
            candidate = _generate_link_code(8)
            if not await _link_code_exists(db, candidate):
                code = candidate
                break
        if not code:
            raise RuntimeError("Não foi possível gerar um código único de vínculo")

        now_iso = _utc_now_iso()
        await db.execute(
            """
            INSERT INTO link_codes(discord_id, link_code, created_at, updated_at, is_active)
            VALUES(?, ?, ?, ?, 1)
            ON CONFLICT(discord_id) DO UPDATE SET
                link_code=excluded.link_code,
                updated_at=excluded.updated_at,
                is_active=1
            """,
            (int(discord_id), code, now_iso, now_iso),
        )
        await db.commit()

    created = await get_link_code_by_discord_id(discord_id)
    if not created:
        raise RuntimeError("Falha ao recuperar código de vínculo recém-criado")
    return created


async def regenerate_link_code(discord_id: int) -> dict[str, Any]:
    db = await get_db()
    async with _db_lock:
        code = None
        for _ in range(20):
            candidate = _generate_link_code(8)
            if not await _link_code_exists(db, candidate):
                code = candidate
                break
        if not code:
            raise RuntimeError("Não foi possível regenerar um código único de vínculo")

        now_iso = _utc_now_iso()
        await db.execute(
            """
            INSERT INTO link_codes(discord_id, link_code, created_at, updated_at, is_active)
            VALUES(?, ?, ?, ?, 1)
            ON CONFLICT(discord_id) DO UPDATE SET
                link_code=excluded.link_code,
                updated_at=excluded.updated_at,
                is_active=1
            """,
            (int(discord_id), code, now_iso, now_iso),
        )
        await db.commit()

    result = await get_link_code_by_discord_id(discord_id)
    if not result:
        raise RuntimeError("Falha ao recuperar código de vínculo regenerado")
    return result


async def has_active_player_link(discord_id: int) -> bool:
    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT 1
              FROM player_links
             WHERE discord_id=?
               AND is_active=1
             LIMIT 1
            """,
            (int(discord_id),),
        )
        row = await cur.fetchone()
    return row is not None


async def get_active_player_link(discord_id: int) -> dict[str, Any] | None:
    db = await get_db()
    async with _db_lock:
        cur = await db.execute(
            """
            SELECT id, discord_id, official_steam_id, pz_reported_steam_id,
                   username, display_name, character_name, server_id,
                   linked_at, linked_via, is_active, link_code
              FROM player_links
             WHERE discord_id=?
               AND is_active=1
             ORDER BY linked_at DESC, id DESC
             LIMIT 1
            """,
            (int(discord_id),),
        )
        row = await cur.fetchone()

    if not row:
        return None

    return {
        "id": int(row[0]),
        "discord_id": int(row[1]),
        "official_steam_id": row[2],
        "pz_reported_steam_id": row[3],
        "username": row[4],
        "display_name": row[5],
        "character_name": row[6],
        "server_id": row[7],
        "linked_at": row[8],
        "linked_via": row[9],
        "is_active": int(row[10] or 0),
        "link_code": row[11],
    }

async def fix_player_links_index():
    """Corrige o índice único para permitir histórico de desvínculos."""
    db = await get_db()
    async with _db_lock:
        print("[DB-FIX] Ajustando índices da tabela player_links...")
        # 1. Removemos o índice antigo que está causando o erro
        await db.execute("DROP INDEX IF EXISTS uq_player_links_active_pz")
        
        # 2. Criamos um índice que SÓ é único quando o vínculo está ATIVO
        # Isso permite ter 1 ativo e infinitos inativos (histórico)
        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_player_links_only_one_active
            ON player_links(pz_reported_steam_id, server_id)
            WHERE is_active = 1
        """)
        await db.commit()
        print("[DB-FIX] Índices atualizados com sucesso!")

async def deactivate_player_link_by_steam_id(pz_reported_steam_id: str, *, server_id: str | None = None) -> dict[str, Any]:
    pz_reported_steam_id = str(pz_reported_steam_id or "").strip()
    db = await get_db()
    async with _db_lock:
        # 1. Procurar o discord_id antes de desativar para feedback
        cur = await db.execute("SELECT discord_id FROM player_links WHERE pz_reported_steam_id = ? AND is_active = 1 LIMIT 1", (pz_reported_steam_id,))
        row = await cur.fetchone()
        discord_id = row[0] if row else None

        # 2. Desativar TODOS os links ativos deste Steam ID
        await db.execute("UPDATE player_links SET is_active = 0 WHERE pz_reported_steam_id = ?", (pz_reported_steam_id,))
        
        # 3. Limpar a identidade do jogador (is_linked e código)
        await db.execute("""
            UPDATE player_identity 
            SET discord_id = NULL, is_linked = 0, active_link_code = NULL, linked_at = NULL 
            WHERE steam_id = ?
        """, (pz_reported_steam_id,))
        
        await db.commit()
    return {"ok": True, "discord_id": discord_id, "message": "Conta desvinculada com sucesso."}

async def deactivate_player_link_by_discord_id(discord_id: int) -> dict[str, Any]:
    db = await get_db()
    async with _db_lock:
        # Busca o steam_id do vínculo que está ATUALMENTE ativo
        cur = await db.execute(
            "SELECT pz_reported_steam_id FROM player_links WHERE discord_id = ? AND is_active = 1 LIMIT 1",
            (int(discord_id),)
        )
        row = await cur.fetchone()
        pz_reported_steam_id = str(row[0] or "").strip() if row else None

        # SÓ atualiza o que estiver com is_active = 1
        await db.execute(
            "UPDATE player_links SET is_active = 0 WHERE discord_id = ? AND is_active = 1",
            (int(discord_id),)
        )

        # Limpa a identidade para o jogo resetar a UI
        await db.execute(
            """
            UPDATE player_identity 
               SET discord_id = NULL, is_linked = 0, active_link_code = NULL, linked_at = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE discord_id = ? OR steam_id = ?
            """,
            (int(discord_id), pz_reported_steam_id)
        )
        await db.commit()

    return {"ok": True, "message": "Conta desvinculada com sucesso."}

async def create_or_update_player_link(
    discord_id: int,
    link_code: str,
    official_steam_id: str | None,
    pz_reported_steam_id: str,
    username: str | None = None,
    display_name: str | None = None,
    character_name: str | None = None,
    server_id: str | None = None,
    linked_via: str = "link_code",
) -> None:
    link_code = str(link_code or "").strip().upper()
    pz_reported_steam_id = str(pz_reported_steam_id or "").strip()

    if not link_code:
        raise ValueError("link_code ausente")
    if not pz_reported_steam_id:
        raise ValueError("pz_reported_steam_id ausente")

    db = await get_db()
    async with _db_lock:
        # 1. Desativa vínculo ativo anterior do mesmo discord_id
        await db.execute(
            "UPDATE player_links SET is_active=0 WHERE discord_id=? AND is_active=1",
            (int(discord_id),),
        )

        # 2. NOVO: Desativa vínculo ativo anterior para a mesma steam_id!
        await db.execute(
            "UPDATE player_links SET is_active=0 WHERE pz_reported_steam_id=? AND is_active=1",
            (pz_reported_steam_id,),
        )

        # 3. Insere o novo vínculo
        await db.execute(
            """
            INSERT INTO player_links(
                discord_id, link_code, official_steam_id, pz_reported_steam_id,
                username, display_name, character_name, server_id,
                linked_at, linked_via, is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                int(discord_id), link_code, str(official_steam_id).strip() if official_steam_id else None,
                pz_reported_steam_id, str(username or "").strip() or None,
                str(display_name or "").strip() or None, str(character_name or "").strip() or None,
                str(server_id or "").strip() or None, _utc_now_iso(),
                str(linked_via or "link_code").strip() or "link_code",
            ),
        )
        await db.commit()

async def consume_link_code_submit_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    code = str(payload.get("code") or "").strip().upper()
    pz_reported_steam_id = str(payload.get("steam_id") or "").strip()
    username = str(payload.get("username") or "").strip() or None
    display_name = str(payload.get("display_name") or "").strip() or None
    character_name = str(payload.get("character_name") or "").strip() or None
    server_id = str(event.get("server_id") or payload.get("server_id") or "main").strip() or "main"

    if not code:
        return {
            "ok": False,
            "linked": False,
            "message": "Codigo vazio.",
            "code": code,
        }

    if not pz_reported_steam_id:
        return {
            "ok": False,
            "linked": False,
            "message": "Nao foi possivel identificar o jogador no jogo.",
            "code": code,
        }

    link_code_row = await get_link_code_by_code(code)
    if not link_code_row:
        return {
            "ok": False,
            "linked": False,
            "message": "Codigo invalido ou expirado. Gere um novo codigo no Discord.",
            "code": code,
        }

    if int(link_code_row.get("is_active") or 0) != 1:
        return {
            "ok": False,
            "linked": False,
            "message": "Codigo invalido ou expirado. Gere um novo codigo no Discord.",
            "code": code,
        }

    discord_id = int(link_code_row["discord_id"])

    await create_or_update_player_link(
        discord_id=discord_id,
        link_code=code,
        official_steam_id=pz_reported_steam_id,
        pz_reported_steam_id=pz_reported_steam_id,
        username=username,
        display_name=display_name,
        character_name=character_name,
        server_id=server_id,
        linked_via="link_code_submit",
    )

    db = await get_db()
    async with _db_lock:
        await db.execute(
            """
            UPDATE link_codes
               SET is_active = 0,
                   updated_at = ?
             WHERE discord_id = ?
            """,
            (_utc_now_iso(), discord_id),
        )

        await db.execute(
            """
            UPDATE player_identity
               SET discord_id = NULL,
                   updated_at = CURRENT_TIMESTAMP
             WHERE discord_id = ?
               AND steam_id <> ?
            """,
            (discord_id, pz_reported_steam_id),
        )

        await db.execute(
            """
            INSERT INTO player_identity (
                steam_id,
                discord_id,
                username,
                display_name,
                character_name,
                last_whitelist_name,
                is_linked,
                active_link_code,
                linked_at,
                first_seen_at,
                last_seen_at,
                last_event_id,
                last_event_ts,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(steam_id) DO UPDATE SET
                discord_id = excluded.discord_id,
                username = COALESCE(excluded.username, player_identity.username),
                display_name = COALESCE(excluded.display_name, player_identity.display_name),
                character_name = COALESCE(excluded.character_name, player_identity.character_name),
                is_linked = 1,
                active_link_code = excluded.active_link_code,
                linked_at = excluded.linked_at,
                last_seen_at = CURRENT_TIMESTAMP,
                last_event_id = COALESCE(excluded.last_event_id, player_identity.last_event_id),
                last_event_ts = COALESCE(excluded.last_event_ts, player_identity.last_event_ts),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                pz_reported_steam_id,
                discord_id,
                username,
                display_name,
                character_name,
                code,
                str(event.get("event_id") or "").strip() or None,
                event.get("ts"),
            ),
        )

        await db.commit()

    return {
        "ok": True,
        "linked": True,
        "message": "Conta vinculada com sucesso.",
        "discord_id": discord_id,
        "code": code,
    }

# =========================================================
# SISTEMA DE ECONOMIA DO DISCORD
# =========================================================

async def add_discord_balance(discord_id: int, amount: int) -> int:
    db = await get_db()
    async with _db_lock: # Escrita ainda precisa de trava
        await db.execute("""
            INSERT INTO discord_economy (discord_id, balance, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(discord_id) DO UPDATE SET
                balance = balance + excluded.balance,
                updated_at = CURRENT_TIMESTAMP
        """, (int(discord_id), int(amount)))
        await db.commit()
        cur = await db.execute("SELECT balance FROM discord_economy WHERE discord_id = ?", (int(discord_id),))
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_discord_balance(discord_id: int) -> int:
    """VIA VERDE: Leitura sem trava para a loja não travar."""
    db = await get_db()
    cur = await db.execute("SELECT balance FROM discord_economy WHERE discord_id = ?", (int(discord_id),))
    row = await cur.fetchone()
    return row[0] if row else 0
    
async def get_player_profile(steam_id: str) -> dict[str, Any] | None:
    """VIA VERDE: Leitura sem trava para a loja não travar."""
    steam_id = str(steam_id or "").strip()
    if not steam_id: return None
    db = await get_db()
    cur = await db.execute("""
        SELECT online, is_alive, inventory_weight, carry_capacity, is_in_vehicle, is_outdoors
        FROM player_profiles WHERE steam_id = ? LIMIT 1
    """, (steam_id,))
    row = await cur.fetchone()
    if not row: return None
    return {
        "online": int(row[0] or 0), "is_alive": int(row[1] or 0),
        "inventory_weight": float(row[2] or 0.0), "carry_capacity": float(row[3] or 0.0),
        "is_in_vehicle": int(row[4] or 0), "is_outdoors": int(row[5] or 0)
    }