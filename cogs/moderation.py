from __future__ import annotations

import os
import re
import traceback
import datetime as dt
import asyncio
from typing import Optional, Any
from collections import defaultdict, deque

import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiosqlite

from utils.db import DB_PATH, get_config, set_config

# =========================
# ENV
# =========================
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
MOD_LOG_THREAD_ID = int(os.getenv("MOD_LOG_THREAD_ID", "0"))      # ID da THREAD (post fixo no fórum)
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "0"))    # fallback (canal normal)

# Timeout hardcore (cargo automático)
TIMEOUT_ROLE_NAME = os.getenv("TIMEOUT_ROLE_NAME", "🚫 Timeout").strip() or "🚫 Timeout"

# =========================
# Helpers
# =========================

def _is_staff(member: discord.Member) -> bool:
    p = member.guild_permissions
    return (
        p.administrator
        or p.moderate_members
        or p.manage_guild
        or p.manage_roles
        or p.kick_members
        or p.ban_members
    )

def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)

def parse_duration_to_seconds(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    m = DURATION_RE.match(s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return n * mult

async def get_log_target(bot: commands.Bot, guild: discord.Guild) -> Optional[discord.abc.Messageable]:
    """Retorna o destino de log:
    1) Thread fixa (MOD_LOG_THREAD_ID)
    2) Canal fallback (MOD_LOG_CHANNEL_ID)
    """
    # 1) Thread fixa
    if MOD_LOG_THREAD_ID:
        ch: Optional[discord.abc.GuildChannel | discord.Thread] = None
        try:
            ch = guild.get_thread(MOD_LOG_THREAD_ID)
        except Exception:
            ch = None
        if ch is None:
            try:
                ch = guild.get_channel(MOD_LOG_THREAD_ID)  # type: ignore
            except Exception:
                ch = None
        if ch is None:
            try:
                ch = await bot.fetch_channel(MOD_LOG_THREAD_ID)  # type: ignore
            except Exception:
                ch = None

        if isinstance(ch, discord.Thread):
            try:
                if ch.archived:
                    await ch.edit(archived=False)
            except Exception:
                pass
            return ch

        # Se apontaram para um canal normal por engano, ainda tenta enviar
        if isinstance(ch, discord.abc.Messageable):
            return ch

    # 2) Fallback canal
    if MOD_LOG_CHANNEL_ID:
        ch2 = guild.get_channel(MOD_LOG_CHANNEL_ID)
        if ch2 is None:
            try:
                ch2 = await bot.fetch_channel(MOD_LOG_CHANNEL_ID)  # type: ignore
            except Exception:
                ch2 = None
        if isinstance(ch2, discord.abc.Messageable):
            return ch2

    return None

async def send_mod_log(bot: commands.Bot, guild: discord.Guild, embed: discord.Embed, view: discord.ui.View | None = None) -> None:
    try:
        target = await get_log_target(bot, guild)
        if target:
            embed = _style_embed(embed, guild=guild, accent_name="Moderação")
            await target.send(embed=embed, view=view)
        else:
            print("[MOD] Nenhum destino de log configurado (MOD_LOG_THREAD_ID / MOD_LOG_CHANNEL_ID).")
    except Exception as e:
        print(f"[MOD] Falha ao enviar log: {type(e).__name__}: {e}")

async def send_dm_embed(
    user: discord.abc.User,
    *,
    title: str,
    description: str,
    motivo: str,
    prova: str | None,
    footer: str,
    color: discord.Color,
    thumbnail_url: str | None = None,
) -> bool:
    """Tenta enviar DM. Retorna True se enviou; False se falhou (DM fechada/bloqueio)."""
    try:
        desc = (description or "").strip()
        motivo_txt = (motivo or "Não informado.").strip()
        prova_txt = (prova or "").strip()

        # força vermelho mais próximo do painel
        panel_red = discord.Color.from_rgb(237, 66, 69)

        emb = discord.Embed(
            title=f"🛡️ {title}",
            description=(
                "**CENTRAL DE MODERAÇÃO**\n"
                "**PAINEL DE PUNIÇÕES**\n\n"
                f"{desc}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=panel_red,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )

        if thumbnail_url:
            emb.set_thumbnail(url=thumbnail_url)

        emb.add_field(
            name="📌 MOTIVO",
            value=f"```{motivo_txt[:900]}```",
            inline=False,
        )

        if prova_txt:
            emb.add_field(
                name="🧾 PROVA / OBSERVAÇÃO",
                value=f"```{prova_txt[:900]}```",
                inline=False,
            )

        emb.add_field(
            name="🔎 ORIENTAÇÃO",
            value=(
                "Se você acredita que houve um engano, procure a staff "
                "pelos canais oficiais do servidor."
            ),
            inline=False,
        )

        emb.set_footer(text=footer)

        # NÃO chama _style_embed aqui, senão ele puxa o visual antigo
        await user.send(embed=emb)
        return True

    except Exception as e:
        print(f"[MOD] DM falhou para {user} ({getattr(user,'id',None)}): {type(e).__name__}: {e}")
        return False

def _server_thumb(guild: discord.Guild, fallback_avatar_url: str | None = None) -> str | None:
    if guild.icon:
        return guild.icon.url
    return fallback_avatar_url

def _spacious(text: str | None) -> str | None:
    if not text:
        return text
    t = str(text).replace("\r\n", "\n").strip()
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t


# =========================
# EMBED STYLE (CINEMATIC / PREMIUM)
# =========================

# Paleta principal (preto + pink neon)
EMBED_COLOR_PRIMARY = discord.Color.from_rgb(255, 0, 102)     # pink neon
EMBED_COLOR_SUCCESS = discord.Color.from_rgb(57, 255, 20)     # verde neon (opcional)
EMBED_COLOR_WARN    = discord.Color.from_rgb(255, 184, 0)     # amber (opcional)
EMBED_COLOR_DANGER  = discord.Color.from_rgb(255, 59, 92)     # vermelho rosado (opcional)

# Separadores e detalhes visuais
_EMBED_BAR = "━━━━━━━━━━━━━━━━━━━━"
_EMBED_FADE = "┈┈┈┈┈┈┈┈┈┈┈┈"
_EMBED_DOT = "•"


def _cinema_block(text: str) -> str:
    t = _spacious(text) or "—"

    # Não mexe em bloco de código
    if "```" in t:
        return t

    # Moldura sutil premium
    if t.startswith("▌"):
        return t

    # Para textos curtos, deixa com cara de painel
    if len(t) <= 900:
        return f"▌ {t}"

    return t


def _pretty_field_name(name: str) -> str:
    n = (_spacious(name) or "").strip()
    if not n:
        return "\u200b"
    if n in {"\u200b", "—"}:
        return "\u200b"

    # Mantém nomes já estilizados
    if n.startswith(("✦", "▌", "┈", "•", "◆", "◇", "◈", "⟡", "⟢", "⟣")):
        return n

    # Cabeçalho de campo mais elegante
    return f"✦ {n}"


def _normalize_embed_visuals(emb: discord.Embed) -> None:
    """Uniformiza cor, espaçamento e pequenos acabamentos visuais."""
    try:
        # Cor lateral padrão (pink neon) se não vier cor definida
        if not emb.color or int(emb.color.value) == 0:
            emb.color = EMBED_COLOR_PRIMARY

        # Título
        if emb.title:
            title = _spacious(emb.title) or emb.title
            title = re.sub(r"^[✦▌◆◇◈⟡]+\s*", "", title).strip()
            emb.title = f"✦ {title}"

        # Descrição
        if emb.description:
            desc = _spacious(emb.description) or ""
            desc = _cinema_block(desc)

            # espaçamento visual bonito
            if len(desc) < 1800 and not desc.endswith("\n\u200b"):
                desc = f"{desc}\n\u200b"

            emb.description = desc

        # Campos
        if emb.fields:
            rebuilt: list[tuple[str, str, bool]] = []
            for f in emb.fields:
                raw_name = getattr(f, "name", "") or "\u200b"
                raw_value = getattr(f, "value", "") or "—"
                inline = bool(getattr(f, "inline", False))

                name = _pretty_field_name(str(raw_name))
                value = _spacious(str(raw_value)) or "—"

                # Só aplica moldura se não for código
                if "```" not in value:
                    value = _cinema_block(value)

                # Dá respiro sem quebrar lógica
                if len(value) <= 980 and not value.endswith("\n\u200b"):
                    value = f"{value}\n\u200b"

                rebuilt.append((name, value, inline))

            emb.clear_fields()
            for name, value, inline in rebuilt:
                emb.add_field(name=name, value=value, inline=inline)

            # Separador final sutil (se tiver espaço)
            has_sep = any(
                (getattr(f, "name", "") == "\u200b")
                and (
                    str(getattr(f, "value", "")).replace("\u200b", "").strip()
                    in {_EMBED_BAR, _EMBED_FADE, "─" * 18}
                )
                for f in emb.fields
            )
            if len(rebuilt) >= 2 and len(emb.fields) < 25 and not has_sep:
                emb.add_field(name="\u200b", value=_EMBED_FADE, inline=False)

        # Timestamp
        if emb.timestamp is None:
            emb.timestamp = dt.datetime.now(dt.timezone.utc)

    except Exception:
        pass


def _style_embed(
    emb: discord.Embed,
    *,
    guild: discord.Guild | None = None,
    accent_name: str | None = None,
    compact: bool = False,
) -> discord.Embed:
    """Ajuste visual global dos embeds (sem mexer na lógica)."""
    try:
        # Base visual global
        _normalize_embed_visuals(emb)

        # Variante compacta (DM etc.) -> menos "enfeite"
        if compact:
            if emb.description:
                d = _spacious(emb.description) or ""
                if d.startswith("▌ "):
                    emb.description = d[2:]
            # campos compactos: remove prefixo pesado no valor
            if emb.fields:
                rebuilt: list[tuple[str, str, bool]] = []
                for f in emb.fields:
                    name = str(getattr(f, "name", "") or "\u200b")
                    value = str(getattr(f, "value", "") or "—")
                    inline = bool(getattr(f, "inline", False))

                    value = _spacious(value) or "—"
                    if value.startswith("▌ "):
                        value = value[2:]
                    if len(value) <= 980 and not value.endswith("\n\u200b"):
                        value = f"{value}\n\u200b"

                    rebuilt.append((name, value, inline))

                emb.clear_fields()
                for name, value, inline in rebuilt:
                    emb.add_field(name=name, value=value, inline=inline)

        # Author (topo)
        if not emb.author.name:
            if guild and guild.icon:
                emb.set_author(
                    name=f"{guild.name}  {_EMBED_DOT}  {accent_name or 'Dashboard Advs Doom'}",
                    icon_url=guild.icon.url,
                )
            elif guild:
                emb.set_author(name=f"{guild.name}  {_EMBED_DOT}  {accent_name or 'Dashboard Advs Doom'}")
            elif accent_name:
                emb.set_author(name=f"{accent_name}")

        # Thumbnail (usa ícone do servidor se não houver)
        try:
            if guild and guild.icon and not getattr(emb.thumbnail, "url", None):
                emb.set_thumbnail(url=guild.icon.url)
        except Exception:
            pass

        # Footer premium
        footer_text = (emb.footer.text or "").strip() if emb.footer else ""
        base = accent_name or (guild.name if guild else "Sistema")
        if not footer_text:
            emb.set_footer(text=f"{base}  {_EMBED_DOT}  registro oficial")
        else:
            clean = _spacious(footer_text) or footer_text
            clean = re.sub(r"\s*•\s*", f"  {_EMBED_DOT}  ", clean)
            if "registro oficial" not in clean.lower() and base.lower() not in clean.lower():
                clean = f"{clean}  {_EMBED_DOT}  {base}"
            emb.set_footer(text=clean)

    except Exception:
        pass
    return emb


# =========================
# AUTO-MOD (regras básicas)
# =========================
URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
INVITE_RE = re.compile(r"(discord\.gg/|discord\.com/invite/)", re.IGNORECASE)

# Coloque aqui os canais onde link é permitido (IDs)
AUTOMOD_ALLOWED_LINK_CHANNELS: set[int] = {
    # 123456789012345678,
}

# Palavras proibidas (lowercase)
AUTOMOD_BAD_WORDS: set[str] = {
    # "palavra1",
    # "palavra2",
}

def _normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def _caps_ratio(s: str) -> float:
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return 0.0
    caps = sum(1 for c in letters if c.isupper())
    return caps / len(letters)


TZ_BR = dt.timezone(dt.timedelta(hours=-3))

def _now_br() -> dt.datetime:
    return dt.datetime.now(TZ_BR)

def _bar(value: int, max_value: int, width: int = 12) -> str:
    max_value = max(1, max_value)
    v = max(0, min(value, max_value))
    filled = int(round((v / max_value) * width))
    return "▰" * filled + "▱" * (width - filled)

def _status_emoji(level: str) -> str:
    return {"ok": "🟢", "warn": "🟠", "bad": "🔴"}.get(level, "🟢")

async def _trigger_punish_dashboard_update(bot: commands.Bot, guild_id: int) -> None:
    try:
        cog = bot.get_cog("Moderation")
        if cog and hasattr(cog, "schedule_punish_dashboard_update"):
            cog.schedule_punish_dashboard_update(delay=0.2)
    except Exception:
        pass

# =========================
# HARDCORE TIMEOUT ROLE
# =========================

async def _apply_timeout_role_overwrites(guild: discord.Guild, role: discord.Role) -> None:
    """Aplica permissões negativas do cargo de timeout em TODOS os canais/categorias."""
    for ch in guild.channels:
        try:
            # overwrite com bloqueios principais
            await ch.set_permissions(
                role,
                send_messages=False,
                add_reactions=False,
                speak=False,
                stream=False,
                connect=False,
                send_messages_in_threads=False,
                create_public_threads=False,
                create_private_threads=False,
                reason="Timeout hardcore (auto)",
            )
        except Exception:
            # sem permissão pra editar canal, ou tipo não suportado
            pass

async def get_or_create_timeout_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=TIMEOUT_ROLE_NAME)
    if role:
        return role

    # cria com permissões none
    role = await guild.create_role(
        name=TIMEOUT_ROLE_NAME,
        permissions=discord.Permissions.none(),
        reason="Cargo automático de timeout (hardcore)",
    )

    # aplica overwrites em todos canais
    await _apply_timeout_role_overwrites(guild, role)
    return role

async def add_timeout_hardcore(member: discord.Member) -> None:
    role = await get_or_create_timeout_role(member.guild)
    # adiciona cargo
    try:
        if role not in member.roles:
            await member.add_roles(role, reason="Timeout hardcore")
    except Exception:
        raise

    # desconecta de voz se estiver em call (precisa Move Members)
    try:
        if member.voice and member.voice.channel:
            await member.move_to(None, reason="Timeout hardcore (desconectar)")
    except Exception:
        pass

async def remove_timeout_hardcore(member: discord.Member) -> None:
    role = discord.utils.get(member.guild.roles, name=TIMEOUT_ROLE_NAME)
    if not role:
        return

    try:
        if role in member.roles:
            await member.remove_roles(role, reason="Timeout acabou (hardcore)")
    except Exception:
        return

    # se ninguém mais tiver o cargo, remove o cargo (limpeza)
    try:
        # role.members só existe em guild cacheado (normalmente ok)
        if len(getattr(role, "members", [])) == 0:
            await role.delete(reason="Timeout role cleanup (ninguém usando)")
    except Exception:
        pass

# =========================
# DB helpers (migração local)
# =========================

async def _ensure_columns(db: aiosqlite.Connection, table: str, columns_sql: list[str]) -> None:
    cur = await db.execute(f"PRAGMA table_info({table})")
    rows = await cur.fetchall()
    existing = {r[1] for r in rows}
    for coldef in columns_sql:
        col = coldef.split()[0].strip()
        if col not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")

async def ensure_warning_duration_columns() -> None:
    """Garante colunas para advertência com duração."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_columns(db, "warnings", [
            "duration_seconds INTEGER",
            "expires_at TEXT",
            "expired_notified_at TEXT",
        ])
        await db.commit()

async def ensure_punishment_timeout_columns() -> None:
    """Garante colunas extras usadas por punições (inclusive timeout)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_columns(db, "punishments", [
            "duration_seconds INTEGER",
            "ends_at TEXT",
            "end_notified_at TEXT",
        ])
        await db.commit()

# =========================
# DB operations
# =========================

async def db_add_warning(
    guild_id: int,
    user_id: int,
    staff_id: int,
    reason: str,
    evidence: str | None,
    points: int = 1,
    duration_seconds: int | None = None,
) -> int:
    created_at = _utc_now_iso()

    expires_at = None
    if duration_seconds is not None:
        try:
            dt0 = dt.datetime.fromisoformat(created_at)
            expires_at = (dt0 + dt.timedelta(seconds=int(duration_seconds))).isoformat(timespec="seconds")
        except Exception:
            expires_at = None

    async with aiosqlite.connect(DB_PATH) as db:
        # garante schema (caso init do cog ainda não rodou)
        await _ensure_columns(db, "warnings", [
            "duration_seconds INTEGER",
            "expires_at TEXT",
            "expired_notified_at TEXT",
        ])
        cur = await db.execute(
            """
            INSERT INTO warnings (guild_id, user_id, staff_id, reason, evidence, points, created_at, duration_seconds, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (int(guild_id), int(user_id), int(staff_id), reason, evidence, int(points), created_at, duration_seconds, expires_at),
        )
        await db.commit()
        return int(cur.lastrowid)

async def db_revoke_warning(warn_id: int, staff_id: int, revoke_reason: str) -> bool:
    revoked_at = _utc_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE warnings
               SET revoked_by=?, revoked_at=?, revoked_reason=?
             WHERE id=? AND revoked_at IS NULL
            """,
            (int(staff_id), revoked_at, revoke_reason, int(warn_id)),
        )
        await db.commit()
        return cur.rowcount > 0

async def db_get_warning_user_id(warn_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM warnings WHERE id=?", (int(warn_id),))
        row = await cur.fetchone()
        return int(row[0]) if row else None

async def db_add_punishment(
    guild_id: int,
    user_id: int,
    staff_id: int,
    ptype: str,
    reason: str,
    evidence: str | None,
    duration_seconds: int | None,
) -> int:
    created_at = _utc_now_iso()

    ends_at = None
    if ptype == "timeout" and duration_seconds is not None:
        try:
            dt0 = dt.datetime.fromisoformat(created_at)
            ends_at = (dt0 + dt.timedelta(seconds=int(duration_seconds))).isoformat(timespec="seconds")
        except Exception:
            ends_at = None

    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_columns(db, "punishments", [
            "duration_seconds INTEGER",
            "ends_at TEXT",
            "end_notified_at TEXT",
        ])

        # Detecta se a coluna ends_at existe pra inserir com segurança
        cur = await db.execute("PRAGMA table_info(punishments)")
        cols = {r[1] for r in await cur.fetchall()}

        if "ends_at" in cols:
            cur2 = await db.execute(
                """
                INSERT INTO punishments (guild_id, user_id, staff_id, type, reason, evidence, duration_seconds, created_at, ends_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(guild_id), int(user_id), int(staff_id), ptype, reason, evidence, duration_seconds, created_at, ends_at),
            )
        else:
            cur2 = await db.execute(
                """
                INSERT INTO punishments (guild_id, user_id, staff_id, type, reason, evidence, duration_seconds, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(guild_id), int(user_id), int(staff_id), ptype, reason, evidence, duration_seconds, created_at),
            )

        await db.commit()
        return int(cur2.lastrowid)

async def db_revoke_punishment(pid: int, staff_id: int, revoke_reason: str) -> bool:
    revoked_at = _utc_now_iso()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            UPDATE punishments
               SET revoked_by=?, revoked_at=?, revoked_reason=?
             WHERE id=? AND revoked_at IS NULL
            """,
            (int(staff_id), revoked_at, revoke_reason, int(pid)),
        )
        await db.commit()
        return cur.rowcount > 0

async def db_get_punishment_user_id(pid: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM punishments WHERE id=?", (int(pid),))
        row = await cur.fetchone()
        return int(row[0]) if row else None


async def db_count_automod_actions(
    guild_id: int,
    user_id: int,
    rule_name: str,
    within_seconds: int,
) -> int:
    """Conta quantas ações do AutoMod (warnings/timeouts) esse usuário teve nessa regra
    dentro da janela 'within_seconds'. Usa o campo evidence como marcador."""
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=int(within_seconds))).isoformat(timespec="seconds")
    needle = f"AutoMod regra: {rule_name}"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            SELECT COUNT(1)
              FROM warnings
             WHERE guild_id=? AND user_id=? AND revoked_at IS NULL
               AND created_at >= ?
               AND (evidence LIKE ?)
            """,
            (int(guild_id), int(user_id), since, f"%{needle}%"),
        )
        w = await cur.fetchone()
        wcount = int(w[0]) if w else 0

        cur2 = await db.execute(
            """
            SELECT COUNT(1)
              FROM punishments
             WHERE guild_id=? AND user_id=? AND revoked_at IS NULL
               AND type='timeout'
               AND created_at >= ?
               AND (evidence LIKE ?)
            """,
            (int(guild_id), int(user_id), since, f"%{needle}%"),
        )
        p = await cur2.fetchone()
        pcount = int(p[0]) if p else 0

        return wcount + pcount

# =========================
# Views: botões persistentes
# =========================
# custom_id:
# - doom:warn_revoke:<warn_id>
# - doom:timeout_revoke:<pid>

class WarnRevokeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"doom:warn_revoke:(\d+)"):
    def __init__(self, warn_id: int):
        super().__init__(
            discord.ui.Button(
                label="Remover advertência",
                style=discord.ButtonStyle.danger,
                custom_id=f"doom:warn_revoke:{warn_id}",
            )
        )
        self.warn_id = int(warn_id)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return
        await interaction.response.send_modal(RevokeWarnModal(self.warn_id))

class TimeoutRevokeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"doom:timeout_revoke:(\d+)"):
    def __init__(self, punishment_id: int):
        super().__init__(
            discord.ui.Button(
                label="Remover timeout",
                style=discord.ButtonStyle.danger,
                custom_id=f"doom:timeout_revoke:{punishment_id}",
            )
        )
        self.punishment_id = int(punishment_id)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]):
        return cls(int(match.group(1)))

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return
        await interaction.response.send_modal(RevokeTimeoutModal(self.punishment_id))

