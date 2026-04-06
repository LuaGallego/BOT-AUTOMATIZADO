from datetime import datetime
from services.pz_server_agent import PZServerAgentClient
from utils.db import log_sync_event, insert_player_snapshot, upsert_player_profile


class PZSyncService:
    def __init__(self):
        self.agent = PZServerAgentClient(
            base_url="http://sp-18.raze.host:9000",
            api_key="MINHA_CHAVE_PZ_123",  # depois trocamos para env
            timeout=15,
        )

    async def sync_server_status(self) -> dict:
        try:
            data = await self.agent.server_status()

            await log_sync_event(
                source="pz_agent",
                action="sync_server_status",
                status="success",
                details=str(data),
            )

            return data

        except Exception as e:
            await log_sync_event(
                source="pz_agent",
                action="sync_server_status",
                status="failed",
                details=str(e),
            )
            raise
        
    async def sync_log_dates(self) -> dict:
        try:
            data = await self.agent.log_dates()

            await log_sync_event(
                source="pz_agent",
                action="sync_log_dates",
                status="success",
                details=str(data),
            )

            return data

        except Exception as e:
            await log_sync_event(
                source="pz_agent",
                action="sync_log_dates",
                status="failed",
                details=str(e),
            )
            raise

    async def test_connection(self) -> dict:
        try:
            data = await self.agent.health()

            await log_sync_event(
                source="pz_agent",
                action="health_check",
                status="success",
                details=str(data),
            )

            return data

        except Exception as e:
            await log_sync_event(
                source="pz_agent",
                action="health_check",
                status="failed",
                details=str(e),
            )
            raise

    async def sync_players_online(self) -> dict:
        try:
            data = await self.agent.players_online()
            players = data.get("players", [])
            now = datetime.utcnow().isoformat()

            for player in players:
                steam_id = str(player.get("steam_id") or "").strip()
                if not steam_id:
                    continue

                username = player.get("username")
                character_name = player.get("character_name") or player.get("name")
                profession = player.get("profession")

                x = player.get("x")
                y = player.get("y")
                z = player.get("z")

                zombie_kills = int(player.get("zombie_kills_total") or 0)
                player_kills = int(player.get("player_kills_total") or 0)
                deaths_total = int(player.get("deaths_total") or 0)
                survival_minutes = int(player.get("survival_minutes_total") or 0)
                current_run_minutes = int(player.get("current_run_minutes") or 0)

                await insert_player_snapshot(
                    steam_id=steam_id,
                    username=username,
                    character_name=character_name,
                    profession=profession,
                    online=True,
                    is_alive=True,
                    x=x,
                    y=y,
                    z=z,
                    zombie_kills_total=zombie_kills,
                    player_kills_total=player_kills,
                    deaths_total=deaths_total,
                    survival_minutes_total=survival_minutes,
                    current_run_minutes=current_run_minutes,
                    captured_at=now,
                )

                await upsert_player_profile(
                    steam_id=steam_id,
                    username=username,
                    current_character_name=character_name,
                    current_profession=profession,
                    online=True,
                    is_alive=True,
                    x=x,
                    y=y,
                    z=z,
                    total_zombie_kills=zombie_kills,
                    total_player_kills=player_kills,
                    total_deaths=deaths_total,
                    total_survival_minutes=survival_minutes,
                    current_run_minutes=current_run_minutes,
                    last_seen_at=now,
                )

            await log_sync_event(
                source="pz_agent",
                action="sync_players_online",
                status="success",
                details=f"{len(players)} players synced",
            )

            return {"ok": True, "count": len(players), "raw": data}

        except Exception as e:
            await log_sync_event(
                source="pz_agent",
                action="sync_players_online",
                status="failed",
                details=str(e),
            )
            raise