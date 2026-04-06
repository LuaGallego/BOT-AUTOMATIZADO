import aiohttp

STEAM_BANS_URL = "https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/"

def is_valid_steamid64(s: str) -> bool:
    return s.isdigit() and len(s) == 17

async def get_player_bans(api_key: str, steamid64: str) -> dict | None:
    if not api_key:
        return None
    params = {"key": api_key, "steamids": steamid64}
    async with aiohttp.ClientSession() as session:
        async with session.get(STEAM_BANS_URL, params=params, timeout=12) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    players = (data or {}).get("players") or []
    return players[0] if players else None

def format_ban_details(bans: dict) -> tuple[bool, str]:
    """
    Retorna (precisa_aprovacao, texto_motivo)
    """
    community = bool(bans.get("CommunityBanned"))
    vac = bool(bans.get("VACBanned"))
    vac_n = int(bans.get("NumberOfVACBans") or 0)
    game_n = int(bans.get("NumberOfGameBans") or 0)
    econ = bool(bans.get("EconomyBan")) and bans.get("EconomyBan") != "none"
    days = int(bans.get("DaysSinceLastBan") or 0)

    motivo = (
        f"CommunityBanned: **{community}** | "
        f"VACBanned: **{vac}** (VAC bans: **{vac_n}**) | "
        f"GameBans: **{game_n}** | "
        f"EconomyBan: **{bans.get('EconomyBan')}** | "
        f"Dias desde último ban: **{days}**"
    )

    # Regra segura: qualquer ban => aprovação manual
    precisa = community or vac or game_n > 0 or econ or (days > 0 and days <= 365)
    return precisa, motivo