class WarnLogView(discord.ui.View):
    def __init__(self, warn_id: int):
        super().__init__(timeout=None)
        self.add_item(WarnRevokeButton(warn_id))

class TimeoutLogView(discord.ui.View):
    def __init__(self, punishment_id: int):
        super().__init__(timeout=None)
        self.add_item(TimeoutRevokeButton(punishment_id))

# =========================
# Modals: remover
# =========================

class RevokeWarnModal(discord.ui.Modal, title="Remover Advertência"):
    motivo = discord.ui.TextInput(
        label="Motivo da remoção",
        placeholder="Ex: aplicado por engano / resolvido...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=800,
    )

    def __init__(self, warn_id: int):
        super().__init__()
        self.warn_id = int(warn_id)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            motivo = str(self.motivo.value).strip()
            ok = await db_revoke_warning(self.warn_id, interaction.user.id, motivo)
            if not ok:
                await interaction.edit_original_response(content="❌ Essa advertência já foi removida ou não existe.")
                return

            user_id = await db_get_warning_user_id(self.warn_id)
            member = interaction.guild.get_member(int(user_id)) if user_id else None

            # log
            emb = discord.Embed(
                title="🧾 Advertência removida",
                description=f"**Usuário:** <@{user_id}> (`{user_id}`)\n**Staff:** {interaction.user.mention} (`{interaction.user.id}`)\n**ID:** `{self.warn_id}`",
                color=discord.Color.dark_grey(),
            )
            emb.add_field(name="Motivo", value=motivo[:1024], inline=False)
            emb.set_thumbnail(url=_server_thumb(interaction.guild, interaction.user.display_avatar.url))
            await send_mod_log(interaction.client, interaction.guild, emb)

            # DM (opcional)
            if member:
                await send_dm_embed(
                    member,
                    title="ℹ️ Sua advertência foi removida",
                    description=f"Servidor: **{interaction.guild.name}**\nID: `{self.warn_id}`",
                    motivo=motivo,
                    prova=None,
                    footer="Isso foi feito pela staff.",
                    color=discord.Color.dark_grey(),
                    thumbnail_url=_server_thumb(interaction.guild, member.display_avatar.url),
                )

            await interaction.edit_original_response(content=f"✅ Advertência removida. ID: `{self.warn_id}`")

            await _trigger_punish_dashboard_update(interaction.client, interaction.guild.id)
        except Exception:
            traceback.print_exc()
            await interaction.edit_original_response(content="❌ Erro ao remover advertência.")

