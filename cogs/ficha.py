from __future__ import annotations

import os
import datetime as dt

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

from utils.db import DB_PATH

GUILD_ID = int(os.getenv("GUILD_ID", "0"))

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

def _iso_to_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tz.timezone.utc)
        return int(d.timestamp())
    except Exception:
        return None

def _fmt_ts(iso: str | None) -> str:
    ts = _iso_to_ts(iso)
    return f"<t:{ts}:f>" if ts else "—"

def _short(s: str | None, n: int = 48) -> str:
    if not s:
        return "—"
    s = str(s).replace("\n", " ").strip()
    return (s[: n - 1] + "…") if len(s) > n else s

# =========================
# DB fetch
# =========================

async def fetch_ficha(guild_id: int, user_id: int):
    now_iso = _utc_now_iso()

    async with aiosqlite.connect(DB_PATH) as db:
        # WARNINGS counters
        cur = await db.execute(
            """
            SELECT
              SUM(CASE WHEN revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?) THEN 1 ELSE 0 END) AS active,
              SUM(CASE WHEN expires_at IS NOT NULL AND expires_at <= ? THEN 1 ELSE 0 END) AS expired,
              SUM(CASE WHEN revoked_at IS NOT NULL THEN 1 ELSE 0 END) AS revoked,
              COUNT(*) AS total
            FROM warnings
            WHERE guild_id=? AND user_id=?
            """,
            (now_iso, now_iso, int(guild_id), int(user_id)),
        )
        w = await cur.fetchone() or (0, 0, 0, 0)

        cur = await db.execute(
            """
            SELECT id, reason, evidence, created_at, expires_at, revoked_at, revoked_reason
            FROM warnings
            WHERE guild_id=? AND user_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (int(guild_id), int(user_id)),
        )
        warn_rows = await cur.fetchall()

        # PUNISHMENTS counters
        cur = await db.execute(
            """
            SELECT
              SUM(CASE WHEN type='timeout' AND revoked_at IS NULL AND (ends_at IS NULL OR ends_at > ?) THEN 1 ELSE 0 END) AS timeout_active,
              SUM(CASE WHEN type='timeout' THEN 1 ELSE 0 END) AS timeout_total,
              SUM(CASE WHEN type='kick' THEN 1 ELSE 0 END) AS kick_total,
              SUM(CASE WHEN type='ban' THEN 1 ELSE 0 END) AS ban_total
            FROM punishments
            WHERE guild_id=? AND user_id=?
            """,
            (now_iso, int(guild_id), int(user_id)),
        )
        p = await cur.fetchone() or (0, 0, 0, 0)

        cur = await db.execute(
            """
            SELECT id, type, reason, evidence, duration_seconds, created_at, ends_at, revoked_at, revoked_reason
            FROM punishments
            WHERE guild_id=? AND user_id=?
            ORDER BY id DESC
            LIMIT 10
            """,
            (int(guild_id), int(user_id)),
        )
        pun_rows = await cur.fetchall()

    return w, warn_rows, p, pun_rows

# =========================
# Embed
# =========================

def build_ficha_embed(guild: discord.Guild, target: discord.abc.User, w, warn_rows, p, pun_rows) -> discord.Embed:
    w_active, w_expired, w_revoked, w_total = [int(x or 0) for x in w]
    t_active, t_total, k_total, b_total = [int(x or 0) for x in p]

    emb = discord.Embed(
        title="📋 Ficha disciplinar",
        description=f"**Usuário:** {target.mention} (`{target.id}`)\n**Servidor:** **{guild.name}**",
        color=discord.Color.blurple(),
    )
    emb.set_thumbnail(url=target.display_avatar.url)

    emb.add_field(
        name="⚠️ Advertências",
        value=(
            f"Ativas: **{w_active}**\n"
            f"Expiradas: **{w_expired}**\n"
            f"Removidas: **{w_revoked}**\n"
            f"Total: **{w_total}**"
        ),
        inline=True,
    )
    emb.add_field(
        name="🔨 Punições",
        value=(
            f"Timeouts ativos: **{t_active}**\n"
            f"Timeouts total: **{t_total}**\n"
            f"Kicks: **{k_total}**\n"
            f"Bans: **{b_total}**"
        ),
        inline=True,
    )

    if warn_rows:
        lines = []
        for (wid, reason, _evidence, created_at, expires_at, revoked_at, _revoked_reason) in warn_rows[:5]:
            status = "ativa"
            if revoked_at:
                status = "removida"
            elif expires_at and str(expires_at) <= _utc_now_iso():
                status = "expirada"
            lines.append(f"`W{wid}` • **{status}** • {_fmt_ts(created_at)} • {_short(reason)}")
        emb.add_field(name="🧾 Últimas advertências", value="\n".join(lines), inline=False)

    if pun_rows:
        lines = []
        for (pid, ptype, reason, _evidence, dur, created_at, ends_at, revoked_at, _revoked_reason) in pun_rows[:5]:
            extra = ""
            if ptype == "timeout" and ends_at:
                extra = f" • até {_fmt_ts(ends_at)}"
            if revoked_at:
                extra += " • removida"
            if dur and ptype == "timeout":
                extra += f" • {int(dur)}s"
            lines.append(f"`P{pid}` • **{ptype}** • {_fmt_ts(created_at)}{extra} • {_short(reason)}")
        emb.add_field(name="🧾 Últimas punições", value="\n".join(lines), inline=False)

    emb.set_footer(text="Visível apenas para a staff.")
    return emb



class FichaModal(discord.ui.Modal, title="Ficha disciplinar (visualização)"):
    def __init__(self, guild: discord.Guild, target: discord.abc.User, w, warn_rows, p, pun_rows):
        super().__init__(timeout=180)

        w_active, w_expired, w_revoked, w_total = [int(x or 0) for x in w]
        t_active, t_total, k_total, b_total = [int(x or 0) for x in p]

        resumo = (
            f"Usuário: {target} ({target.id})\n"
            f"Servidor: {guild.name}\n"
            f"Warns: ativas {w_active} | expiradas {w_expired} | removidas {w_revoked} | total {w_total}\n"
            f"Punições: timeouts ativos {t_active} | timeouts total {t_total} | kicks {k_total} | bans {b_total}"
        )

        warns_txt = "Sem advertências."
        if warn_rows:
            linhas = []
            for (wid, reason, _evidence, created_at, expires_at, revoked_at, _revoked_reason) in warn_rows[:3]:
                status = "ativa"
                if revoked_at:
                    status = "removida"
                elif expires_at and str(expires_at) <= _utc_now_iso():
                    status = "expirada"
                linhas.append(f"W{wid} | {status} | {_short(reason, 70)}")
            warns_txt = "\n".join(linhas)

        puns_txt = "Sem punições."
        if pun_rows:
            linhas = []
            for (pid, ptype, reason, _evidence, dur, _created_at, ends_at, revoked_at, _revoked_reason) in pun_rows[:3]:
                extra = []
                if ptype == "timeout" and ends_at:
                    extra.append(f"até {_fmt_ts(ends_at)}")
                if revoked_at:
                    extra.append("removida")
                if dur and ptype == "timeout":
                    extra.append(f"{int(dur)}s")
                extras = f" | {' / '.join(extra)}" if extra else ""
                linhas.append(f"P{pid} | {ptype}{extras} | {_short(reason, 60)}")
            puns_txt = "\n".join(linhas)

        aviso = "Visualização apenas. O botão Enviar do Discord é obrigatório no modal e não salva nada."

        self.resumo = discord.ui.TextInput(
            label="Resumo",
            default=resumo[:4000],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000,
        )
        self.warns = discord.ui.TextInput(
            label="Últimas advertências",
            default=warns_txt[:4000],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000,
        )
        self.puns = discord.ui.TextInput(
            label="Últimas punições",
            default=puns_txt[:4000],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000,
        )
        self.info = discord.ui.TextInput(
            label="Observação",
            default=aviso[:4000],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=4000,
        )

        self.add_item(self.resumo)
        self.add_item(self.warns)
        self.add_item(self.puns)
        self.add_item(self.info)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "📋 Ficha é apenas visualização. Nada foi salvo/alterado.",
            ephemeral=True,
        )

# =========================
# Slash + Apps commands
# =========================


@app_commands.command(name="ficha", description="Mostra a ficha disciplinar de um usuário (staff)")
@app_commands.describe(usuario="Usuário para consultar")
async def ficha_slash(interaction: discord.Interaction, usuario: discord.User):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
        await interaction.response.send_message("Sem permissão.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    w, warn_rows, p, pun_rows = await fetch_ficha(interaction.guild.id, usuario.id)
    emb = build_ficha_embed(interaction.guild, usuario, w, warn_rows, p, pun_rows)
    await interaction.edit_original_response(embed=emb)

@app_commands.context_menu(name="Ficha")
async def ficha_context(interaction: discord.Interaction, usuario: discord.Member):
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not _is_staff(interaction.user):
        await interaction.response.send_message("Sem permissão.", ephemeral=True)
        return

    w, warn_rows, p, pun_rows = await fetch_ficha(interaction.guild.id, usuario.id)
    await interaction.response.send_modal(FichaModal(interaction.guild, usuario, w, warn_rows, p, pun_rows))

class FichaCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._synced = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._synced:
            return
        self._synced = True

        try:
            # SYNC "forte": remove em ambos escopos e sincroniza no destino correto
            if GUILD_ID:
                g = discord.Object(id=GUILD_ID)

                # remove global (se existir antigo)
                self.bot.tree.remove_command("Ficha", guild=None, type=discord.AppCommandType.user)
                self.bot.tree.remove_command("ficha", guild=None, type=discord.AppCommandType.chat_input)

                # remove guild
                self.bot.tree.remove_command("Ficha", guild=g, type=discord.AppCommandType.user)
                self.bot.tree.remove_command("ficha", guild=g, type=discord.AppCommandType.chat_input)

                # re-add guild
                self.bot.tree.add_command(ficha_slash, guild=g, override=True)
                self.bot.tree.add_command(ficha_context, guild=g, override=True)

                await self.bot.tree.sync(guild=g)
                print(f"[FICHA] Sincronizado no guild {GUILD_ID}.")
            else:
                # modo global (pode demorar pra aparecer)
                self.bot.tree.remove_command("Ficha", guild=None, type=discord.AppCommandType.user)
                self.bot.tree.remove_command("ficha", guild=None, type=discord.AppCommandType.chat_input)

                self.bot.tree.add_command(ficha_slash, override=True)
                self.bot.tree.add_command(ficha_context, override=True)

                await self.bot.tree.sync()
                print("[FICHA] Sincronizado globalmente (pode demorar a propagar).")

        except Exception as e:
            print(f"[FICHA] Erro ao sincronizar comandos: {type(e).__name__}: {e}")

async def setup(bot: commands.Bot):
    await bot.add_cog(FichaCog(bot))

# setup não encontrado automaticamente
