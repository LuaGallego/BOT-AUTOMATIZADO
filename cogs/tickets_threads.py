import os
import re
import json
import asyncio
import html
import io
import discord
from discord.ext import commands, tasks
from datetime import timedelta
from pathlib import Path

# -----------------------------
# ENV / CONFIG
# -----------------------------
TICKET_PANEL_CHANNEL_ID = int(os.getenv("TICKET_PANEL_CHANNEL_ID", "0"))
TICKET_THREADS_CHANNEL_ID = int(os.getenv("TICKET_THREADS_CHANNEL_ID", "0"))

# onde salvar quem abriu cada ticket (thread_id -> user_id)
TICKET_OPENER_MAP_PATH = Path("data") / "tickets_thread_opener_map.json"

_raw_roles = os.getenv("TICKET_STAFF_ROLE_IDS", "")
TICKET_STAFF_ROLE_IDS = [int(r.strip()) for r in _raw_roles.split(",") if r.strip().isdigit()]

# Logs gerais (ajuda + denúncia juntas)
TICKET_LOG_OPEN_THREAD_ID = int(os.getenv("TICKET_LOG_OPEN_THREAD_ID", "0"))
TICKET_LOG_CLOSE_THREAD_ID = int(os.getenv("TICKET_LOG_CLOSE_THREAD_ID", "0"))

# Log específico de denúncia (para transcript HTML + infos quando FECHAR)
TICKET_LOG_DENUNCIA_THREAD_ID = int(os.getenv("TICKET_LOG_DENUNCIA_THREAD_ID", "0"))

DENUNCIA_RETENTION_DAYS = int(os.getenv("DENUNCIA_RETENTION_DAYS", "30"))
DENUNCIA_TRANSCRIPT_LIMIT = int(os.getenv("DENUNCIA_TRANSCRIPT_LIMIT", "500"))  # limite mensagens no HTML

TICKET_THREAD_NAME_RE = re.compile(r"^ticket-(\d{4})-(denuncia|ajuda)$", re.IGNORECASE)

# onde salvar o ID da mensagem do painel
PANEL_STORE_PATH = Path("data") / "tickets_panel.json"

# onde salvar o ID da mensagem do LOG "ticket aberto" pra poder apagar quando fechar
OPEN_LOG_MAP_PATH = Path("data") / "tickets_open_log_map.json"

# onde salvar os dados pra reabrir denúncia a partir do log
DENUNCIA_REOPEN_MAP_PATH = Path("data") / "tickets_denuncia_reopen_map.json"

# onde salvar transcripts HTML de denúncia para envio por DM via botão Histórico
DENUNCIA_TRANSCRIPTS_DIR = Path("data") / "tickets_denuncia_transcripts"

# -----------------------------
# DM NOTICE (cooldown)
# -----------------------------
DM_NOTICE_COOLDOWN_MIN = int(os.getenv("TICKET_DM_COOLDOWN_MIN", "15"))
DM_NOTICE_MAP_PATH = Path("data") / "tickets_dm_notice_map.json"

# -----------------------------
# CLAIM / RESPONSÁVEL (persistente)
# -----------------------------
TICKET_CLAIM_MAP_PATH = Path("data") / "tickets_claim_map.json"