class RevokeTimeoutModal(discord.ui.Modal, title="Remover Timeout"):
    motivo = discord.ui.TextInput(
        label="Motivo da remoção",
        placeholder="Ex: aplicado por engano / revisado...",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=800,
    )

    def __init__(self, pid: int):
        super().__init__()
        self.pid = int(pid)

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            motivo = str(self.motivo.value).strip()
            user_id = await db_get_punishment_user_id(self.pid)
            member = interaction.guild.get_member(int(user_id)) if user_id else None

            # remove timeout no discord
            if member:
                try:
                    await member.timeout(None, reason=f"Timeout removido por {interaction.user}: {motivo}")
                except Exception as e:
                    await interaction.edit_original_response(content=f"❌ Não consegui remover timeout no Discord: {type(e).__name__}: {e}")
                    return

            # remove cargo hardcore (se existir)
            if member:
                try:
                    await remove_timeout_hardcore(member)
                except Exception:
                    pass

            ok = await db_revoke_punishment(self.pid, interaction.user.id, motivo)
            if not ok:
                await interaction.edit_original_response(content="❌ Esse timeout já foi removido ou não existe.")
                return

            # log
            emb = discord.Embed(
                title="🧾 Timeout removido",
                description=f"**Usuário:** <@{user_id}> (`{user_id}`)\n**Staff:** {interaction.user.mention} (`{interaction.user.id}`)\n**ID:** `{self.pid}`",
                color=discord.Color.dark_grey(),
            )
            emb.add_field(name="Motivo", value=motivo[:1024], inline=False)
            emb.set_thumbnail(url=_server_thumb(interaction.guild, interaction.user.display_avatar.url))
            await send_mod_log(interaction.client, interaction.guild, emb)

            # DM (opcional)
            if member:
                await send_dm_embed(
                    member,
                    title="ℹ️ Seu timeout foi removido",
                    description=f"Servidor: **{interaction.guild.name}**\nID: `{self.pid}`",
                    motivo=motivo,
                    prova=None,
                    footer="Isso foi feito pela staff.",
                    color=discord.Color.dark_grey(),
                    thumbnail_url=_server_thumb(interaction.guild, member.display_avatar.url),
                )

            await interaction.edit_original_response(content=f"✅ Timeout removido. ID: `{self.pid}`")

            await _trigger_punish_dashboard_update(interaction.client, interaction.guild.id)
        except Exception:
            traceback.print_exc()
            await interaction.edit_original_response(content="❌ Erro ao remover timeout.")

# =========================
# Modals: aplicar punições
# =========================

class AdvertenciaModal(discord.ui.Modal, title="Aplicar Advertência"):
    motivo = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, required=True, max_length=800)
    prova = discord.ui.TextInput(label="Prova (opcional)", style=discord.TextStyle.paragraph, required=False, max_length=800)
    duracao = discord.ui.TextInput(label="Duração (opcional) ex: 2d, 12h, 30m", placeholder="ex: 2d", required=False, max_length=8)

    def __init__(self, bot: commands.Bot, alvo: discord.Member):
        super().__init__()
        self.bot = bot
        self.alvo = alvo

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return
        if self.alvo.bot:
            await interaction.response.send_message("Não aplico em bot.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            motivo = str(self.motivo.value).strip()
            prova = str(self.prova.value).strip() or None
            dur_raw = str(self.duracao.value).strip()
            dur_secs = parse_duration_to_seconds(dur_raw) if dur_raw else None
            if dur_raw and dur_secs is None:
                await interaction.edit_original_response(content="❌ Duração inválida. Use ex: `2d`, `12h`, `30m`.")
                return

            warn_id = await db_add_warning(interaction.guild.id, self.alvo.id, interaction.user.id, motivo, prova, points=1, duration_seconds=dur_secs)

            # LOG
            emb = discord.Embed(
                title="📌 Advertência aplicada",
                description=f"**Usuário:** {self.alvo.mention} (`{self.alvo.id}`)\n**Staff:** {interaction.user.mention} (`{interaction.user.id}`)",
                color=discord.Color.red(),
            )
            emb.add_field(name="ID", value=f"`{warn_id}`", inline=True)
            if dur_secs:
                ends = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=dur_secs)
                emb.add_field(name="Duração", value=f"`{dur_raw}` (até <t:{int(ends.timestamp())}:f>)", inline=False)
            emb.add_field(name="Motivo", value=motivo[:1024], inline=False)
            if prova:
                emb.add_field(name="Prova / Observação", value=prova[:1024], inline=False)
            emb.set_thumbnail(url=_server_thumb(interaction.guild, self.alvo.display_avatar.url))
            await send_mod_log(self.bot, interaction.guild, emb, view=WarnLogView(warn_id))

            # DM
            sent = await send_dm_embed(
                self.alvo,
                title="📌 Você foi advertido(a)",
                description=f"Servidor: **{interaction.guild.name}**\nID: `{warn_id}`" + (f"\nDuração: `{dur_raw}`" if dur_raw else ""),
                motivo=motivo,
                prova=prova,
                footer="Se você achar que foi um engano, fale com a staff.",
                color=discord.Color.red(),
                thumbnail_url=_server_thumb(interaction.guild, self.alvo.display_avatar.url),
            )
            print(f"[MOD][TIMEOUT] DM timeout enviada={sent}")
            if not sent:
                emb_dm = discord.Embed(
                    title="⚠️ DM não entregue (advertência)",
                    description=f"**Usuário:** {self.alvo.mention} (`{self.alvo.id}`)\nO usuário está com DM fechada/bloqueada.",
                    color=discord.Color.yellow(),
                )
                emb_dm.set_thumbnail(url=_server_thumb(interaction.guild, self.alvo.display_avatar.url))
                await send_mod_log(self.bot, interaction.guild, emb_dm)

            await interaction.edit_original_response(content=f"✅ Advertência aplicada. ID: `{warn_id}`")

            await _trigger_punish_dashboard_update(self.bot, interaction.guild.id)
            print(f"[MOD][TIMEOUT] Dashboard refresh agendado guild={interaction.guild.id}")

        except Exception as e:
            traceback.print_exc()
            await interaction.edit_original_response(content=f"❌ Erro ao aplicar advertência: {type(e).__name__}: {e}")

class TimeoutModal(discord.ui.Modal, title="Aplicar Timeout"):
    duracao = discord.ui.TextInput(label="Duração (ex: 10m, 2h, 1d)", placeholder="10m", required=True, max_length=8)
    motivo = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, required=True, max_length=800)
    prova = discord.ui.TextInput(label="Prova (opcional)", style=discord.TextStyle.paragraph, required=False, max_length=800)

    def __init__(self, bot: commands.Bot, alvo: discord.Member):
        super().__init__()
        self.bot = bot
        self.alvo = alvo

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return
        if self.alvo.bot:
            await interaction.response.send_message("Não aplico em bot.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            print(f"[MOD][TIMEOUT] submit alvo={self.alvo.id} staff={interaction.user.id} guild={interaction.guild.id}")
            secs = parse_duration_to_seconds(str(self.duracao.value))
            if not secs or secs < 10:
                await interaction.edit_original_response(content="❌ Duração inválida. Use ex: `10m`, `2h`, `1d`.")
                return

            motivo = str(self.motivo.value).strip()
            prova = str(self.prova.value).strip() or None
            until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=secs)

            # 1) aplica timeout do Discord (bloqueia escrever e interações)
            try:
                await self.alvo.timeout(until, reason=f"Timeout por {interaction.user}: {motivo}")
                print(f"[MOD][TIMEOUT] Discord timeout aplicado em {self.alvo.id}")
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Não consegui aplicar timeout: {type(e).__name__}: {e}")
                return

            # 2) HARDCORE: cargo que bloqueia texto e VOZ (connect/speak/stream)
            try:
                await add_timeout_hardcore(self.alvo)
            except Exception as e:
                # não falha o timeout do Discord; mas avisa no log
                print(f"[MOD] Falha ao aplicar cargo hardcore: {type(e).__name__}: {e}")

            pid: int | None = None
            try:
                pid = await db_add_punishment(interaction.guild.id, self.alvo.id, interaction.user.id, "timeout", motivo, prova, secs)
            except Exception as e:
                print(f"[MOD] Falha ao gravar timeout no banco: {type(e).__name__}: {e}")
                traceback.print_exc()

            # LOG
            emb = discord.Embed(
                title="⏳ Timeout aplicado (hardcore)",
                description=f"**Usuário:** {self.alvo.mention} (`{self.alvo.id}`)\n**Staff:** {interaction.user.mention} (`{interaction.user.id}`)",
                color=discord.Color.red(),
            )
            emb.add_field(name="ID", value=(f"`{pid}`" if pid is not None else "`N/D`"), inline=True)
            emb.add_field(name="Duração", value=f"`{self.duracao.value}` (até <t:{int(until.timestamp())}:f>)", inline=False)
            emb.add_field(name="Motivo", value=motivo[:1024], inline=False)
            if prova:
                emb.add_field(name="Prova / Observação", value=prova[:1024], inline=False)
            emb.add_field(name="Hardcore", value=f"Cargo: `{TIMEOUT_ROLE_NAME}` (texto+voz)", inline=False)
            emb.set_thumbnail(url=_server_thumb(interaction.guild, self.alvo.display_avatar.url))
            await send_mod_log(self.bot, interaction.guild, emb, view=(TimeoutLogView(pid) if pid is not None else None))
            print(f"[MOD][TIMEOUT] Log auditoria enviado pid={pid}")

            # DM
            sent = await send_dm_embed(
                self.alvo,
                title="⏳ Você recebeu um timeout",
                description=(
                    f"Servidor: **{interaction.guild.name}**\n"
                    f"Até: <t:{int(until.timestamp())}:f> (duração: `{self.duracao.value}`)\n" +
                    (f"ID: `{pid}`\n" if pid is not None else "ID: `N/D`\n") +
                    f"Durante o timeout você não poderá escrever nem entrar em call."
                ),
                motivo=motivo,
                prova=prova,
                footer="Se você achar que foi um engano, fale com a staff.",
                color=discord.Color.red(),
                thumbnail_url=_server_thumb(interaction.guild, self.alvo.display_avatar.url),
            )
            if not sent:
                emb_dm = discord.Embed(
                    title="⚠️ DM não entregue (timeout)",
                    description=f"**Usuário:** {self.alvo.mention} (`{self.alvo.id}`)\nO usuário está com DM fechada/bloqueada.",
                    color=discord.Color.yellow(),
                )
                emb_dm.set_thumbnail(url=_server_thumb(interaction.guild, self.alvo.display_avatar.url))
                await send_mod_log(self.bot, interaction.guild, emb_dm)

            await interaction.edit_original_response(
                content=(f"✅ Timeout aplicado. ID: `{pid}`" if pid is not None else "✅ Timeout aplicado. (Sem ID no banco — veja o log do bot)")
            )

            await _trigger_punish_dashboard_update(self.bot, interaction.guild.id)

        except Exception as e:
            traceback.print_exc()
            await interaction.edit_original_response(content=f"❌ Erro ao aplicar timeout: {type(e).__name__}: {e}")

class BanModal(discord.ui.Modal, title="Banir"):
    motivo = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, required=True, max_length=800)
    prova = discord.ui.TextInput(label="Prova (opcional)", style=discord.TextStyle.paragraph, required=False, max_length=800)
    apagar_dias = discord.ui.TextInput(label="Apagar msgs dos últimos X dias (0-7)", placeholder="1", required=False, max_length=1)

    def __init__(self, bot: commands.Bot, alvo: discord.Member):
        super().__init__()
        self.bot = bot
        self.alvo = alvo

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            motivo = str(self.motivo.value).strip()
            prova = str(self.prova.value).strip() or None

            days_raw = str(self.apagar_dias.value).strip()
            delete_days = 1
            if days_raw:
                try:
                    delete_days = max(0, min(7, int(days_raw)))
                except Exception:
                    delete_days = 1

            # DM primeiro
            sent = await send_dm_embed(
                self.alvo,
                title="🔨 Você foi banido(a)",
                description=f"Servidor: **{interaction.guild.name}**",
                motivo=motivo,
                prova=prova,
                footer="Se você acredita que foi um engano, contate a staff.",
                color=discord.Color.dark_red(),
                thumbnail_url=_server_thumb(interaction.guild, self.alvo.display_avatar.url),
            )

            # aplica ban
            try:
                await interaction.guild.ban(
                    self.alvo,
                    reason=f"Ban por {interaction.user}: {motivo}",
                    delete_message_days=delete_days,
                )
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Não consegui banir: {type(e).__name__}: {e}")
                return

            pid = await db_add_punishment(interaction.guild.id, self.alvo.id, interaction.user.id, "ban", motivo, prova, None)

            # LOG
            emb = discord.Embed(
                title="🔨 Ban aplicado",
                description=f"**Usuário:** `{self.alvo}` (`{self.alvo.id}`)\n**Staff:** {interaction.user.mention} (`{interaction.user.id}`)",
                color=discord.Color.dark_red(),
            )
            emb.add_field(name="ID", value=f"`{pid}`", inline=True)
            emb.add_field(name="Motivo", value=motivo[:1024], inline=False)
            if prova:
                emb.add_field(name="Prova / Observação", value=prova[:1024], inline=False)
            emb.set_thumbnail(url=_server_thumb(interaction.guild, self.alvo.display_avatar.url))
            await send_mod_log(self.bot, interaction.guild, emb)

            if not sent:
                emb_dm = discord.Embed(
                    title="⚠️ DM não entregue (ban)",
                    description=f"**Usuário:** `{self.alvo}` (`{self.alvo.id}`)\nO usuário está com DM fechada/bloqueada.",
                    color=discord.Color.yellow(),
                )
                emb_dm.set_thumbnail(url=_server_thumb(interaction.guild, self.alvo.display_avatar.url))
                await send_mod_log(self.bot, interaction.guild, emb_dm)

            await interaction.edit_original_response(content=f"✅ Ban aplicado. ID: `{pid}`")

            await _trigger_punish_dashboard_update(self.bot, interaction.guild.id)

        except Exception as e:
            traceback.print_exc()
            await interaction.edit_original_response(content=f"❌ Erro ao banir: {type(e).__name__}: {e}")