def _load_claim_map() -> dict:
    try:
        if TICKET_CLAIM_MAP_PATH.exists():
            with open(TICKET_CLAIM_MAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_claim_map(data: dict) -> None:
    try:
        TICKET_CLAIM_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TICKET_CLAIM_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _get_claimed_by(guild_id: int, thread_id: int) -> int | None:
    store = _load_claim_map()
    g = store.get(str(guild_id), {})
    v = g.get(str(thread_id))
    try:
        return int(v) if v else None
    except Exception:
        return None

def _set_claimed_by(guild_id: int, thread_id: int, staff_id: int | None) -> None:
    store = _load_claim_map()
    gkey = str(guild_id)
    store.setdefault(gkey, {})
    if staff_id is None:
        store[gkey].pop(str(thread_id), None)
    else:
        store[gkey][str(thread_id)] = int(staff_id)
    _save_claim_map(store)



# -----------------------------
# STORAGE (ticket opener map)
# -----------------------------
def _load_ticket_opener_map() -> dict:
    try:
        if TICKET_OPENER_MAP_PATH.exists():
            with open(TICKET_OPENER_MAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_ticket_opener_map(data: dict) -> None:
    try:
        TICKET_OPENER_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TICKET_OPENER_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _store_ticket_opener(guild_id: int, thread_id: int, user_id: int) -> None:
    store = _load_ticket_opener_map()
    gkey = str(guild_id)
    store.setdefault(gkey, {})
    store[gkey][str(thread_id)] = int(user_id)
    _save_ticket_opener_map(store)

def _get_ticket_opener(guild_id: int, thread_id: int) -> int | None:
    store = _load_ticket_opener_map()
    g = store.get(str(guild_id), {})
    v = g.get(str(thread_id))
    try:
        return int(v) if v else None
    except Exception:
        return None

def _pop_ticket_opener(guild_id: int, thread_id: int) -> int | None:
    store = _load_ticket_opener_map()
    gkey = str(guild_id)
    g = store.get(gkey, {})
    v = g.pop(str(thread_id), None)
    store[gkey] = g
    _save_ticket_opener_map(store)
    try:
        return int(v) if v else None
    except Exception:
        return None


# -----------------------------
# STORAGE (panel message id)
# -----------------------------
def _load_panel_store() -> dict:
    try:
        if PANEL_STORE_PATH.exists():
            with open(PANEL_STORE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_panel_store(data: dict) -> None:
    try:
        PANEL_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PANEL_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# -----------------------------
# STORAGE (open log message map)
# -----------------------------
def _load_open_log_map() -> dict:
    try:
        if OPEN_LOG_MAP_PATH.exists():
            with open(OPEN_LOG_MAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_open_log_map(data: dict) -> None:
    try:
        OPEN_LOG_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(OPEN_LOG_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# -----------------------------
# STORAGE (denuncia reopen map)
# -----------------------------
def _load_denuncia_reopen_map() -> dict:
    try:
        if DENUNCIA_REOPEN_MAP_PATH.exists():
            with open(DENUNCIA_REOPEN_MAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_denuncia_reopen_map(data: dict) -> None:
    try:
        DENUNCIA_REOPEN_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DENUNCIA_REOPEN_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _store_denuncia_reopen_payload(guild_id: int, log_message_id: int, payload: dict) -> None:
    store = _load_denuncia_reopen_map()
    gkey = str(guild_id)
    store.setdefault(gkey, {})
    store[gkey][str(log_message_id)] = payload
    _save_denuncia_reopen_map(store)

def _get_denuncia_reopen_payload(guild_id: int, log_message_id: int) -> dict | None:
    store = _load_denuncia_reopen_map()
    g = store.get(str(guild_id), {})
    return g.get(str(log_message_id))


# -----------------------------
# STORAGE (dm notice map)
# -----------------------------
def _load_dm_notice_map() -> dict:
    try:
        if DM_NOTICE_MAP_PATH.exists():
            with open(DM_NOTICE_MAP_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _save_dm_notice_map(data: dict) -> None:
    try:
        DM_NOTICE_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DM_NOTICE_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _get_last_dm_ts(guild_id: int, thread_id: int) -> float | None:
    store = _load_dm_notice_map()
    g = store.get(str(guild_id), {})
    v = g.get(str(thread_id))
    try:
        return float(v) if v is not None else None
    except Exception:
        return None

def _set_last_dm_ts(guild_id: int, thread_id: int, ts: float) -> None:
    store = _load_dm_notice_map()
    gkey = str(guild_id)
    store.setdefault(gkey, {})
    store[gkey][str(thread_id)] = float(ts)
    _save_dm_notice_map(store)

def _clear_last_dm_ts(guild_id: int, thread_id: int) -> None:
    store = _load_dm_notice_map()
    gkey = str(guild_id)
    g = store.get(gkey, {})
    g.pop(str(thread_id), None)
    store[gkey] = g
    _save_dm_notice_map(store)


# -----------------------------
# HELPERS
# -----------------------------
async def get_thread_by_id(bot: commands.Bot, guild: discord.Guild, thread_id: int) -> discord.Thread | None:
    """Busca uma thread (postagem fixa do fórum) por ID e desarquiva se necessário."""
    if not thread_id:
        return None

    t = guild.get_thread(thread_id)
    if t is None:
        try:
            ch = await bot.fetch_channel(thread_id)
            t = ch if isinstance(ch, discord.Thread) else None
        except Exception:
            return None

    try:
        if t and t.archived:
            await t.edit(archived=False)
    except Exception:
        pass

    return t

def next_ticket_number(existing_threads: list[discord.Thread]) -> int:
    max_n = 0
    for t in existing_threads:
        m = TICKET_THREAD_NAME_RE.match((t.name or "").strip())
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


# -----------------------------
# UI (Views / Modals)
# -----------------------------

class TicketDMView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Abrir ticket",
            style=discord.ButtonStyle.link,
            url=url,
        ))
        
class TicketCloseView(discord.ui.View):
    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Fechar", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketCloseReasonModal(self.cog))

class DenunciaReopenView(discord.ui.View):
    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Reabrir", style=discord.ButtonStyle.secondary, emoji="🔓", custom_id="ticket:reopen:denuncia")
    async def reopen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.reopen_denuncia_from_log(interaction)

class DenunciaLogView(discord.ui.View):
    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Reabrir", style=discord.ButtonStyle.secondary, emoji="🔓", custom_id="ticket:reopen:denuncia")
    async def reopen_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.reopen_denuncia_from_log(interaction)

    @discord.ui.button(label="Histórico", style=discord.ButtonStyle.primary, emoji="📜", custom_id="ticket:history:denuncia")
    async def history_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.send_denuncia_history_dm(interaction)

class DenunciaModal(discord.ui.Modal, title="Denúncia"):
    acusado = discord.ui.TextInput(
        label="Quem você está denunciando?",
        placeholder="Nickname / @ / ID (o que você souber)",
        max_length=200,
        required=True
    )
    motivo = discord.ui.TextInput(
        label="Motivo da denúncia",
        style=discord.TextStyle.paragraph,
        placeholder="Explique o que aconteceu",
        max_length=1200,
        required=True
    )
    provas = discord.ui.TextInput(
        label="Provas (links / prints / vídeos)",
        style=discord.TextStyle.paragraph,
        placeholder="Cole links aqui (se tiver). Se não tiver, escreva 'Não tenho'.",
        max_length=1200,
        required=True
    )

    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        try:
            await self.cog.create_ticket_thread(
                interaction=interaction,
                kind="denuncia",
                form_data={
                    "Acusado": str(self.acusado.value),
                    "Motivo": str(self.motivo.value),
                    "Provas": str(self.provas.value),
                }
            )
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ Erro ao enviar a denúncia: `{type(e).__name__}`", ephemeral=True)
            except Exception:
                pass

class AjudaModal(discord.ui.Modal, title="Ajuda"):
    assunto = discord.ui.TextInput(
        label="Assunto",
        placeholder="Ex: dúvida sobre whitelist / servidor / regras / etc.",
        max_length=120,
        required=True
    )
    detalhes = discord.ui.TextInput(
        label="Descreva sua dúvida/problema",
        style=discord.TextStyle.paragraph,
        placeholder="Quanto mais detalhes, melhor 🙂",
        max_length=1200,
        required=True
    )

    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        try:
            await self.cog.create_ticket_thread(
                interaction=interaction,
                kind="ajuda",
                form_data={
                    "Assunto": str(self.assunto.value),
                    "Detalhes": str(self.detalhes.value),
                }
            )
        except Exception as e:
            try:
                await interaction.followup.send(f"❌ Erro ao enviar o pedido de ajuda: `{type(e).__name__}`", ephemeral=True)
            except Exception:
                pass


class TicketAddMemberModal(discord.ui.Modal, title="Adicionar membro ao ticket"):
    user_ref = discord.ui.TextInput(
        label="ID ou @ da pessoa",
        placeholder="Ex: 1234567890 ou @Fulano",
        max_length=100,
        required=True
    )

    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.add_member_to_ticket(interaction, str(self.user_ref.value))


class TicketCloseReasonModal(discord.ui.Modal, title="Fechar ticket com motivo"):
    reason = discord.ui.TextInput(
        label="Motivo do fechamento",
        style=discord.TextStyle.paragraph,
        placeholder="Explique o motivo (isso vai pro log e DM do autor)",
        max_length=800,
        required=True
    )

    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.close_ticket(interaction, reason=str(self.reason.value))


class TicketActionsView(discord.ui.View):
    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Assumir", style=discord.ButtonStyle.primary, emoji="👤", custom_id="ticket:claim")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.claim_ticket(interaction)

    @discord.ui.button(label="Adicionar membro", style=discord.ButtonStyle.secondary, emoji="➕", custom_id="ticket:add_member")
    async def add_member_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.open_add_member_modal(interaction)
    @discord.ui.button(label="Fechar", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="ticket:close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketCloseReasonModal(self.cog))


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketThreadsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Denúncia", style=discord.ButtonStyle.danger, emoji="🚨", custom_id="ticket:open:denuncia")
    async def denuncia_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DenunciaModal(self.cog))

    @discord.ui.button(label="Ajuda", style=discord.ButtonStyle.primary, emoji="🆘", custom_id="ticket:open:ajuda")
    async def ajuda_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AjudaModal(self.cog))


# -----------------------------
# COG
# -----------------------------
class TicketThreadsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_checked = False
        self.cleanup_denuncias.start()

    async def open_link_help_ticket(
        self,
        member,
        whitelist_status=None,
        active_code=None,
        linked=None,
        initial_embed: discord.Embed | None = None,
        reason: str | None = None,
        link_code: str | None = None,
        link_expires_at=None,
    ):
        guild = member.guild

        parent = guild.get_channel(TICKET_THREADS_CHANNEL_ID)
        if not isinstance(parent, discord.TextChannel):
            raise RuntimeError("Canal de tickets não configurado (ID inválido).")

        staff_roles = self._get_staff_roles(guild)
        if not staff_roles:
            raise RuntimeError("Cargos de staff não configurados (IDs inválidos).")

        existing = await self._find_existing_ticket_for_user(parent, member.id)
        if existing:
            try:
                await existing.send(
                    content=f"{member.mention} pediu ajuda com vínculo Discord ↔ jogo novamente."
                )
                if initial_embed:
                    await existing.send(embed=initial_embed)
            except Exception:
                pass
            return existing

        n = next_ticket_number(parent.threads)
        thread_name = f"ticket-{n:04d}-ajuda"

        try:
            thread = await parent.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                reason=f"Ajuda de vínculo aberta para {member} ({member.id})"
            )
        except discord.Forbidden:
            raise RuntimeError(
                "Sem permissão para criar thread privada (Create Private Threads / Manage Threads)."
            )

        _store_ticket_opener(guild.id, thread.id, member.id)

        try:
            await thread.add_user(member)
        except Exception:
            pass

        added_staff = 0
        try:
            for m in guild.members:
                if m.bot:
                    continue
                if any(r in m.roles for r in staff_roles):
                    try:
                        await thread.add_user(m)
                        added_staff += 1
                    except Exception:
                        pass
        except Exception:
            pass

        wl_status = "-"
        wl_created = "-"
        if whitelist_status:
            try:
                wl_status = str(whitelist_status[0])
            except Exception:
                pass
            try:
                wl_created = str(whitelist_status[1])
            except Exception:
                pass

        current_code = "-"
        current_exp = "-"

        if link_code:
            current_code = str(link_code)

        if link_expires_at:
            current_exp = str(link_expires_at)

        if current_code == "-" and active_code:
            current_code = str(active_code.get("code", "-"))

        if current_exp == "-" and active_code:
            current_exp = str(active_code.get("expires_at", "-"))

        linked_status = "Sim" if linked else "Não"

        embed = self._styled_embed(
            title="🆘 Ajuda de vínculo",
            description=(
                f"**{member.mention}** precisa de ajuda com o vínculo Discord ↔ jogo.\n\n"
                f"**🎟️ Número:** `{n:04d}`\n"
                "**🔒 Privacidade:** thread visível apenas para o usuário e a staff\n\n"
                "Este ticket foi aberto automaticamente porque a DM de vínculo falhou "
                "ou o usuário pediu ajuda."
            ),
            color_key="warning",
            guild=guild,
            footer="Ajuda de vínculo • Atendimento privado",
        )

        self._add_section(embed, "👤 Usuário")
        self._add_kv(embed, "Discord", f"{member.mention} (`{member.id}`)", inline=False)

        self._add_section(embed, "📋 Estado atual")
        self._add_kv(embed, "Whitelist", f"`{wl_status}`", inline=True)
        self._add_kv(embed, "Registrada em", f"`{wl_created}`", inline=True)
        self._add_kv(embed, "Já vinculado", f"`{linked_status}`", inline=True)

        self._add_section(embed, "🔑 Código atual")
        self._add_kv(embed, "Código", f"`{current_code}`", inline=True)
        self._add_kv(embed, "Expira em", f"`{current_exp}`", inline=True)

        if reason:
            self._add_section(embed, "📝 Motivo")
            self._add_kv(embed, "Origem", reason, inline=False)

        await thread.send(
            content=f"{member.mention} " + " ".join(r.mention for r in staff_roles),
            embed=embed,
            view=TicketActionsView(self)
        )

        if initial_embed:
            try:
                await thread.send(embed=initial_embed)
            except Exception:
                pass

        form_data = {
            "Assunto": "Ajuda com vínculo Discord ↔ jogo",
            "Detalhes": f"DM falhou ou usuário pediu ajuda. Código atual: {current_code}",
        }

        log_embed = self._build_log_embed(
            action="open",
            kind="ajuda",
            number=n,
            thread=thread,
            thread_name=thread_name,
            member=member,
            actor=None,
            form_data=form_data,
            added_staff=added_staff,
        )

        msg = await self._log_open(guild, "ajuda", embed=log_embed)
        if msg is not None:
            self._store_open_log_message_id(guild.id, thread.id, msg.id)

        return thread

    # =============================
    # ESTILO GLOBAL DAS EMBEDS
    # =============================
    def _theme_color(self, kind: str = "info") -> discord.Color:
        red = discord.Color.from_rgb(237, 66, 69)  # vermelho painel

        colors = {
            "danger": red,
            "warning": red,
            "success": red,
            "info": red,
            "primary": red,
            "purple": red,
            "muted": red,
        }
        return colors.get(kind, red)

    def _styled_embed(
        self,
        *,
        title: str,
        description: str,
        color_key: str = "info",
        guild: discord.Guild | None = None,
        footer: str = "Doom Project • Central de Tickets",
        author_name: str | None = None,
        author_icon: str | None = None,
        thumbnail_url: str | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=(
                "**CENTRAL DE ATENDIMENTO**\n"
                "**PAINEL DE TICKETS**\n\n"
                f"{description}\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=self._theme_color(color_key),
            timestamp=discord.utils.utcnow(),
        )

        # author com ícone estilo painel
        try:
            if author_name:
                if author_icon:
                    embed.set_author(name=author_name, icon_url=author_icon)
                else:
                    embed.set_author(name=author_name)
            else:
                if guild and guild.icon:
                    embed.set_author(name=f"{guild.name} • Atendimento Oficial", icon_url=guild.icon.url)
                elif guild:
                    embed.set_author(name=f"{guild.name} • Atendimento Oficial")
                else:
                    embed.set_author(name="Doom Project • Atendimento Oficial")
        except Exception:
            pass

    # thumbnail à direita
        try:
            if thumbnail_url:
                embed.set_thumbnail(url=thumbnail_url)
            elif guild and getattr(guild, "icon", None):
                embed.set_thumbnail(url=guild.icon.url)
            elif getattr(self.bot.user, "display_avatar", None):
                embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        except Exception:
            pass

        embed.set_footer(text=footer)
        return embed

    def _add_section(self, embed: discord.Embed, title: str) -> None:
        embed.add_field(name="ㅤ", value=f"**{title}**", inline=False)

    def _add_kv(self, embed: discord.Embed, name: str, value: str, inline: bool = False) -> None:
        if value is None:
            value = "-"
        text = str(value).strip()
        if len(text) > 900:
            text = text[:900] + "…"
        embed.add_field(name=name, value=text or "-", inline=inline)


    def cog_unload(self):
        self.cleanup_denuncias.cancel()

    def _get_staff_roles(self, guild: discord.Guild) -> list[discord.Role]:
        roles: list[discord.Role] = []
        for rid in TICKET_STAFF_ROLE_IDS:
            r = guild.get_role(rid)
            if r:
                roles.append(r)
        return roles

    async def _dm_user_safe(self, user: discord.abc.User, content: str | None = None, *, embed: discord.Embed | None = None, view: discord.ui.View | None = None) -> bool:
        """Envia DM sem derrubar o fluxo; suporta texto, embed e/ou view."""
        try:
            if content is None and embed is None:
                return False
            await user.send(content=content, embed=embed, view=view)
            return True
        except Exception as e:
            print(f"[DM] Falhou para {getattr(user, 'id', None)}: {type(e).__name__}")
            return False
    def _build_log_embed(
        self,
        *,
        action: str,
        kind: str,
        number: int | None,
        thread: discord.Thread | None,
        thread_name: str | None,
        member: discord.Member | discord.User | None,
        actor: discord.Member | discord.User | None,
        form_data: dict[str, str] | None = None,
        added_staff: int | None = None,
        close_action: str | None = None,
        status_txt: str | None = None,
    ) -> discord.Embed:
        is_open = (action == "open")
        is_denuncia = (kind == "denuncia")
        tipo = "Denúncia" if is_denuncia else "Ajuda"

        if is_open and is_denuncia:
            color_key = "danger"
        elif is_open:
            color_key = "info"
        elif is_denuncia:
            color_key = "purple"
        else:
            color_key = "success"

        title = "🎬 Ticket Aberto" if is_open else "🔒 Ticket Finalizado"
        description = (
            f"**• Tipo:** `{tipo}`\n"
            f"**• Evento:** `{'Abertura' if is_open else 'Fechamento'}`\n"
            "**• Registro:** `Logs de staff`\n"
        )

        guild_ref = thread.guild if thread else None
        embed = self._styled_embed(
            title=title,
            description=description,
            color_key=color_key,
            guild=guild_ref,
            footer=f"Logs • Tickets {tipo}",
        )

        if number is not None:
            self._add_kv(embed, "🎟️ Número", f"`{number:04d}`", inline=True)

        if thread is not None:
            self._add_kv(embed, "🧵 Thread", thread.mention, inline=True)

        if thread_name:
            self._add_kv(embed, "📌 Nome interno", f"`{thread_name}`", inline=False)

        if member is not None:
            self._add_kv(embed, "👤 Autor", f"{member.mention} (`{member.id}`)", inline=False)

        if is_open:
            if added_staff is not None:
                self._add_kv(embed, "👮 Staff adicionada", f"`{added_staff}`", inline=True)
        else:
            if actor is not None:
                self._add_kv(embed, "🛡️ Fechado por", f"{actor.mention} (`{actor.id}`)", inline=False)
            if close_action:
                self._add_kv(embed, "⚙️ Ação", f"`{close_action}`", inline=True)
            if status_txt:
                self._add_kv(embed, "📊 Status", status_txt, inline=True)

        if form_data:
            self._add_section(embed, "🧾 Formulário")
            for k, v in form_data.items():
                vv = v if len(v) <= 900 else (v[:900] + "…")
                self._add_kv(embed, f"✦ {k}", vv, inline=False)

        return embed


    async def _send_log_to_thread(
        self,
        guild: discord.Guild,
        thread_id: int,
        *,
        text: str | None = None,
        embed: discord.Embed | None = None,
        file: discord.File | None = None,
        view: discord.ui.View | None = None,
    ) -> discord.Message | None:
        if not thread_id:
            return None

        t = await get_thread_by_id(self.bot, guild, int(thread_id))
        if not t:
            return None

        try:
            kwargs: dict = {}
            if text is not None:
                kwargs["content"] = text
            if embed is not None:
                kwargs["embed"] = embed
            if file is not None:
                kwargs["file"] = file
            if view is not None:
                kwargs["view"] = view
            return await t.send(**kwargs)
        except Exception as e:
            print(f"[LOG SEND] Falhou thread={thread_id}: {type(e).__name__}")
            return None

    async def _log_open(self, guild: discord.Guild, kind: str, *, embed: discord.Embed) -> discord.Message | None:
        # Log principal de "abertos" (se configurado)
        return await self._send_log_to_thread(
            guild,
            TICKET_LOG_OPEN_THREAD_ID,
            text=None,
            embed=embed,
            file=None,
            view=None,
        )

    async def _log_close(self, guild: discord.Guild, kind: str, *, embed: discord.Embed) -> discord.Message | None:
        # Denúncia também pode ter bundle específico; este aqui é o log geral de "fechados"
        return await self._send_log_to_thread(
            guild,
            TICKET_LOG_CLOSE_THREAD_ID,
            text=None,
            embed=embed,
            file=None,
            view=None,
        )


    def _store_open_log_message_id(self, guild_id: int, ticket_thread_id: int, message_id: int | None):
        if not message_id:
            return
        store = _load_open_log_map()
        gkey = str(guild_id)
        store.setdefault(gkey, {})
        store[gkey][str(ticket_thread_id)] = int(message_id)
        _save_open_log_map(store)

    def _pop_open_log_message_id(self, guild_id: int, ticket_thread_id: int) -> int | None:
        store = _load_open_log_map()
        gkey = str(guild_id)
        g = store.get(gkey, {})
        mid = g.pop(str(ticket_thread_id), None)
        store[gkey] = g
        _save_open_log_map(store)
        try:
            return int(mid) if mid else None
        except Exception:
            return None

    async def _delete_open_log_entry_if_exists(self, guild: discord.Guild, ticket_thread_id: int):
        if not TICKET_LOG_OPEN_THREAD_ID:
            return

        t = await get_thread_by_id(self.bot, guild, TICKET_LOG_OPEN_THREAD_ID)
        if not t:
            return

        # 1) tenta pelo mapa persistido
        msg_id = self._pop_open_log_message_id(guild.id, ticket_thread_id)
        if msg_id:
            try:
                msg = await t.fetch_message(int(msg_id))
                await msg.delete()
                return
            except Exception:
                pass

        # 2) fallback: procura nas últimas mensagens pelo campo da thread no embed
        try:
            async for msg in t.history(limit=200):
                if not msg.embeds:
                    continue
                emb = msg.embeds[0]
                if not emb or not emb.fields:
                    continue

                for f in emb.fields:
                    if "Thread" in str(f.name) and str(ticket_thread_id) in str(f.value):
                        try:
                            await msg.delete()
                            return
                        except Exception:
                            pass
        except Exception:
            pass

    async def _find_existing_ticket_for_user(self, parent: discord.TextChannel, user_id: int) -> discord.Thread | None:
        for t in parent.threads:
            if not TICKET_THREAD_NAME_RE.match((t.name or "").strip()):
                continue
            try:
                cached_members = getattr(t, "members", None)
                if isinstance(cached_members, list):
                    if any(getattr(m, "id", None) == user_id for m in cached_members):
                        return t

                if hasattr(t, "fetch_members"):
                    fetched = [m async for m in t.fetch_members()]
                    if any(getattr(m, "id", None) == user_id for m in fetched):
                        return t
            except Exception:
                pass
        return None

    async def _safe_thread_members(self, thread: discord.Thread, timeout: float = 8.0) -> list[discord.Member]:
        try:
            async def _fetch():
                out = []

                cached = getattr(thread, "members", None)
                if isinstance(cached, list):
                    out.extend([m for m in cached if isinstance(m, discord.Member)])

                if hasattr(thread, "fetch_members"):
                    try:
                        fetched = [m async for m in thread.fetch_members()]
                        for m in fetched:
                            if isinstance(m, discord.Member) and all(x.id != m.id for x in out):
                                out.append(m)
                    except Exception:
                        pass

                return out

            res = await asyncio.wait_for(_fetch(), timeout=timeout)
            return res
        except Exception:
            return []

    async def _extract_form_data_from_thread(self, thread: discord.Thread) -> dict[str, str] | None:
        try:
            async def _fetch():
                return [m async for m in thread.history(limit=50, oldest_first=True)]
            messages = await asyncio.wait_for(_fetch(), timeout=8.0)
        except Exception:
            return None

        bot_id = getattr(self.bot.user, "id", None)
        if not bot_id:
            return None

        for msg in messages:
            try:
                if msg.author.id != bot_id:
                    continue
                if not msg.embeds:
                    continue
                emb = msg.embeds[0]
                if not emb or not emb.fields:
                    continue

                data: dict[str, str] = {}
                for f in emb.fields:
                    if f.name and f.value:
                        data[str(f.name)] = str(f.value)

                if any(k in data for k in ("Assunto", "Detalhes", "Acusado", "Motivo", "Provas")):
                    return data
            except Exception:
                continue

        return None

    def _build_transcript_html(self, title: str, rows: list[dict]) -> str:
        css = """
        body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;background:#0b0f14;color:#e6edf3;margin:0;padding:24px}
        .wrap{max-width:980px;margin:0 auto}
        .head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}
        .title{font-size:20px;font-weight:700}
        .meta{font-size:12px;color:#9fb0c0}
        .msg{background:#111824;border:1px solid #1f2a37;border-radius:12px;padding:12px 14px;margin:10px 0}
        .msg.staff{border-color:#2b4a2f;background:#0f1a12;box-shadow: inset 0 0 0 1px rgba(80,200,120,.08)}
        .msg.staff .author{color:#7ee787}
        .top{display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:6px}
        .author{font-weight:700}
        .time{font-size:12px;color:#9fb0c0;white-space:nowrap}
        .content{white-space:pre-wrap;word-break:break-word;line-height:1.35}
        .attachments{margin-top:8px;font-size:12px}
        .attachments a{color:#7cc7ff;text-decoration:none}
        .attachments a:hover{text-decoration:underline}
        hr{border:none;border-top:1px solid #1f2a37;margin:18px 0}
        """
        parts = [
            "<!doctype html>",
            "<html><head><meta charset='utf-8'/>",
            f"<title>{html.escape(title)}</title>",
            f"<style>{css}</style>",
            "</head><body><div class='wrap'>",
            "<div class='head'>",
            f"<div class='title'>{html.escape(title)}</div>",
            f"<div class='meta'>Gerado por bot • Total mensagens: {len(rows)}</div>",
            "</div>",
            "<hr/>"
        ]

        for r in rows:
            author = html.escape(r.get("author", ""))
            ts = html.escape(r.get("time", ""))
            content = html.escape(r.get("content", "") or "")
            atts = r.get("attachments", []) or []
            is_staff = bool(r.get("is_staff"))
            cls = "msg staff" if is_staff else "msg"

            parts.append(f"<div class='{cls}'>")
            parts.append("<div class='top'>")
            parts.append(f"<div class='author'>{author}</div>")
            parts.append(f"<div class='time'>{ts}</div>")
            parts.append("</div>")
            parts.append(f"<div class='content'>{content}</div>")

            if atts:
                parts.append("<div class='attachments'><b>Anexos:</b><ul>")
                for a in atts:
                    u = html.escape(a)
                    parts.append(f"<li><a href='{u}' target='_blank' rel='noreferrer noopener'>{u}</a></li>")
                parts.append("</ul></div>")

            parts.append("</div>")

        parts.append("</div></body></html>")
        return "\n".join(parts)

    async def _make_denuncia_transcript_file(self, guild: discord.Guild, thread: discord.Thread, *, limit: int) -> discord.File | None:
        try:
            async def _fetch():
                msgs = [m async for m in thread.history(limit=limit, oldest_first=False)]
                msgs.reverse()
                return msgs

            msgs = await asyncio.wait_for(_fetch(), timeout=15.0)
        except Exception:
            return None

        staff_roles = self._get_staff_roles(guild)

        rows: list[dict] = []
        for m in msgs:
            try:
                if m.type != discord.MessageType.default and not (m.content or m.attachments):
                    continue

                author = f"{m.author} ({m.author.id})"
                when = (m.created_at.isoformat(sep=' ', timespec='seconds') if m.created_at else "")
                content = m.content or ""

                atts = []
                for a in (m.attachments or []):
                    try:
                        atts.append(a.url)
                    except Exception:
                        pass

                if not content and atts:
                    content = "(anexo)"

                member = guild.get_member(m.author.id)
                is_staff = False
                if member and staff_roles:
                    try:
                        is_staff = any(r in member.roles for r in staff_roles)
                    except Exception:
                        is_staff = False

                rows.append({
                    "author": author,
                    "time": when,
                    "content": content,
                    "attachments": atts,
                    "is_staff": is_staff,
                })
            except Exception:
                continue

        title = f"Transcript - {thread.name or 'ticket-denuncia'}"
        html_text = self._build_transcript_html(title, rows)

        data = html_text.encode("utf-8", errors="replace")
        bio = io.BytesIO(data)
        filename = f"{(thread.name or 'ticket-denuncia')}.html".replace("/", "_")
        return discord.File(fp=bio, filename=filename)


    def _persist_denuncia_transcript_local(self, guild_id: int, thread_id: int, file: discord.File | None) -> str | None:
        if file is None:
            return None
        try:
            DENUNCIA_TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
            fname = (getattr(file, "filename", None) or f"ticket-{thread_id}-denuncia.html").replace("/", "_")
            out_path = DENUNCIA_TRANSCRIPTS_DIR / f"{guild_id}_{thread_id}_{fname}"
            fp = getattr(file, "fp", None)
            if fp is None:
                return None
            try:
                fp.seek(0)
            except Exception:
                pass
            data = fp.read()
            if not isinstance(data, (bytes, bytearray)):
                return None
            out_path.write_bytes(bytes(data))
            try:
                fp.seek(0)
            except Exception:
                pass
            return str(out_path)
        except Exception:
            return None

    async def _send_denuncia_close_bundle(
        self,
        guild: discord.Guild,
        *,
        thread: discord.Thread,
        closer: discord.Member | discord.User,
        form_data: dict[str, str] | None,
        status_txt: str,
        opener_id: int | None,
        opener_member: discord.Member | None,
    ):
        if not TICKET_LOG_DENUNCIA_THREAD_ID:
            return

        close_embed = self._build_log_embed(
            action="close",
            kind="denuncia",
            number=None,
            thread=thread,
            thread_name=(thread.name or None),
            member=opener_member,
            actor=closer,
            form_data=form_data,
            close_action="ARCHIVE",
            status_txt=status_txt,
        )

        if opener_member is None and opener_id:
            close_embed.add_field(name="Autor (ID)", value=f"`{opener_id}`", inline=False)

        file = await self._make_denuncia_transcript_file(guild, thread, limit=DENUNCIA_TRANSCRIPT_LIMIT)
        transcript_path = self._persist_denuncia_transcript_local(guild.id, thread.id, file)

        msg = await self._send_log_to_thread(
            guild,
            TICKET_LOG_DENUNCIA_THREAD_ID,
            text=None,
            embed=close_embed,
            file=None,
            view=DenunciaLogView(self),
        )

        if msg is not None:
            _store_denuncia_reopen_payload(
                guild.id,
                msg.id,
                {
                    "old_thread_id": int(thread.id),
                    "old_thread_name": str(thread.name or ""),
                    "opener_id": int(opener_id) if opener_id else None,
                    "form_data": form_data or {},
                    "transcript_path": transcript_path,
                }
            )

    # -------- Panel auto-recreate --------
    async def ensure_panel(self, guild: discord.Guild):
        if not TICKET_PANEL_CHANNEL_ID:
            return

        panel_ch = guild.get_channel(TICKET_PANEL_CHANNEL_ID)
        if not isinstance(panel_ch, discord.TextChannel):
            return

        store = _load_panel_store()
        gkey = str(guild.id)
        saved = store.get(gkey, {})

        msg_id = saved.get("message_id")
        ch_id = saved.get("channel_id")

        if msg_id and ch_id == panel_ch.id:
            try:
                _ = await panel_ch.fetch_message(int(msg_id))
                return
            except discord.NotFound:
                pass
            except Exception:
                pass

        embed = self._styled_embed(
            title="🎬 Central de Tickets",
            description=(
                "Abra seu atendimento com um clique — tudo acontece em **thread privada** com você e a staff.\n\n"
                "**🚨 Denúncia**\n"
                "・Envie detalhes, nomes e provas\n"
                "・Canal ideal para reportar ocorrências\n\n"
                "**🆘 Ajuda**\n"
                "・Suporte geral e dúvidas\n"
                "・Atendimento direto com a equipe\n\n"
                "Clique em um botão abaixo para começar."
            ),
            color_key="primary",
            guild=guild,
            footer="Atendimento privado • Resposta da staff dentro do ticket",
        )

        msg = await panel_ch.send(embed=embed, view=TicketPanelView(self))

        store[gkey] = {"channel_id": panel_ch.id, "message_id": msg.id}
        _save_panel_store(store)

    # -------- Core (create / close) --------
    async def create_ticket_thread(self, interaction: discord.Interaction, kind: str, form_data: dict[str, str]):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            try:
                return await interaction.followup.send("Isso só funciona dentro do servidor.", ephemeral=True)
            except Exception:
                return

        guild = interaction.guild
        member = interaction.user

        parent = guild.get_channel(TICKET_THREADS_CHANNEL_ID)
        if not isinstance(parent, discord.TextChannel):
            return await interaction.followup.send("Canal de tickets não configurado (ID inválido).", ephemeral=True)

        staff_roles = self._get_staff_roles(guild)
        if not staff_roles:
            return await interaction.followup.send("Cargos de staff não configurados (IDs inválidos).", ephemeral=True)

        existing = await self._find_existing_ticket_for_user(parent, member.id)
        if existing:
            return await interaction.followup.send(f"Você já tem um ticket aberto: {existing.mention}", ephemeral=True)

        n = next_ticket_number(parent.threads)
        thread_name = f"ticket-{n:04d}-{kind}"

        try:
            thread = await parent.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                reason=f"Ticket ({kind}) aberto por {member} ({member.id})"
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "Não tenho permissão para criar **thread privada** aqui.\n"
                "No canal base (#tickets), dê ao bot: **Create Private Threads** e **Manage Threads**.",
                ephemeral=True
            )

        # ✅ salva autor do ticket (confiável para fechar depois)
        _store_ticket_opener(guild.id, thread.id, member.id)

        try:
            await thread.add_user(member)
        except Exception:
            pass

        added_staff = 0
        try:
            for m in guild.members:
                if m.bot:
                    continue
                if any(r in m.roles for r in staff_roles):
                    try:
                        await thread.add_user(m)
                        added_staff += 1
                    except Exception:
                        pass
        except Exception:
            pass

        is_denuncia = (kind == "denuncia")
        title = "🚨 Ticket de Denúncia" if is_denuncia else "🆘 Ticket de Ajuda"
        embed = self._styled_embed(
            title=title,
            description=(
                f"**Bem-vindo(a), {member.mention}!**\n"
                "Seu atendimento foi iniciado com sucesso.\n\n"
                f"**🎟️ Número:** `{n:04d}`\n"
                f"**🧾 Tipo:** `{'denuncia' if is_denuncia else 'ajuda'}`\n"
                "**🔒 Privacidade:** thread visível apenas para você e a staff\n\n"
                "Use este espaço para conversar com a equipe. Quando concluir, clique em **🔒 Fechar**."
            ),
            color_key="danger" if is_denuncia else "info",
            guild=guild,
            footer="Ticket ativo • Aguarde a staff responder",
        )

        self._add_section(embed, "🧾 Formulário enviado")
        for k, v in form_data.items():
            vv = v if len(v) <= 900 else (v[:900] + "…")
            self._add_kv(embed, f"✦ {k}", vv, inline=False)
        await thread.send(
            content=f"{member.mention} " + " ".join(r.mention for r in staff_roles),
            embed=embed,
            view=TicketActionsView(self)
        )

        await interaction.followup.send(f"✅ Ticket criado: {thread.mention}", ephemeral=True)

        log_embed = self._build_log_embed(
            action="open",
            kind=kind,
            number=n,
            thread=thread,
            thread_name=thread_name,
            member=member,
            actor=None,
            form_data=form_data,
            added_staff=added_staff,
        )

        msg = await self._log_open(guild, kind, embed=log_embed)

        if msg is not None:
            self._store_open_log_message_id(guild.id, thread.id, msg.id)

    async def close_ticket(self, interaction: discord.Interaction, reason: str | None = None):
        try:
            if not interaction.guild:
                return await interaction.response.send_message("Isso só funciona dentro do servidor.", ephemeral=True)

            if not isinstance(interaction.channel, discord.Thread):
                return await interaction.response.send_message("Isso só funciona dentro de um ticket (thread).", ephemeral=True)

            guild = interaction.guild
            thread = interaction.channel

            m = TICKET_THREAD_NAME_RE.match((thread.name or "").strip())
            if not m:
                return await interaction.response.send_message("Essa thread não parece ser um ticket.", ephemeral=True)

            kind = m.group(2).lower()

            staff_roles = self._get_staff_roles(guild)
            is_staff = isinstance(interaction.user, discord.Member) and any(r in interaction.user.roles for r in staff_roles)

            # pega o autor do MAPA (não depende da thread estar viva/cache)
            opener_id = _get_ticket_opener(guild.id, thread.id)

            # fallback antigo (se não tiver salvo por algum motivo)
            if opener_id is None:
                try:
                    members = await self._safe_thread_members(thread, timeout=8.0)
                    for mem in members:
                        if (not mem.bot) and (not any(r in mem.roles for r in staff_roles)):
                            opener_id = mem.id
                            break
                except Exception:
                    pass

            opener_member: discord.Member | None = None
            if opener_id:
                opener_member = guild.get_member(int(opener_id))

            is_opener = (opener_id == interaction.user.id)
            if not (is_staff or is_opener):
                return await interaction.response.send_message("Você não tem permissão para fechar este ticket.", ephemeral=True)

            await interaction.response.send_message("Fechando ticket…", ephemeral=True)

            form_data = await self._extract_form_data_from_thread(thread)

            close_action = ("DELETE" if kind == "ajuda" else "ARCHIVE")
            success = True
            error_name: str | None = None


            if kind == "ajuda":
                try:
                    await thread.delete(reason=f"Ticket ajuda fechado por {interaction.user} ({interaction.user.id})")
                except discord.Forbidden:
                    success = False
                    error_name = "Forbidden"
                    try:
                        await thread.send("❌ Não tenho permissão para deletar esta thread.")
                    except Exception:
                        pass
                except Exception as e:
                    success = False
                    error_name = type(e).__name__
                    try:
                        await thread.send(f"❌ Erro ao deletar: `{error_name}`")
                    except Exception:
                        pass
            else:
                try:
                    await thread.edit(
                        archived=True,
                        locked=True,
                        reason=f"Ticket denúncia fechado por {interaction.user} ({interaction.user.id})"
                    )
                except discord.Forbidden:
                    success = False
                    error_name = "Forbidden"
                    try:
                        await thread.send("❌ Não tenho permissão para arquivar/trancar esta thread. (precisa **Manage Threads**)")
                    except Exception:
                        pass
                except Exception as e:
                    success = False
                    error_name = type(e).__name__
                    try:
                        await thread.send(f"❌ Erro ao arquivar: `{error_name}`")
                    except Exception:
                        pass

            status_txt = "✅ OK" if success else f"❌ FALHOU ({error_name})"

            if success:
                await self._delete_open_log_entry_if_exists(guild, thread.id)

            close_embed = self._build_log_embed(
                action="close",
                kind=kind,
                number=None,
                thread=thread,
                thread_name=(thread.name or None),
                member=opener_member,
                actor=interaction.user,
                form_data=form_data,
                close_action=close_action,
                status_txt=status_txt,
            )

            if opener_member is None and opener_id:
                close_embed.add_field(name="Autor (ID)", value=f"`{opener_id}`", inline=False)

            if reason:
                close_embed.add_field(name="Motivo", value=(reason[:900] + "…") if len(reason) > 900 else reason, inline=False)

            await self._log_close(guild, kind, embed=close_embed)

            if kind == "denuncia":
                await self._send_denuncia_close_bundle(
                    guild,
                    thread=thread,
                    closer=interaction.user,
                    form_data=form_data,
                    status_txt=status_txt,
                    opener_id=opener_id,
                    opener_member=opener_member,
                )

            # 📩 DM de finalização pro autor (Ajuda + Denúncia)
            if opener_id:
                try:
                    target = opener_member
                    if target is None:
                        target = await self.bot.fetch_user(int(opener_id))

                    tipo = "Denúncia" if kind == "denuncia" else "Ajuda"

                    # 💌 DM de finalização (embed)
                    dm_embed = self._styled_embed(
                        title="✅ Atendimento encerrado",
                        description=(
                            "Seu ticket foi finalizado com sucesso.\n"
                            "Quando precisar, é só abrir um novo atendimento pela Central."
                        ),
                        color_key="success",
                        guild=interaction.guild,
                        footer="Central de Tickets • Atendimento finalizado",
                        author_name="Doom Project • Atendimento Oficial",
                        author_icon=(self.bot.user.display_avatar.url if getattr(self.bot.user, "display_avatar", None) else None),
                    )

                    self._add_kv(
                        dm_embed,
                        "🎟️ Resumo do ticket",
                        (
                            f"**Ticket:** `{thread.name}`\n"
                            f"**Tipo:** `{tipo}`"
                        ),
                        inline=False,
                    )
                    self._add_kv(dm_embed, "⚙️ Encerramento", f"`{close_action}`", inline=True)
                    self._add_kv(dm_embed, "📊 Resultado", f"{status_txt}", inline=True)
                    self._add_kv(dm_embed, "📁 Status", "`Finalizado`", inline=True)

                    if reason:
                        self._add_kv(
                            dm_embed,
                            "📝 Motivo informado",
                            (reason[:900] + "…") if len(reason) > 900 else reason,
                            inline=False,
                        )

                    self._add_kv(
                        dm_embed,
                        "👮 Encerrado por",
                        f"{interaction.user.mention}\n`ID: {interaction.user.id}`",
                        inline=False,
                    )
                    await self._dm_user_safe(target, None, embed=dm_embed)
                except Exception as e:
                    print(f"[DM FECHAMENTO] Falhou para {opener_id}: {type(e).__name__}")

            # limpa cooldown do aviso de resposta (não acumula)
            _clear_last_dm_ts(guild.id, thread.id)

            # limpa do map após fechar
            _pop_ticket_opener(guild.id, thread.id)
            _set_claimed_by(guild.id, thread.id, None)

        except Exception as e:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ Erro ao fechar o ticket: `{type(e).__name__}`", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ Erro ao fechar o ticket: `{type(e).__name__}`", ephemeral=True)
            except Exception:
                pass


    def _is_staff_member(self, guild: discord.Guild, member: discord.Member | None) -> bool:
        if not member:
            return False
        staff_roles = self._get_staff_roles(guild)
        if not staff_roles:
            return False
        try:
            return any(r in member.roles for r in staff_roles)
        except Exception:
            return False

    async def claim_ticket(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Isso só funciona dentro do servidor.", ephemeral=True)
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("Isso só funciona dentro de um ticket (thread).", ephemeral=True)

        guild = interaction.guild
        thread = interaction.channel

        if not self._is_staff_member(guild, interaction.user):
            return await interaction.response.send_message("Apenas a staff pode assumir tickets.", ephemeral=True)

        if not TICKET_THREAD_NAME_RE.match((thread.name or "").strip()):
            return await interaction.response.send_message("Essa thread não parece ser um ticket.", ephemeral=True)

        current = _get_claimed_by(guild.id, thread.id)
        if current and current != interaction.user.id:
            return await interaction.response.send_message("Este ticket já foi assumido por outra pessoa.", ephemeral=True)

        _set_claimed_by(guild.id, thread.id, interaction.user.id)

        try:
            await interaction.response.send_message(f"👤 Ticket assumido por {interaction.user.mention}", ephemeral=False)
        except Exception:
            pass

    async def open_add_member_modal(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Isso só funciona dentro do servidor.", ephemeral=True)
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("Isso só funciona dentro de um ticket (thread).", ephemeral=True)

        guild = interaction.guild
        if not self._is_staff_member(guild, interaction.user):
            return await interaction.response.send_message("Apenas a staff pode adicionar membros.", ephemeral=True)

        await interaction.response.send_modal(TicketAddMemberModal(self))

    async def add_member_to_ticket(self, interaction: discord.Interaction, user_ref: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("Isso só funciona dentro do servidor.", ephemeral=True)
        if not isinstance(interaction.channel, discord.Thread):
            return await interaction.response.send_message("Isso só funciona dentro de um ticket (thread).", ephemeral=True)

        guild = interaction.guild
        thread = interaction.channel

        if not self._is_staff_member(guild, interaction.user):
            return await interaction.response.send_message("Apenas a staff pode adicionar membros.", ephemeral=True)

        raw = user_ref.strip()
        uid = None
        mm = re.match(r"<@!?(\d+)>", raw)
        if mm:
            uid = int(mm.group(1))
        elif raw.isdigit():
            uid = int(raw)

        if not uid:
            return await interaction.response.send_message("Não entendi o ID/@. Envie um ID numérico ou mencione alguém.", ephemeral=True)

        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                member = None

        if member is None:
            return await interaction.response.send_message("Não consegui encontrar esse usuário no servidor.", ephemeral=True)

        try:
            await thread.add_user(member)
        except discord.Forbidden:
            return await interaction.response.send_message("Sem permissão para adicionar pessoas na thread (Manage Threads).", ephemeral=True)
        except Exception:
            return await interaction.response.send_message("Falhou ao adicionar o membro. Tente novamente.", ephemeral=True)

        try:
            await interaction.response.send_message(f"✅ Adicionado ao ticket: {member.mention}", ephemeral=True)
        except Exception:
            pass

    # -------- Reopen denúncia (from log button) --------
    async def reopen_denuncia_from_log(self, interaction: discord.Interaction):
        try:
            if not interaction.guild:
                return await interaction.response.send_message("Isso só funciona dentro do servidor.", ephemeral=True)

            guild = interaction.guild

            staff_roles = self._get_staff_roles(guild)
            is_staff = isinstance(interaction.user, discord.Member) and any(r in interaction.user.roles for r in staff_roles)
            if not is_staff:
                return await interaction.response.send_message("Apenas a staff pode reabrir denúncias.", ephemeral=True)

            if not interaction.message:
                return await interaction.response.send_message("Não consegui identificar a mensagem do log.", ephemeral=True)

            payload = _get_denuncia_reopen_payload(guild.id, interaction.message.id)
            if not payload:
                return await interaction.response.send_message("Não encontrei os dados para reabrir essa denúncia.", ephemeral=True)

            parent = guild.get_channel(TICKET_THREADS_CHANNEL_ID)
            if not isinstance(parent, discord.TextChannel):
                return await interaction.response.send_message("Canal de tickets não configurado (ID inválido).", ephemeral=True)

            n = next_ticket_number(parent.threads)
            thread_name = f"ticket-{n:04d}-denuncia"

            await interaction.response.defer(ephemeral=True)

            try:
                new_thread = await parent.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.private_thread,
                    auto_archive_duration=1440,
                    reason=f"Denúncia reaberta por {interaction.user} ({interaction.user.id})"
                )
            except discord.Forbidden:
                return await interaction.followup.send(
                    "Não tenho permissão para criar **thread privada** aqui.\n"
                    "No canal base (#tickets), dê ao bot: **Create Private Threads** e **Manage Threads**.",
                    ephemeral=True
                )

            opener_id = payload.get("opener_id")
            opener_member = None
            if opener_id:
                opener_member = guild.get_member(int(opener_id))
                if opener_member:
                    try:
                        await new_thread.add_user(opener_member)
                    except Exception:
                        pass

                # salva autor da denúncia reaberta (pro fechamento/log)
                try:
                    _store_ticket_opener(guild.id, new_thread.id, int(opener_id))
                except Exception:
                    pass

            added_staff = 0
            try:
                for m in guild.members:
                    if m.bot:
                        continue
                    if any(r in m.roles for r in staff_roles):
                        try:
                            await new_thread.add_user(m)
                            added_staff += 1
                        except Exception:
                            pass
            except Exception:
                pass

            old_thread_id = payload.get("old_thread_id")
            old_thread_name = payload.get("old_thread_name") or "(sem nome)"
            form_data = payload.get("form_data") or {}

            embed = self._styled_embed(
                title="🔓 Denúncia Reaberta",
                description=(
                    "A denúncia foi reativada para continuidade do atendimento.\n\n"
                    f"**👮 Reaberta por:** {interaction.user.mention}\n"
                    f"**🎟️ Número:** `{n:04d}`\n"
                    f"**📦 Origem:** `{old_thread_name}` (`{old_thread_id}`)\n\n"
                    "Converse normalmente nesta thread. Para finalizar, use **🔒 Fechar**."
                ),
                color_key="purple",
                guild=guild,
                footer="Denúncia reaberta • Histórico preservado",
            )

            self._add_section(embed, "🗂️ Dados recuperados do ticket")
            for k, v in form_data.items():
                vv = v if len(v) <= 900 else (v[:900] + "…")
                self._add_kv(embed, f"✦ {k}", vv, inline=False)
            mentions = []
            if opener_member:
                mentions.append(opener_member.mention)
            mentions.extend([r.mention for r in staff_roles])
            mention_line = " ".join(mentions).strip()

            await new_thread.send(
                content=mention_line if mention_line else None,
                embed=embed,
                view=TicketActionsView(self)
            )

            open_embed = self._build_log_embed(
                action="open",
                kind="denuncia",
                number=n,
                thread=new_thread,
                thread_name=thread_name,
                member=opener_member,
                actor=None,
                form_data=form_data,
                added_staff=added_staff,
            )
            if opener_member is None and opener_id:
                open_embed.add_field(name="Autor (ID)", value=f"`{opener_id}`", inline=False)

            msg_open = await self._log_open(guild, "denuncia", embed=open_embed)
            if msg_open is not None:
                self._store_open_log_message_id(guild.id, new_thread.id, msg_open.id)

            await interaction.followup.send(f"✅ Denúncia reaberta: {new_thread.mention}", ephemeral=True)

        except Exception as e:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ Erro ao reabrir: `{type(e).__name__}`", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ Erro ao reabrir: `{type(e).__name__}`", ephemeral=True)
            except Exception:
                pass

    async def send_denuncia_history_dm(self, interaction: discord.Interaction):
        try:
            if not interaction.guild:
                return await interaction.response.send_message("Isso só funciona dentro do servidor.", ephemeral=True)

            guild = interaction.guild
            staff_roles = self._get_staff_roles(guild)
            is_staff = isinstance(interaction.user, discord.Member) and any(r in interaction.user.roles for r in staff_roles)
            if not is_staff:
                return await interaction.response.send_message("Apenas a staff pode ver o histórico.", ephemeral=True)

            if not interaction.message:
                return await interaction.response.send_message("Não consegui identificar a mensagem do log.", ephemeral=True)

            payload = _get_denuncia_reopen_payload(guild.id, interaction.message.id)
            if not payload:
                return await interaction.response.send_message("Não encontrei os dados do histórico dessa denúncia.", ephemeral=True)

            transcript_path = payload.get("transcript_path")
            if not transcript_path:
                return await interaction.response.send_message("Histórico indisponível para esse registro.", ephemeral=True)

            p = Path(str(transcript_path))
            if not p.exists() or not p.is_file():
                return await interaction.response.send_message("Não encontrei o arquivo de histórico dessa denúncia.", ephemeral=True)

            old_thread_name = str(payload.get("old_thread_name") or "ticket-denuncia")
            opener_id = payload.get("opener_id")
            form_data = payload.get("form_data") or {}

            dm_embed = self._styled_embed(
                title="📜 Histórico da denúncia",
                description=(
                    "Separei o transcript em HTML para você consultar com calma.\n"
                    "Abra o arquivo anexado para ver toda a conversa registrada."
                ),
                color_key="purple",
                guild=guild,
                footer="Central de Tickets • Histórico da denúncia",
                author_name="Doom Project • Atendimento Oficial",
                author_icon=(self.bot.user.display_avatar.url if getattr(self.bot.user, "display_avatar", None) else None),
            )
            
            self._add_kv(dm_embed, "📌 Ticket", f"`{old_thread_name}`", inline=False)
            self._add_kv(dm_embed, "🧾 Categoria", "**Denúncia**", inline=True)

            if opener_id:
                self._add_kv(dm_embed, "👤 Autor do ticket", f"`{opener_id}`", inline=True)

            self._add_kv(
                dm_embed,
                "👮 Solicitado por",
                f"{interaction.user.mention} (`{interaction.user.id}`)",
                inline=False,
            )

            acusado = str(form_data.get("Acusado", "")).strip()
            motivo = str(form_data.get("Motivo", "")).strip()

            if acusado:
                self._add_kv(
                    dm_embed,
                    "🚨 Acusado",
                    (acusado[:900] + "…") if len(acusado) > 900 else acusado,
                    inline=False,
                )

            if motivo:
                self._add_kv(
                    dm_embed,
                    "📝 Motivo",
                    (motivo[:900] + "…") if len(motivo) > 900 else motivo,
                    inline=False,
                )
            try:
                if guild.icon:
                    dm_embed.set_thumbnail(url=guild.icon.url)
            except Exception:
                pass

            try:
                if getattr(interaction.user, "display_avatar", None):
                    dm_embed.set_author(
                        name=str(interaction.user),
                        icon_url=interaction.user.display_avatar.url
                    )
            except Exception:
                pass

            dm_embed.set_footer(text="Transcript em HTML • Uso interno da staff")

            try:
                dm_file = discord.File(str(p), filename=p.name)
                sent = await self._dm_user_safe(interaction.user, None, embed=dm_embed)
                if sent:
                    await interaction.user.send(file=dm_file)
                else:
                    return await interaction.response.send_message("Não consegui te enviar DM. Verifique se suas DMs estão abertas.", ephemeral=True)
            except Exception:
                return await interaction.response.send_message("Não consegui te enviar DM. Verifique se suas DMs estão abertas.", ephemeral=True)

            if interaction.response.is_done():
                await interaction.followup.send("✅ Te enviei o histórico no privado.", ephemeral=True)
            else:
                await interaction.response.send_message("✅ Te enviei o histórico no privado.", ephemeral=True)

        except Exception as e:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(f"❌ Erro ao enviar histórico: `{type(e).__name__}`", ephemeral=True)
                else:
                    await interaction.response.send_message(f"❌ Erro ao enviar histórico: `{type(e).__name__}`", ephemeral=True)
            except Exception:
                pass

    # -------- Auto cleanup (denúncias antigas) --------
    @tasks.loop(hours=24)
    async def cleanup_denuncias(self):
        cutoff = discord.utils.utcnow() - timedelta(days=DENUNCIA_RETENTION_DAYS)

        for guild in self.bot.guilds:
            parent = guild.get_channel(TICKET_THREADS_CHANNEL_ID)
            if not isinstance(parent, discord.TextChannel):
                continue

            threads_to_check: list[discord.Thread] = list(parent.threads)

            try:
                async for t in parent.archived_threads(limit=200):
                    threads_to_check.append(t)
            except Exception:
                pass

            for t in threads_to_check:
                name = (t.name or "").strip()
                mm = TICKET_THREAD_NAME_RE.match(name)
                if not mm:
                    continue

                kind = mm.group(2).lower()
                if kind != "denuncia":
                    continue

                if not getattr(t, "archived", False):
                    continue

                ts = getattr(t, "archive_timestamp", None) or getattr(t, "created_at", None)
                if ts is None:
                    continue

                if ts < cutoff:
                    try:
                        await t.delete(reason=f"Auto-cleanup: denúncia arquivada há mais de {DENUNCIA_RETENTION_DAYS} dias")
                    except Exception:
                        pass

    @cleanup_denuncias.before_loop
    async def before_cleanup_denuncias(self):
        await self.bot.wait_until_ready()

    # -------- Ready (persist views + ensure panel) --------
    @commands.Cog.listener()
    async def on_ready(self):
        # registra views persistentes
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketCloseView(self))
        self.bot.add_view(DenunciaLogView(self))
        self.bot.add_view(TicketActionsView(self))

        if self._panel_checked:
            return
        self._panel_checked = True

        for guild in self.bot.guilds:
            try:
                await self.ensure_panel(guild)
            except Exception:
                pass

    # -------- DM: avisar quando staff responder (cooldown por ticket) --------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
         return
        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        guild = message.guild

        m = TICKET_THREAD_NAME_RE.match((thread.name or "").strip())
        if not m:
            return

    # só staff
        member = guild.get_member(message.author.id)
        if not isinstance(member, discord.Member):
             return

        staff_roles = self._get_staff_roles(guild)
        if not staff_roles:
             return

        if not any(r in member.roles for r in staff_roles):
             return

        opener_id = _get_ticket_opener(guild.id, thread.id)
        if not opener_id or opener_id == message.author.id:
            return

        now = discord.utils.utcnow().timestamp()
        last = _get_last_dm_ts(guild.id, thread.id)
        cooldown = DM_NOTICE_COOLDOWN_MIN * 60

    # 1ª DM imediata; depois respeita cooldown
        if last is not None and (now - float(last)) < cooldown:
            return

        try:
             opener = guild.get_member(int(opener_id)) or await self.bot.fetch_user(int(opener_id))
        except Exception:
            return

        preview = (message.content or "").strip()
        if len(preview) > 200:
            preview = preview[:200] + "…"
        if not preview and getattr(message, "attachments", None):
             preview = "(anexo)"
        if not preview:
             preview = "(mensagem)"

        tipo = "Denúncia" if "denuncia" in (thread.name or "").lower() else "Ajuda"

    # ===== EMBED BONITO (igual estilo fechamento) =====
        dm_embed = self._styled_embed(
            title="✉️ Atualização no seu ticket",
            description=(
                "Uma nova resposta da equipe foi enviada no seu atendimento.\n"
                "Use o botão abaixo para voltar direto ao ticket."
            ),
            color_key="info",
            guild=guild,
            footer="Central de Tickets • Nova resposta da staff",
            author_name=str(message.author),
            author_icon=(message.author.display_avatar.url if getattr(message.author, "display_avatar", None) else None),
        )

        self._add_kv(
            dm_embed,
            "🎟️ Atendimento",
            (
                f"**Ticket:** `{thread.name}`\n"
                f"**Tipo:** `{tipo}`"
            ),
            inline=False,
        )
        self._add_kv(
            dm_embed,
            "👮 Resposta enviada por",
            f"{message.author.mention}\n`ID: {message.author.id}`",
            inline=True,
        )
        self._add_kv(
            dm_embed,
            "⏱️ Status",
            "`Aguardando seu retorno`",
            inline=True,
        )
        self._add_kv(
            dm_embed,
            "📝 Prévia da mensagem",
            (preview[:900] + "…") if len(preview) > 900 else preview,
            inline=False,
        )
        self._add_kv(
            dm_embed,
            "🔗 Acesso rápido",
            "Clique no botão **Abrir Ticket** logo abaixo.",
            inline=False,
        )
        ok = await self._dm_user_safe(opener, None, embed=dm_embed, view=TicketDMView(message.jump_url))
        if ok:
            _set_last_dm_ts(guild.id, thread.id, now)

    # -------- Listeners: se apagarem o painel, recria na hora --------
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id:
            return

        store = _load_panel_store()
        gkey = str(payload.guild_id)
        saved = store.get(gkey, {})
        saved_msg_id = saved.get("message_id")
        saved_ch_id = saved.get("channel_id")

        if not saved_msg_id or not saved_ch_id:
            return

        if int(saved_msg_id) != int(payload.message_id):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        try:
            await self.ensure_panel(guild)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if not payload.guild_id:
            return

        store = _load_panel_store()
        gkey = str(payload.guild_id)
        saved = store.get(gkey, {})
        saved_msg_id = saved.get("message_id")

        if not saved_msg_id:
            return

        if int(saved_msg_id) not in {int(mid) for mid in payload.message_ids}:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        try:
            await self.ensure_panel(guild)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketThreadsCog(bot))