class KickModal(discord.ui.Modal, title="Kickar"):
    motivo = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, required=True, max_length=800)
    prova = discord.ui.TextInput(label="Prova (opcional)", style=discord.TextStyle.paragraph, required=False, max_length=800)

    def __init__(self, bot: commands.Bot, alvo: discord.Member):
        super().__init__()
        self.bot = bot
        self.alvo = alvo

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            motivo = str(self.motivo.value).strip()
            prova = str(self.prova.value).strip() or None

            # DM primeiro
            sent = await send_dm_embed(
                self.alvo,
                title="👢 Você foi removido(a) (kick)",
                description=f"Servidor: **{interaction.guild.name}**",
                motivo=motivo,
                prova=prova,
                footer="Você pode entrar novamente se ainda tiver convite/permissão.",
                color=discord.Color.red(),
                thumbnail_url=_server_thumb(interaction.guild, self.alvo.display_avatar.url),
            )

            try:
                await self.alvo.kick(reason=f"Kick por {interaction.user}: {motivo}")
            except Exception as e:
                await interaction.edit_original_response(content=f"❌ Não consegui kickar: {type(e).__name__}: {e}")
                return

            pid = await db_add_punishment(interaction.guild.id, self.alvo.id, interaction.user.id, "kick", motivo, prova, None)

            # LOG
            emb = discord.Embed(
                title="👢 Kick aplicado",
                description=f"**Usuário:** `{self.alvo}` (`{self.alvo.id}`)\n**Staff:** {interaction.user.mention} (`{interaction.user.id}`)",
                color=discord.Color.red(),
            )
            emb.add_field(name="ID", value=f"`{pid}`", inline=True)
            emb.add_field(name="Motivo", value=motivo[:1024], inline=False)
            if prova:
                emb.add_field(name="Prova / Observação", value=prova[:1024], inline=False)
            emb.set_thumbnail(url=_server_thumb(interaction.guild, self.alvo.display_avatar.url))
            await send_mod_log(self.bot, interaction.guild, emb)

            if not sent:
                emb_dm = discord.Embed(
                    title="⚠️ DM não entregue (kick)",
                    description=f"**Usuário:** `{self.alvo}` (`{self.alvo.id}`)\nO usuário está com DM fechada/bloqueada.",
                    color=discord.Color.yellow(),
                )
                emb_dm.set_thumbnail(url=_server_thumb(interaction.guild, self.alvo.display_avatar.url))
                await send_mod_log(self.bot, interaction.guild, emb_dm)

            await interaction.edit_original_response(content=f"✅ Kick aplicado. ID: `{pid}`")

            await _trigger_punish_dashboard_update(self.bot, interaction.guild.id)

        except Exception as e:
            traceback.print_exc()
            await interaction.edit_original_response(content=f"❌ Erro ao kickar: {type(e).__name__}: {e}")
# =========================
# AutoMod core
# =========================


# =========================
# MENUS DE CONTEXTO (Apps)
# =========================
@app_commands.context_menu(name="Advertir")
async def ctx_advertir(interaction: discord.Interaction, member: discord.Member):
    if not _is_staff(interaction.user): return await interaction.response.send_message("Sem permissão.", ephemeral=True)
    await interaction.response.send_modal(AdvertenciaModal(interaction.client, member))

@app_commands.context_menu(name="Aplicar Timeout")
async def ctx_timeout(interaction: discord.Interaction, member: discord.Member):
    if not _is_staff(interaction.user): return await interaction.response.send_message("Sem permissão.", ephemeral=True)
    await interaction.response.send_modal(TimeoutModal(interaction.client, member))

@app_commands.context_menu(name="Banir")
async def ctx_ban(interaction: discord.Interaction, member: discord.Member):
    if not _is_staff(interaction.user): return await interaction.response.send_message("Sem permissão.", ephemeral=True)
    await interaction.response.send_modal(BanModal(interaction.client, member))

@app_commands.context_menu(name="Kickar")
async def ctx_kick(interaction: discord.Interaction, member: discord.Member):
    if not _is_staff(interaction.user): return await interaction.response.send_message("Sem permissão.", ephemeral=True)
    await interaction.response.send_modal(KickModal(interaction.client, member))

@app_commands.context_menu(name="Ficha")
async def ctx_ficha(interaction: discord.Interaction, member: discord.Member):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
        return await interaction.response.send_message("Sem permissão.", ephemeral=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_columns(db, "warnings", ["duration_seconds INTEGER", "expires_at TEXT", "expired_notified_at TEXT"])
        await _ensure_columns(db, "punishments", ["ends_at TEXT", "end_notified_at TEXT"])
        cur = await db.execute("""
            SELECT id, reason, evidence, created_at, revoked_at, expires_at
              FROM warnings
             WHERE guild_id=? AND user_id=?
             ORDER BY id DESC LIMIT 10
        """, (interaction.guild.id, member.id))
        warns = await cur.fetchall()
        cur = await db.execute("""
            SELECT id, type, reason, evidence, created_at, duration_seconds, revoked_at, ends_at
              FROM punishments
             WHERE guild_id=? AND user_id=?
             ORDER BY id DESC LIMIT 10
        """, (interaction.guild.id, member.id))
        puns = await cur.fetchall()

    emb = discord.Embed(title=f"📄 Ficha de Moderação", description=f"**Usuário:** {member.mention}\n**ID:** `{member.id}`\n━━━━━━━━━━━━━━━━━━━━━━━━━━", color=0xED4245)
    emb.set_thumbnail(url=member.display_avatar.url)

    def _fmt_dt(iso: str | None) -> str:
        if not iso: return "—"
        try:
            ts = int(dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc).timestamp())
            return f"<t:{ts}:f>"
        except Exception: return iso

    if warns:
        lines = []
        for (wid, reason, evidence, created_at, revoked_at, expires_at) in warns:
            status = "Ativa" if not revoked_at else "Removida/Expirada"
            lines.append(f"• `#{wid}` **{status}** • {_fmt_dt(created_at)}\n  > {str(reason)[:120]}")
        emb.add_field(name="📌 Últimas advertências (10)", value="\n".join(lines)[:1024], inline=False)
    else:
        emb.add_field(name="📌 Últimas advertências", value="> Nenhuma registrada.", inline=False)

    if puns:
        lines = []
        for (pid, ptype, reason, evidence, created_at, dur, revoked_at, ends_at) in puns:
            status = "Ativa" if not revoked_at else "Revogada"
            extra = f" • até {_fmt_dt(ends_at)}" if (ptype == "timeout" and ends_at) else ""
            lines.append(f"• `#{pid}` **{ptype}** ({status}) • {_fmt_dt(created_at)}{extra}\n  > {str(reason)[:120]}")
        emb.add_field(name="🛡️ Últimas punições (10)", value="\n".join(lines)[:1024], inline=False)
    else:
        emb.add_field(name="🛡️ Últimas punições", value="> Nenhuma registrada.", inline=False)

    emb = _style_embed(emb, guild=interaction.guild, accent_name="Ficha Criminal")
    await interaction.response.send_message(embed=emb, ephemeral=True)

# =========================
# Cog
# =========================

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._sync_once_task: asyncio.Task | None = None

        # AutoMod caches
        self._am_msg_times: dict[tuple[int, int], deque[tuple[float, int]]] = defaultdict(deque)  # (guild_id,user_id)->[(ts,msg_id)]
        self._am_recent_texts: dict[tuple[int, int], deque[tuple[float, str, int]]] = defaultdict(deque)  # [(ts,norm,msg_id)]
        self._am_last_action: dict[tuple[int, int, str], float] = {}  # cooldown por regra

    def _punish_dash_key(self, guild_id: int) -> str:
        return f"mod_punish_dashboard:{guild_id}"

    async def _automod_delete_messages(self, channel: discord.abc.Messageable, message_ids: list[int]) -> int:
        """Apaga várias mensagens (quando possível) para spam/flood."""
        deleted = 0
        if not message_ids:
            return deleted
        ids = []
        seen = set()
        for mid in message_ids:
            if mid and mid not in seen:
                seen.add(mid)
                ids.append(int(mid))
        # tenta apagar em lote (TextChannel)
        try:
            if isinstance(channel, discord.TextChannel):
                msgs = []
                async for m in channel.history(limit=100):
                    if m.id in seen:
                        msgs.append(m)
                if msgs:
                    try:
                        if len(msgs) == 1:
                            await msgs[0].delete()
                        else:
                            await channel.delete_messages(msgs)
                        return len(msgs)
                    except Exception:
                        # fallback individual
                        for m in msgs:
                            try:
                                await m.delete()
                                deleted += 1
                            except Exception:
                                pass
                        return deleted
        except Exception:
            pass
        return deleted

    async def _automod_apply(
        self,
        message: discord.Message,
        *,
        rule_name: str,
        reason: str,
        info: str,
        base_stage: int = 1,  # 1=adv, 3=timeout
        delete_ids: list[int] | None = None,
    ) -> None:
        """Aplica punição automática com progressão por histórico (janela 12h)."""
        guild = message.guild
        author = message.author
        if guild is None or not isinstance(author, discord.Member):
            return

        # anti-loop por regra
        now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
        cd_key = (guild.id, author.id, rule_name)
        last = self._am_last_action.get(cd_key, 0.0)
        if now_ts - last < 8:
            return
        self._am_last_action[cd_key] = now_ts

        # apaga mensagem(ns)
        ids_to_delete = list(delete_ids or [])
        if message.id not in ids_to_delete:
            ids_to_delete.append(message.id)
        try:
            await self._automod_delete_messages(message.channel, ids_to_delete)
        except Exception:
            pass

        # conta histórico 12h dessa regra
        count_12h = await db_count_automod_actions(guild.id, author.id, rule_name, within_seconds=12 * 3600)

        # progressão:
        # 0 -> stage1
        # 1 -> stage2 (2 ADV + aviso)
        # 2 -> stage3 (timeout 10m)
        # 3+ -> stage4 (timeout 1h)
        stage_by_hist = 1
        if count_12h >= 3:
            stage_by_hist = 4
        elif count_12h == 2:
            stage_by_hist = 3
        elif count_12h == 1:
            stage_by_hist = 2

        stage = max(int(base_stage), int(stage_by_hist))

        # configura ação
        points = 1
        timeout_seconds: int | None = None
        action_txt = "Advertência"
        extra_note = ""

        if stage == 1:
            points = 1
            action_txt = "Advertência"
        elif stage == 2:
            points = 2
            action_txt = "2 Advertências"
            extra_note = "⚠️ Próxima ocorrência em até 12h vira **timeout**."
        elif stage == 3:
            points = 1
            timeout_seconds = 10 * 60
            action_txt = "Timeout 10 minutos"
        else:  # stage 4+
            points = 1
            timeout_seconds = 60 * 60
            action_txt = "Timeout 1 hora"

        staff_id_system = 0
        prova = (
            f"AutoMod regra: {rule_name}\n"
            f"Canal: #{getattr(message.channel, 'name', 'desconhecido')}\n"
            f"Info: {info}\n"
            f"Conteúdo: {message.content[:700]}"
        )

        # sempre registra warning (ADV)
        warn_reason = f"[AutoMod] {reason}"
        if stage == 2:
            warn_reason += " (2 ADV)"
        if extra_note:
            warn_reason += f" | {extra_note}"

        warn_id = await db_add_warning(
            guild.id,
            author.id,
            staff_id_system,
            warn_reason,
            prova,
            points=points,
            duration_seconds=7 * 24 * 3600,  # 7 dias
        )

        pid: int | None = None
        if timeout_seconds:
            try:
                until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(timeout_seconds))
                await author.timeout(until, reason=f"AutoMod ({rule_name}): {reason}")
                try:
                    await add_timeout_hardcore(author)
                except Exception:
                    pass
                pid = await db_add_punishment(
                    guild.id,
                    author.id,
                    staff_id_system,
                    "timeout",
                    f"[AutoMod] {reason}",
                    prova,
                    int(timeout_seconds),
                )
            except Exception as e:
                print(f"[AUTOMOD] Falha timeout: {type(e).__name__}: {e}")

        # LOG
        emb = discord.Embed(
            title="🤖 AutoMod acionado",
            description=f"**Usuário:** {author.mention} (`{author.id}`)\n**Regra:** `{rule_name}`",
            color=discord.Color.red() if timeout_seconds else discord.Color.red(),
        )
        emb.add_field(name="Ação", value=action_txt, inline=False)
        emb.add_field(name="Motivo", value=reason[:1024], inline=False)
        emb.add_field(name="Info", value=info[:1024], inline=False)
        if extra_note:
            emb.add_field(name="Aviso", value=extra_note, inline=False)
        emb.add_field(name="Warn ID", value=f"`{warn_id}`", inline=True)
        if pid:
            emb.add_field(name="Timeout ID", value=f"`{pid}`", inline=True)
        emb.add_field(name="Mensagem (cortada)", value=(message.content[:1000] or "—"), inline=False)
        emb.set_thumbnail(url=_server_thumb(guild, author.display_avatar.url))
        await send_mod_log(self.bot, guild, emb)

        # DM
        try:
            desc = (
                f"Servidor: **{guild.name}**\n"
                f"Regra: `{rule_name}`\n"
                f"Ação: **{action_txt}**\n"
                + (f"\n{extra_note}" if extra_note else "")
            )
            await send_dm_embed(
                author,
                title="🤖 AutoMod — ação aplicada",
                description=desc.strip(),
                motivo=reason,
                prova=(info if info else None),
                footer="Se achar que foi engano, fale com a staff.",
                color=discord.Color.red() if timeout_seconds else discord.Color.red(),
                thumbnail_url=_server_thumb(guild, author.display_avatar.url),
            )
        except Exception:
            pass

        try:
            self.schedule_punish_dashboard_update(delay=0.1)
        except Exception:
            pass
        await _trigger_punish_dashboard_update(self.bot, guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            if not message.guild or not isinstance(message.author, discord.Member):
                return
            if message.author.bot:
                return
            if GUILD_ID and message.guild.id != GUILD_ID:
                return
            if _is_staff(message.author):
                return

            content = (message.content or "").strip()
            if not content:
                return

            now = dt.datetime.now(dt.timezone.utc).timestamp()
            key = (message.guild.id, message.author.id)

            # Anti-spam: 5 mensagens em 10s (ADV)
            dq = self._am_msg_times[key]
            dq.append((now, message.id))
            while dq and (now - dq[0][0]) > 10:
                dq.popleft()
            if len(dq) >= 5:
                spam_ids = [mid for _, mid in dq]
                dq.clear()  # limpa janela para não re-disparar em cascata
                await self._automod_apply(
                    message,
                    rule_name="anti_spam",
                    reason="Muitas mensagens em poucos segundos (spam).",
                    info="Limite: 5 mensagens em 10s.",
                    base_stage=1,
                    delete_ids=spam_ids,
                )
                return

            # Anti-flood: texto repetido 3x em 20s (TIMEOUT)
            norm = _normalize_text(content)
            tdq = self._am_recent_texts[key]
            tdq.append((now, norm, message.id))
            while tdq and (now - tdq[0][0]) > 20:
                tdq.popleft()
            flood_ids = [mid for _, t, mid in tdq if t and t == norm]
            same_count = len(flood_ids)
            if len(norm) >= 6 and same_count >= 3:
                # remove os repetidos da janela pra evitar re-disparo na próxima mensagem
                self._am_recent_texts[key] = deque([(ts, t, mid) for ts, t, mid in tdq if t != norm])
                await self._automod_apply(
                    message,
                    rule_name="anti_flood",
                    reason="Mensagem repetida várias vezes (flood).",
                    info="Limite: repetir o mesmo texto 3x em 20s.",
                    base_stage=3,
                    delete_ids=flood_ids,
                )
                return

            # Anti-link (ADV com info)
            if URL_RE.search(content):
                if message.channel.id not in AUTOMOD_ALLOWED_LINK_CHANNELS:
                    await self._automod_apply(
                        message,
                        rule_name="anti_link",
                        reason="Links só são permitidos em canais específicos.",
                        info="Motivo: evitar spam/golpes. Poste links apenas nos canais liberados.",
                        base_stage=1,
                    )
                    return

            # Anti-invite (ADV com info)
            if INVITE_RE.search(content):
                await self._automod_apply(
                    message,
                    rule_name="anti_invite",
                    reason="Convites do Discord não são permitidos.",
                    info="Detectado: discord.gg / discord.com/invite",
                    base_stage=1,
                )
                return

            # Caps: >70% em msg longa (ADV com info)
            if len(content) >= 12 and _caps_ratio(content) >= 0.70:
                await self._automod_apply(
                    message,
                    rule_name="anti_caps",
                    reason="Uso excessivo de CAPS LOCK.",
                    info="Regra: mais de 70% das letras em maiúsculo (mensagem longa).",
                    base_stage=1,
                )
                return

            # Palavras proibidas (TIMEOUT com info)
            low = content.lower()
            hit = next((w for w in AUTOMOD_BAD_WORDS if w and w in low), None)
            if hit:
                await self._automod_apply(
                    message,
                    rule_name="bad_words",
                    reason="Uso de palavra proibida.",
                    info=f"Detectado termo: `{hit}`",
                    base_stage=3,
                )
                return

        except Exception as e:
            print(f"[AUTOMOD] ERRO on_message: {type(e).__name__}: {e}")

    async def cog_load(self):
        # schema local
        await ensure_warning_duration_columns()
        await ensure_punishment_timeout_columns()

        # dynamic items persistentes
        self.bot.add_dynamic_items(WarnRevokeButton, TimeoutRevokeButton)

        # loops
        if not self._warn_expiry_loop.is_running():
            self._warn_expiry_loop.start()
        if not self._timeout_end_loop.is_running():
            self._timeout_end_loop.start()
        # Dashboard de punições agora é event-driven (sem envio periódico).
        # Ele só atualiza quando há mudança de punição/timeout e recria se apagarem a mensagem/thread.
        # Mantemos apenas o bootstrap inicial ao carregar o cog.
        self.schedule_punish_dashboard_update(delay=2.0)

        # sync uma vez após ready (remove comandos velhos tipo "Aplicar Advertência")
        if not self._sync_once_task:
            self._sync_once_task = self.bot.loop.create_task(self._sync_tree_once())

    def cog_unload(self):
        try:
            self._warn_expiry_loop.cancel()
        except Exception:
            pass
        try:
            self._timeout_end_loop.cancel()
        except Exception:
            pass
        try:
            self._punish_dashboard_heartbeat.cancel()
        except Exception:
            pass
        if self._sync_once_task:
            self._sync_once_task.cancel()
            self._sync_once_task = None

    async def _sync_tree_once(self):
        await self.bot.wait_until_ready()
        try:
            if GUILD_ID:
                g = discord.Object(id=GUILD_ID)
                self.bot.tree.clear_commands(guild=g) # Limpa os velhos

                # Adiciona os novos no Servidor (Instantâneo)
                self.bot.tree.add_command(ctx_advertir, guild=g)
                self.bot.tree.add_command(ctx_timeout, guild=g)
                self.bot.tree.add_command(ctx_ban, guild=g)
                self.bot.tree.add_command(ctx_kick, guild=g)
                self.bot.tree.add_command(ctx_ficha, guild=g)

                await self.bot.tree.sync(guild=g)
                print("[MOD] Apps de Moderação ativados no Servidor!")
            else:
                self.bot.tree.clear_commands(guild=None)

                # Adiciona globalmente se o GUILD_ID não estiver no .env
                self.bot.tree.add_command(ctx_advertir)
                self.bot.tree.add_command(ctx_timeout)
                self.bot.tree.add_command(ctx_ban)
                self.bot.tree.add_command(ctx_kick)
                self.bot.tree.add_command(ctx_ficha)

                await self.bot.tree.sync()
                print("[MOD] Apps de Moderação ativados Globalmente!")
        except Exception as e:
            print(f"[MOD] Erro ao sincronizar menus: {e}")

    # =========================
    # Dashboard de punições (tempo real) - REVISÃO PREMIUM
    # =========================
    def schedule_punish_dashboard_update(self, delay: float = 1.0) -> None:
        task = getattr(self, "_punish_dash_debounce", None)
        if task and not task.done():
            task.cancel()
        async def _run():
            try:
                await asyncio.sleep(max(0.0, float(delay)))
                await self.update_punish_dashboard()
            except asyncio.CancelledError:
                return
        self._punish_dash_debounce = asyncio.create_task(_run())

    async def _collect_punish_dashboard_counts(self, guild: discord.Guild) -> dict[str, int]:
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        async with aiosqlite.connect(DB_PATH) as db:
            async def q(sql: str, params: tuple) -> int:
                cur = await db.execute(sql, params)
                row = await cur.fetchone()
                return int((row[0] if row else 0) or 0)

            warnings_total = await q("SELECT COUNT(*) FROM warnings WHERE guild_id=?", (guild.id,))
            warnings_active = await q("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)", (guild.id, now_iso))
            warnings_expired = await q("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND revoked_at IS NOT NULL AND revoked_by=0", (guild.id,))
            warnings_revoked = await q("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND revoked_at IS NOT NULL AND revoked_by != 0", (guild.id,))
            timeouts_total = await q("SELECT COUNT(*) FROM punishments WHERE guild_id=? AND type='timeout'", (guild.id,))
            timeouts_db_active = await q("SELECT COUNT(*) FROM punishments WHERE guild_id=? AND type='timeout' AND revoked_at IS NULL AND (ends_at IS NULL OR ends_at > ?)", (guild.id, now_iso))
            kicks_total = await q("SELECT COUNT(*) FROM punishments WHERE guild_id=? AND type='kick'", (guild.id,))
            bans_total = await q("SELECT COUNT(*) FROM punishments WHERE guild_id=? AND type='ban'", (guild.id,))

        timeouts_live = sum(1 for m in guild.members if getattr(m, "communication_disabled_until", None) and m.communication_disabled_until > dt.datetime.now(dt.timezone.utc))

        return {
            "warnings_total": warnings_total,
            "warnings_active": warnings_active,
            "warnings_expired": warnings_expired,
            "warnings_revoked": warnings_revoked,
            "timeouts_total": timeouts_total,
            "timeouts_active": max(timeouts_db_active, timeouts_live),
            "kicks_total": kicks_total,
            "bans_total": bans_total,
        }

    async def update_punish_dashboard(self) -> None:
        try:
            await self.bot.wait_until_ready()
            guild = self.bot.get_guild(GUILD_ID) if GUILD_ID else None
            if not guild:
                return

            if not hasattr(self, "_punish_dash_lock"):
                self._punish_dash_lock = asyncio.Lock()

            async with self._punish_dash_lock:
                target_id = int(os.getenv("MOD_PUNISH_DASHBOARD_CHANNEL_ID", "0")) or int(os.getenv("STAFF_DASHBOARD_CHANNEL_ID", "0"))
                channel = guild.get_channel(target_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(target_id)
                    except Exception:
                        channel = None
                if not channel:
                    return

                post_name = "🛡️ Dashboard Punições"
                counts = await self._collect_punish_dashboard_counts(guild)

                # --- 1. CONSTRUÇÃO DA EMBED PREMIUM ---
                ping_ms = int(self.bot.latency * 1000)
                w_max = max(10, counts["warnings_total"], counts["warnings_active"])
                t_max = max(10, counts["timeouts_total"], counts["timeouts_active"])

                desc = (
                    "**PAINEL DE PUNIÇÕES**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Visão geral do sistema de segurança e moderação.\n"
                    f"Atualizado: <t:{int(dt.datetime.now().timestamp())}:T> • Ping: **{ping_ms}ms**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                )

                emb = discord.Embed(title="🛡️ CENTRAL DE MODERAÇÃO", description=desc, color=0xED4245)
                bot_avatar = guild.me.display_avatar.url if guild and guild.me else None
                emb.set_author(name="Dashboard de Punimentos", icon_url=bot_avatar) if bot_avatar else emb.set_author(name="Dashboard de Punimentos")
                emb.set_footer(text="Doom Project Mod Hub • Segurança Oficial")
                if guild.icon:
                    emb.set_thumbnail(url=guild.icon.url)

                emb.add_field(
                    name="📌 ADVERTÊNCIAS",
                    value=(
                        f"> `Ativas:` {_bar(counts['warnings_active'], w_max)} **{counts['warnings_active']}**\n"
                        f"> `Total: ` {_bar(counts['warnings_total'], w_max)} **{counts['warnings_total']}**\n"
                        f"> `Exp/Rem:` **{counts['warnings_expired']}** expiradas / **{counts['warnings_revoked']}** removidas"
                    ),
                    inline=False,
                )
                emb.add_field(
                    name="🛡️ PUNIÇÕES RÍGIDAS",
                    value=(
                        f"> `Timeouts:` {_bar(counts['timeouts_active'], t_max)} **{counts['timeouts_active']}** ativos\n"
                        f"> `Total:   ` **{counts['timeouts_total']}**\n"
                        f"> `Ações:   ` **{counts['kicks_total']}** Kicks • **{counts['bans_total']}** Bans"
                    ),
                    inline=False,
                )
                emb.add_field(
                    name="🔎 CONSULTA RÁPIDA",
                    value="```yaml\nUtilize o método (clique na pessoa, botao direito, ficha e analise suas punições) para consultar o histórico completo de um membro.```",
                    inline=False
                )

                cfg_key = self._punish_dash_key(guild.id)

                # --- 2. TRAVA E PUBLICAÇÃO (ANTI-DUPLICATAS) ---
                if isinstance(channel, discord.ForumChannel):
                    target_thread: discord.Thread | None = None
                    target_msg: discord.Message | None = None

                    raw = await get_config(cfg_key)
                    saved_thread_id = 0
                    saved_msg_id = 0
                    if raw:
                        try:
                            parts = [p.strip() for p in str(raw).split(",")]
                            if len(parts) >= 2:
                                saved_thread_id = int(parts[0] or 0)
                                saved_msg_id = int(parts[1] or 0)
                        except Exception:
                            saved_thread_id = 0
                            saved_msg_id = 0

                    # 1) tenta recuperar pela config
                    if saved_thread_id:
                        try:
                            ch = guild.get_thread(saved_thread_id)
                            if ch is None:
                                ch = await self.bot.fetch_channel(saved_thread_id)
                            if isinstance(ch, discord.Thread):
                                target_thread = ch
                                if target_thread.archived:
                                    try:
                                        await target_thread.edit(archived=False)
                                    except Exception:
                                        pass
                        except Exception:
                            target_thread = None

                    # 2) fallback: procura nos tópicos ativos
                    if not target_thread:
                        for thread in channel.threads:
                            if thread.name == post_name:
                                target_thread = thread
                                break

                    # 3) fallback: procura arquivados
                    if not target_thread:
                        try:
                            async for thread in channel.archived_threads(limit=20):
                                if thread.name == post_name:
                                    try:
                                        await thread.edit(archived=False)
                                    except Exception:
                                        pass
                                    target_thread = thread
                                    break
                        except Exception:
                            pass

                    if target_thread:
                        # tenta buscar a msg salva
                        if saved_msg_id:
                            try:
                                target_msg = await target_thread.fetch_message(saved_msg_id)
                            except Exception:
                                target_msg = None

                        # fallback: procura msg do bot
                        if not target_msg:
                            async for m in target_thread.history(limit=10):
                                if m.author.id == self.bot.user.id:
                                    target_msg = m
                                    break

                        if target_msg:
                            await target_msg.edit(embed=emb)
                            await set_config(cfg_key, f"{target_thread.id},{target_msg.id}")
                        else:
                            new_msg = await target_thread.send(embed=emb)
                            await set_config(cfg_key, f"{target_thread.id},{new_msg.id}")
                    else:
                        # O tópico não existe! Cria do zero.
                        created = await channel.create_thread(name=post_name, embed=emb)

                        created_thread = getattr(created, "thread", None)
                        created_message = getattr(created, "message", None)

                        if isinstance(created, discord.Thread):
                            created_thread = created

                        if created_thread and created_message:
                            await set_config(cfg_key, f"{created_thread.id},{created_message.id}")
                        elif created_thread:
                            bot_msg = None
                            async for m in created_thread.history(limit=10):
                                if m.author.id == self.bot.user.id:
                                    bot_msg = m
                                    break
                            await set_config(cfg_key, f"{created_thread.id},{bot_msg.id if bot_msg else 0}")

                else:
                    # Se não for Fórum (Canal de texto normal)
                    raw = await get_config(cfg_key)
                    saved_msg_id = 0
                    if raw:
                        try:
                            parts = [p.strip() for p in str(raw).split(",")]
                            if len(parts) >= 2:
                                saved_msg_id = int(parts[1] or 0)
                        except Exception:
                            saved_msg_id = 0

                    if saved_msg_id:
                        try:
                            msg = await channel.fetch_message(saved_msg_id)
                            await msg.edit(embed=emb)
                            await set_config(cfg_key, f"{channel.id},{msg.id}")
                            return
                        except Exception:
                            pass

                    msg = await channel.send(embed=emb)
                    await set_config(cfg_key, f"{channel.id},{msg.id}")

        except Exception as e:
            print(f"[MOD] ERRO dashboard punições: {e}")

    @tasks.loop(hours=24)
    async def _punish_dashboard_heartbeat(self):
        return

    @_punish_dashboard_heartbeat.before_loop
    async def _before_punish_dashboard_heartbeat(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        try:
            if not payload.guild_id:
                return
            if GUILD_ID and payload.guild_id != GUILD_ID:
                return

            raw = await get_config(self._punish_dash_key(payload.guild_id))
            if not raw:
                return

            parts = [p.strip() for p in str(raw).split(",")]
            if len(parts) < 2:
                return

            saved_dest_id = int(parts[0] or 0)
            saved_msg_id = int(parts[1] or 0)

            if saved_msg_id and payload.message_id == saved_msg_id:
                await set_config(self._punish_dash_key(payload.guild_id), f"{saved_dest_id},0")
                self.schedule_punish_dashboard_update(delay=0.4)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if GUILD_ID and guild.id != GUILD_ID:
            return
        self.schedule_punish_dashboard_update(delay=0.2)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if GUILD_ID and guild.id != GUILD_ID:
            return
        self.schedule_punish_dashboard_update(delay=0.2)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """
        Listener único de member update:
        - atualiza dashboard de punições em tempo real quando timeout muda
        - quando timeout termina, remove cargo hardcore, envia DM e log
        """
        try:
            if not after.guild:
                return
            if GUILD_ID and after.guild.id != GUILD_ID:
                return

            b = getattr(before, "communication_disabled_until", None)
            a = getattr(after, "communication_disabled_until", None)

            if b != a:
                self.schedule_punish_dashboard_update(delay=0.1)

            if b and not a:
                try:
                    await remove_timeout_hardcore(after)
                except Exception:
                    pass

                try:
                    await send_dm_embed(
                        after,
                        title="🔊 Seu mute/timeout acabou",
                        description=f"Servidor: **{after.guild.name}**\nVocê já pode falar normalmente novamente.",
                        motivo="Timeout expirado automaticamente.",
                        prova=None,
                        footer="Aviso automático.",
                        color=discord.Color.green(),
                        thumbnail_url=_server_thumb(after.guild, after.display_avatar.url),
                    )
                except Exception:
                    pass

                try:
                    emb = discord.Embed(
                        title="✅ Timeout finalizado automaticamente",
                        description=f"**Usuário:** {after.mention} (`{after.id}`)",
                        color=discord.Color.green(),
                    )
                    emb.set_thumbnail(url=_server_thumb(after.guild, after.display_avatar.url))
                    await send_mod_log(self.bot, after.guild, emb)
                except Exception:
                    pass

                self.schedule_punish_dashboard_update(delay=0.2)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        """Se apagarem o post/thread do dashboard de punições, recria automaticamente."""
        try:
            if not payload.guild_id:
                return
            if GUILD_ID and payload.guild_id != GUILD_ID:
                return
            raw = await get_config(self._punish_dash_key(payload.guild_id))
            if not raw:
                return
            parts = [p.strip() for p in str(raw).split(",")]
            if len(parts) < 2:
                return
            saved_dest_id = int(parts[0] or 0)
            deleted_thread_id = int(getattr(payload, "thread_id", 0) or 0)
            if deleted_thread_id and deleted_thread_id == saved_dest_id:
                await set_config(self._punish_dash_key(payload.guild_id), "0,0")
                self.schedule_punish_dashboard_update(delay=0.4)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Se criarem canal novo, aplica o overwrite do cargo de timeout (se existir)."""
        try:
            if not channel.guild:
                return
            role = discord.utils.get(channel.guild.roles, name=TIMEOUT_ROLE_NAME)
            if not role:
                return
            try:
                await channel.set_permissions(
                    role,
                    send_messages=False,
                    add_reactions=False,
                    speak=False,
                    stream=False,
                    connect=False,
                    send_messages_in_threads=False,
                    create_public_threads=False,
                    create_private_threads=False,
                    reason="Timeout hardcore (canal novo)",
                )
            except Exception:
                pass
        except Exception:
            pass

    @tasks.loop(minutes=1)
    async def _warn_expiry_loop(self):
        """Revoga advertências com duração quando expiram e notifica (DM + log)."""
        try:
            now_iso = _utc_now_iso()
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    """
                    SELECT id, guild_id, user_id, reason, evidence, expires_at
                      FROM warnings
                     WHERE expires_at IS NOT NULL
                       AND expires_at <= ?
                       AND revoked_at IS NULL
                       AND (expired_notified_at IS NULL)
                    ORDER BY expires_at ASC
                    LIMIT 25
                    """,
                    (now_iso,),
                )
                rows = await cur.fetchall()

                if not rows:
                    return

                # marca revogado (auto)
                for (wid, guild_id, user_id, reason, evidence, expires_at) in rows:
                    await db.execute(
                        """
                        UPDATE warnings
                           SET revoked_by=?, revoked_at=?, revoked_reason=?, expired_notified_at=?
                         WHERE id=? AND revoked_at IS NULL
                        """,
                        (0, now_iso, "Advertência expirada automaticamente", now_iso, int(wid)),
                    )
                await db.commit()

            # notificações fora
            for (wid, guild_id, user_id, reason, evidence, expires_at) in rows:
                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    continue

                # DM
                try:
                    user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                    ts = int(dt.datetime.fromisoformat(expires_at).replace(tzinfo=dt.timezone.utc).timestamp())
                    await send_dm_embed(
                        user,
                        title="✅ Sua advertência expirou",
                        description=f"Servidor: **{guild.name}**\nID: `{wid}`\nExpirou em: <t:{ts}:f>",
                        motivo=str(reason) if reason else "—",
                        prova=str(evidence) if evidence else None,
                        footer="Isso foi automático.",
                        color=discord.Color.green(),
                        thumbnail_url=_server_thumb(guild, user.display_avatar.url),
                    )
                except Exception:
                    pass

                # log
                try:
                    ts = int(dt.datetime.fromisoformat(expires_at).replace(tzinfo=dt.timezone.utc).timestamp())
                    emb = discord.Embed(
                        title="⏱️ Advertência expirada automaticamente",
                        description=f"**Usuário:** <@{user_id}> (`{user_id}`)\n**ID:** `{wid}`\n**Expirou em:** <t:{ts}:f>",
                        color=discord.Color.green(),
                    )
                    emb.add_field(name="Motivo original", value=(str(reason)[:1024] if reason else "—"), inline=False)
                    if evidence:
                        emb.add_field(name="Prova / Observação", value=str(evidence)[:1024], inline=False)
                    emb.set_thumbnail(url=_server_thumb(guild))
                    await send_mod_log(self.bot, guild, emb)
                except Exception as e:
                    print(f"[MOD] Falha ao logar expiração de advertência: {type(e).__name__}: {e}")

        except Exception:
            traceback.print_exc()

    @_warn_expiry_loop.before_loop
    async def _before_warn_expiry_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1)
    async def _timeout_end_loop(self):
        """Detecta timeouts encerrados e notifica (DM + log) + remove cargo hardcore."""
        try:
            now_iso = _utc_now_iso()
            async with aiosqlite.connect(DB_PATH) as db:
                await _ensure_columns(db, "punishments", [
                    "ends_at TEXT",
                    "end_notified_at TEXT",
                ])

                cur = await db.execute(
                    """
                    SELECT id, guild_id, user_id, ends_at
                      FROM punishments
                     WHERE type='timeout'
                       AND ends_at IS NOT NULL
                       AND ends_at <= ?
                       AND revoked_at IS NULL
                       AND (end_notified_at IS NULL)
                    ORDER BY ends_at ASC
                    LIMIT 25
                    """,
                    (now_iso,),
                )
                rows = await cur.fetchall()
                if not rows:
                    return

                for pid, guild_id, user_id, ends_at in rows:
                    guild = self.bot.get_guild(int(guild_id))
                    if not guild:
                        continue

                    member = guild.get_member(int(user_id))
                    if member is None:
                        try:
                            member = await guild.fetch_member(int(user_id))
                        except Exception:
                            member = None

                    if member:
                        # remove timeout do discord se ainda existir (safety)
                        try:
                            if member.communication_disabled_until:
                                await member.timeout(None, reason="Timeout expirado (auto).")
                        except Exception:
                            pass

                        # remove cargo hardcore (e limpa se ninguém mais usar)
                        try:
                            await remove_timeout_hardcore(member)
                        except Exception:
                            pass

                        # DM
                        try:
                            await send_dm_embed(
                                member,
                                title="🔊 Seu mute/timeout acabou",
                                description=f"Servidor: **{guild.name}**\nVocê já pode falar normalmente novamente.",
                                motivo="Timeout expirado automaticamente.",
                                prova=None,
                                footer="Aviso automático.",
                                color=discord.Color.green(),
                                thumbnail_url=_server_thumb(guild, member.display_avatar.url),
                            )
                        except Exception:
                            pass

                        # log
                        try:
                            emb2 = discord.Embed(
                                title="✅ Timeout finalizado automaticamente",
                                description=f"**Usuário:** {member.mention} (`{member.id}`)\nID: `{pid}`",
                                color=discord.Color.green(),
                            )
                            emb2.set_thumbnail(url=_server_thumb(guild, member.display_avatar.url))
                            await send_mod_log(self.bot, guild, emb2)
                        except Exception:
                            pass

                    # marca notificado
                    await db.execute(
                        "UPDATE punishments SET end_notified_at=? WHERE id=? AND end_notified_at IS NULL",
                        (_utc_now_iso(), int(pid)),
                    )
                await db.commit()

            if rows:
                self.schedule_punish_dashboard_update(delay=0.2)

        except Exception as e:
            print(f"[MOD] ERRO _timeout_end_loop: {type(e).__name__}: {e}")

    @_timeout_end_loop.before_loop
    async def _before_timeout_end_loop(self):
        await self.bot.wait_until_ready()

# =========================
# setup (sem duplicação!)
# =========================
async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))