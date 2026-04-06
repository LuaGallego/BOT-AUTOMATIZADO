from __future__ import annotations

import os
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
import re
from utils.db import (
    get_whitelist_status,
    upsert_whitelist,
    get_config,
    set_config,
    get_active_whitelist_conflict_by_steam_id,
    transfer_whitelist_to_new_discord,
    get_whitelist_by_steam_id,
)

import io
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence

# ===== WELCOME CARD (inline no onboarding.py) =====
BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"

GIF_BG_PATH = ASSETS_DIR / "welcome_bg.gif"
PNG_BG_PATH = ASSETS_DIR / "welcome_bg.png"


def _safe_font(size: int, bold: bool = False):
    candidates = []
    if bold:
        candidates = [
            str(ASSETS_DIR / "Phonk Contrast DEMO.otf"),
            str(ASSETS_DIR / "phonk.ttf"),
            str(ASSETS_DIR / "PhonkRegular.ttf"),
            str(ASSETS_DIR / "Phonk.ttf"),
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/segoeuib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            str(ASSETS_DIR / "phonk.ttf"),
            str(ASSETS_DIR / "PhonkRegular.ttf"),
            str(ASSETS_DIR / "Phonk.ttf"),
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeui.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]

    for fp in candidates:
        try:
            return ImageFont.truetype(fp, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


async def _download_avatar(url: str) -> Image.Image:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.read()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _crop_avatar_circle(img: Image.Image, size: int = 140) -> Image.Image:
    avatar = ImageOps.fit(img, (size, size), method=Image.LANCZOS).convert("RGBA")

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)

    avatar.putalpha(mask)
    return avatar


def _draw_text_shadow(draw, xy, text, font, fill, shadow=(0, 0, 0, 180), off=2):
    x, y = xy
    draw.text((x + off, y + off), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def _server_color_rgba(member):
    # Verde neon fixo (estilo tóxico / Project Zomboid)
     return (255, 60, 60, 255)
        



def _draw_neon_ring(base: Image.Image, center_xy, radius: int, color=(255, 60, 60, 255)):
    x, y = center_xy
    glow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)

    for w, alpha in [(16, 25), (10, 50), (6, 90)]:
        d.ellipse(
            (x - radius - w, y - radius - w, x + radius + w, y + radius + w),
            outline=(color[0], color[1], color[2], alpha),
            width=max(2, w // 2),
        )

    d.ellipse(
        (x - radius, y - radius, x + radius, y + radius),
        outline=color,
        width=4,
    )

    base.alpha_composite(glow)


def _fit_text(draw, text: str, max_width: int, start_size: int, bold: bool = True):
    size = start_size
    while size >= 18:
        font = _safe_font(size, bold=bold)
        bbox = draw.textbbox((0, 0), text, font=font)
        width = bbox[2] - bbox[0]
        if width <= max_width:
            return font
        size -= 2
    return _safe_font(18, bold=bold)


def _build_overlay(
    frame_rgba: Image.Image,
    avatar_circle: Image.Image,
    display_name: str,
    subtitle_name: str | None,
    survivor_line: str | None,
    member_color,
    pulse: float = 1.0,
) -> Image.Image:
    img = frame_rgba.copy().convert("RGBA")
    w, h = img.size

    # faixa escura central (mais clean)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle((30, 40, w - 30, h - 40), radius=26, fill=(0, 0, 0, 110))
    img.alpha_composite(overlay)

    # avatar centralizado
    av_size = avatar_circle.size[0]
    avatar_x = (w - av_size) // 2
    avatar_y = (h - av_size) // 2 - 20
    center = (avatar_x + av_size // 2, avatar_y + av_size // 2)

    r, g, b, _ = member_color
    boost = int(30 * pulse)
    neon_color = (min(255, r + boost), min(255, g + boost), min(255, b + boost), 255)

    _draw_neon_ring(img, center, radius=(av_size // 2) + 4, color=neon_color)
    img.alpha_composite(avatar_circle, (avatar_x, avatar_y))

    draw = ImageDraw.Draw(img)

    # texto único embaixo
    text = (survivor_line or "").upper()
    if not text:
        text = "SOBREVIVENTE N ?"

    font_survivor = _fit_text(draw, text, max_width=w - 120, start_size=10, bold=True)

    bbox = draw.textbbox((0, 0), text, font=font_survivor)
    text_w = bbox[2] - bbox[0]
    text_x = (w - text_w) // 2
    text_y = avatar_y + av_size + 5

    _draw_text_shadow(
        draw,
        (text_x, text_y),
        text,
        font_survivor,
        fill=(230, 255, 230, 255),
        shadow=(0, 0, 0, 220),
        off=2,
    )

    return img


async def make_welcome_card(member, subtitle_name=None, survivor_line=None, animated=True) -> io.BytesIO:
    avatar_url = member.display_avatar.replace(format="png", size=256).url
    avatar = await _download_avatar(avatar_url)
    avatar_circle = _crop_avatar_circle(avatar, size=190)
    member_color = _server_color_rgba(member)

    if animated and GIF_BG_PATH.exists():
        bg = Image.open(GIF_BG_PATH)
        frames_out = []
        durations = []

        for idx, frame in enumerate(ImageSequence.Iterator(bg)):
            if idx >= 40:
                break

            fr = frame.convert("RGBA")
            pulse = min(1.0, 0.5 + ((idx % 12) / 12.0))

            composed = _build_overlay(
                fr,
                avatar_circle=avatar_circle,
                display_name=member.display_name,
                subtitle_name=subtitle_name,
                survivor_line=survivor_line,
                member_color=member_color,
                pulse=pulse,
            )
            frames_out.append(composed)

            dur = frame.info.get("duration", 60)
            try:
                dur = int(dur)
            except Exception:
                dur = 60
            durations.append(max(40, min(dur, 120)))

        if frames_out:
            out = io.BytesIO()
            frames_out[0].save(
                out,
                format="GIF",
                save_all=True,
                append_images=frames_out[1:],
                loop=0,
                duration=durations,
                disposal=2,
                optimize=False,
            )
            out.seek(0)
            return out

    if PNG_BG_PATH.exists():
        base = Image.open(PNG_BG_PATH).convert("RGBA")
    else:
        base = Image.new("RGBA", (1200, 400), (16, 16, 20, 255))
        d = ImageDraw.Draw(base)
        d.rectangle((0, 0, 1200, 400), fill=(16, 16, 20, 255))
        d.rectangle((0, 0, 1200, 5), fill=(120, 255, 90, 180))
        d.rectangle((0, 395, 1200, 400), fill=(120, 255, 90, 120))

    final_img = _build_overlay(
        base,
        avatar_circle=avatar_circle,
        display_name=member.display_name,
        subtitle_name=subtitle_name,
        survivor_line=survivor_line,
        member_color=member_color,
        pulse=0.9,
    )

    out = io.BytesIO()
    final_img.save(out, format="PNG")
    out.seek(0)
    return out
# ===== /WELCOME CARD =====

from utils.steam_api import (
    is_valid_steamid64,
    get_player_bans,
    format_ban_details,
)

intents = discord.Intents.default()
intents.members = True
intents.messages = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ====== SEU SERVIDOR ======
GUILD_ID = 1021611772169420820

# ====== CANAIS STAFF (PREENCHA) ======
WHITELIST_LOG_THREAD_ID = int(os.getenv("WHITELIST_LOG_THREAD_ID", "0"))
WHITELIST_APPROVAL_THREAD_ID = int(os.getenv("WHITELIST_APPROVAL_THREAD_ID", "0"))

# ====== CANAIS DO SERVIDOR ======
WELCOME_CHANNEL_ID = 1053423020724736102
RULES_CHANNEL_ID = 1037115525525934252
FAQ_CHANNEL_ID = 1151700680529154058
CONNECT_CHANNEL_ID = 1113486636483883108
WHITELIST_CHANNEL_ID = 1474910191018442762 
VINCULO_CHANNEL_ID=1482819959892480100
# ====== CARGOS ======
ROLE_WAITING_ID = 1474909618206675176       # "Esperando whitelist"
ROLE_SURVIVOR_ID = 1216867549573153010      # "Sobrevivente"

# ====== TESTERS ======
ALLOWED_TESTER_IDS = {1329988627047911465, 777181265324933120}

PANEL_MSG_KEY = "whitelist_panel_message_id"

async def get_thread(bot: commands.Bot, guild: discord.Guild, thread_id: int) -> discord.Thread | None:
    if not thread_id:
        return None

    t = guild.get_thread(thread_id)
    if t is None:
        try:
            ch = await bot.fetch_channel(thread_id)
            t = ch if isinstance(ch, discord.Thread) else None
        except Exception:
            return None

    # garante que não está arquivada (pra não “sumir”)
    try:
        if t.archived:
            await t.edit(archived=False)
    except Exception:
        pass

    return t

def ch_mention(guild: discord.Guild, channel_id: int) -> str:
    ch = guild.get_channel(channel_id)
    return ch.mention if ch else f"(canal {channel_id})"



# ===== EMBED STYLE (somente estética / premium) =====
EMBED_RED = discord.Color.from_rgb(255, 70, 85)
EMBED_DARK = discord.Color.from_rgb(28, 30, 36)
EMBED_ORANGE = discord.Color.from_rgb(255, 153, 51)
EMBED_BLUE = discord.Color.from_rgb(88, 101, 242)
EMBED_GREEN = discord.Color.from_rgb(87, 242, 135)
EMBED_SOFT = discord.Color.from_rgb(43, 45, 49)

BRAND_NAME = "Doom Project"
BRAND_FOOTER = "DOOM BOT • Painel Executivo"


def _line() -> str:
    return "─" * 28


def _line_bold() -> str:
    return "═" * 28


def _spacer() -> str:
    return "‎"  # caractere invisível p/ espaçamento visual


def _mono(text: str) -> str:
    return f"`{text}`"


def _badge(text: str) -> str:
    return f"` {text} `"


def _section_title(icon: str, title: str) -> str:
    return f"{icon} **{title}**"


def _section(title: str, body: str) -> str:
    return f"**{title}**\n{body}"


def _safe_trim(text: str, max_len: int) -> str:
    text = str(text or "")
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def _theme_footer(embed: discord.Embed, label: str):
    embed.set_footer(text=f"{BRAND_FOOTER} • {label}")


def _base_embed(
    *,
    title: str,
    description: str,
    color: discord.Color,
    guild: discord.Guild | None = None,
    thumb_url: str | None = None,
) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_author(
        name="Centro de Operações da Staff" if guild else "Doom Project",
        icon_url=(guild.icon.url if guild and guild.icon else discord.Embed.Empty),
    )
    if thumb_url:
        e.set_thumbnail(url=thumb_url)
    return e


def _status_chip(status: str) -> tuple[str, discord.Color]:
    s = (status or "").lower().strip()
    if s == "pendente":
        return "⏳ PENDENTE", EMBED_ORANGE
    if s == "aprovado":
        return "✅ APROVADO", EMBED_GREEN
    if s == "rejeitado":
        return "❌ REJEITADO", discord.Color.red()
    return f"ℹ️ {s.upper() or 'STATUS'}", EMBED_SOFT


def _format_ban_info_block(ban_info: str | None) -> str | None:
    if not ban_info:
        return None

    raw = str(ban_info).strip()
    if not raw:
        return None

    if raw.startswith("```") and raw.endswith("```"):
        raw = raw[3:-3].strip()
        if raw.startswith(("yaml", "yml", "fix", "ini")):
            raw = raw.split("\n", 1)[1].strip() if "\n" in raw else ""

    parts = []
    for piece in raw.replace("\r", "").split("\n"):
        for sub in piece.split("|"):
            item = sub.strip()
            if item:
                parts.append(item)

    lines = []
    for item in parts:
        if ":" in item:
            k, v = item.split(":", 1)
            key = k.replace("Banned", "").strip()
            val = v.strip().replace("**", "")
            if key.lower() == "days desde último ban":
                key = "Último ban"
            lines.append(f"{key:<10}: {val}")
        else:
            lines.append(item.replace("**", ""))

    body = "\n".join(lines)
    block = f"```yaml\n{body}\n```"
    if len(block) > 1024:
        max_body = 1014 - len("```yaml\n\n```")
        trimmed = body[:max_body].rstrip()
        block = f"```yaml\n{trimmed}\n```"
    return block


def build_dm_embed(guild: discord.Guild) -> discord.Embed:
    desc = (
        f"{_badge(BRAND_NAME)} • {_badge('Onboarding')}\n"
        f"{_mono(_line_bold())}\n"
        "Bem-vindo(a) ao servidor.\n"
        "Para liberar seu acesso, siga o fluxo abaixo:\n\n"
        "➊ **Leia as regras**\n"
        "➋ **Consulte o FAQ**\n"
        "➌ **Veja como conectar**\n"
        "➍ **Envie sua whitelist**\n"
        "➍ **E realize seu vínculo com nosso SV**\n"
        f"{_mono(_line_bold())}"
    )

    e = _base_embed(
        title="🎬 Verificação de Acesso Inicial",
        description=desc,
        color=EMBED_RED,
        guild=guild,
    )

    e.add_field(name=_section_title("📌", "Regras"), value=f"{ch_mention(guild, RULES_CHANNEL_ID)}\n{_spacer()}", inline=True)
    e.add_field(name=_section_title("📚", "FAQ"), value=f"{ch_mention(guild, FAQ_CHANNEL_ID)}\n{_spacer()}", inline=True)
    e.add_field(name=_section_title("🔌", "Conexão"), value=f"{ch_mention(guild, CONNECT_CHANNEL_ID)}\n{_spacer()}", inline=True)
    e.add_field(name=_section_title("🔌", "Vínculo in game"), value=f"{ch_mention(guild, VINCULO_CHANNEL_ID)}\n{_spacer()}", inline=True)

    e.add_field(
        name=_section_title("✅", "Whitelist"),
        value=(
            f"Canal: {ch_mention(guild, WHITELIST_CHANNEL_ID)}\n"
            "Use o botão **✅ Iniciar Whitelist** para abrir o formulário."
        ),
        inline=False,
    )

    e.add_field(
        name=_section_title("⚠️", "Observação"),
        value=(
            "Se a sua DM estiver fechada, use o mesmo botão no canal de whitelist.\n"
            "O processo continua normalmente sem alterar sua análise."
        ),
        inline=False,
    )

    _theme_footer(e, "Onboarding")
    return e


def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    desc = (
        f"{_badge(BRAND_NAME)} • {_badge('Whitelist')}\n"
        f"{_mono(_line_bold())}\n"
        "Clique no botão abaixo para iniciar sua verificação.\n"
        "Envie seus dados corretamente para acelerar a aprovação.\n"
        f"{_mono(_line_bold())}"
    )

    e = _base_embed(
        title="🛡️ Painel de Whitelist",
        description=desc,
        color=EMBED_RED,
        guild=guild,
    )

    e.add_field(
        name=_section_title("🧾", "Dados solicitados"),
        value=(
            "• **SteamID64** (17 dígitos)\n"
            "• **Nome in-game**\n"
            f"{_spacer()}"
        ),
        inline=True,
    )

    e.add_field(
        name=_section_title("🔎", "Checagens"),
        value=(
            "• Formato do SteamID\n"
            "• Consulta de banimentos\n"
            "• Encaminhamento p/ staff (se necessário)"
        ),
        inline=True,
    )

    e.add_field(
        name=_section_title("🔌", "Vínculo in game"),
        value=(
            "• Gera seu link no discord\n"
            "• Cole na UI dentro do Server\n"
            "• E resgate seus itens da loja / VIPS e faça sua imersão"
        ),
        inline=True,
    )

    e.add_field(
        name=_section_title("🔗", "Acesso rápido"),
        value=(
            f"📌 {ch_mention(guild, RULES_CHANNEL_ID)} • Regras\n"
            f"📚 {ch_mention(guild, FAQ_CHANNEL_ID)} • FAQ\n"
            f"🔌 {ch_mention(guild, CONNECT_CHANNEL_ID)} • Conexão"
            f"🔌 {ch_mention(guild, VINCULO_CHANNEL_ID)} • Vínculo in game"
        ),
        inline=False,
    )

    e.add_field(
        name=_section_title("⚠️", "Revisão manual"),
        value=(
            "Se algo for sinalizado na checagem, sua whitelist vai para **aprovação da staff**.\n"
            "Isso é normal e serve para manter o servidor seguro."
        ),
        inline=False,
    )

    _theme_footer(e, "Whitelist Panel")
    return e


def build_log_embed(member: discord.Member, steam: str, ingame: str, status: str, ban_info: str | None):
    status_label, color = _status_chip(status)

    desc = (
        f"{_badge(BRAND_NAME)} • {_badge('Log de Whitelist')}\n"
        f"{_mono(_line())}\n"
        f"{member.mention} • **Entrada recebida**\n"
        f"**ID Discord:** `{member.id}`\n"
        f"**Status:** {_badge(status_label)}"
    )

    e = _base_embed(
        title="📥 Registro de Whitelist",
        description=desc,
        color=color,
        guild=member.guild,
        thumb_url=member.display_avatar.url,
    )

    e.add_field(name=_section_title("🎮", "SteamID64"), value=f"`{steam}`", inline=True)
    e.add_field(name=_section_title("🧍", "Nome In-Game"), value=f"`{_safe_trim(ingame, 32)}`", inline=True)
    e.add_field(name=_section_title("📡", "Origem"), value="`Onboarding / Painel`", inline=True)

    if ban_info:
        e.add_field(
            name=_section_title("🧪", "Checagem / Motivo"),
            value=_format_ban_info_block(ban_info) or "```yaml\nSem detalhes\n```",
            inline=False,
        )

    _theme_footer(e, "Log")
    return e


def build_approval_embed(
    member: discord.Member,
    steam: str,
    ingame: str,
    ban_info: str | None,
    old_discord_id: int | None = None,
    old_status: str | None = None,
):
    desc = (
        f"{_badge(BRAND_NAME)} • {_badge('Revisão Manual')}\n"
        f"{_mono(_line_bold())}\n"
        f"Whitelist enviada por {member.mention}\n"
        f"**ID Discord:** `{member.id}`\n\n"
        "Use os botões abaixo para concluir a decisão da equipe.\n"
        f"{_mono(_line_bold())}"
    )

    e = _base_embed(
        title="🚨 Revisão Manual • Staff",
        description=desc,
        color=EMBED_ORANGE,
        guild=member.guild,
        thumb_url=member.display_avatar.url,
    )

    e.add_field(name=_section_title("🎮", "SteamID64"), value=f"`{steam}`", inline=True)
    e.add_field(name=_section_title("🧍", "Nome In-Game"), value=f"`{_safe_trim(ingame, 32)}`", inline=True)
    e.add_field(name=_section_title("📌", "Ação"), value="`Aprovar / Rejeitar / Transferir`", inline=True)

    if old_discord_id:
        e.add_field(name=_section_title("🔁", "Discord antigo"), value=f"`{old_discord_id}`", inline=True)
        e.add_field(name=_section_title("🆕", "Discord novo"), value=f"`{member.id}`", inline=True)
        e.add_field(name=_section_title("📊", "Status antigo"), value=f"`{old_status or 'desconhecido'}`", inline=True)

    if ban_info:
        e.add_field(
            name=_section_title("⚠️", "Motivo / Banimentos"),
            value=_format_ban_info_block(ban_info) or "```yaml\nSem detalhes\n```",
            inline=False,
        )

    e.add_field(
        name=_section_title("🛠️", "Staff"),
        value=(
            "✅ **Aprovar** → aprova normalmente\n"
            "❌ **Rejeitar** → rejeita o pedido\n"
            "🔁 **Transferir** → move o Steam para o novo Discord"
        ),
        inline=False,
    )

    _theme_footer(e, "Staff Review")
    return e


def _extract_from_embed(msg: discord.Message) -> tuple[int | None, str | None, str | None]:
    """Retorna (discord_id, steam, ingame) lendo do embed da mensagem de aprovação."""
    if not msg.embeds:
        return None, None, None
    emb = msg.embeds[0]

    # tenta pegar o ID do description: "... (`1234567890`)"
    discord_id = None
    if emb.description:
        m = re.search(r"\((\d{17,20})\)", emb.description)
        if m:
            discord_id = int(m.group(1))
        else:
            m2 = re.search(r"(\d{17,20})", emb.description)
            if m2:
                discord_id = int(m2.group(1))

    steam = None
    ingame = None
    for f in emb.fields:
        name = (f.name or "").lower()
        val = (f.value or "").strip().strip("`")
        if "steam" in name:
            steam = val
        if "nome" in name:
            ingame = val

    return discord_id, steam, ingame

class ApproveRejectView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id

    def _is_staff(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        p = interaction.user.guild_permissions
        return p.administrator or p.manage_guild or p.manage_roles

    async def _mark_decided(self, interaction: discord.Interaction, label: str):
        try:
            v = discord.ui.View()
            v.add_item(
                discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    disabled=True
                )
            )
            await interaction.message.edit(view=v)
        except Exception:
            pass

    async def _get_member_from_message(self, msg: discord.Message):
        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            return None, None, None, None

        discord_id, steam, ingame = _extract_from_embed(msg)
        if not discord_id:
            return guild, None, steam, ingame

        member = guild.get_member(discord_id)
        return guild, member, steam, ingame

    def _extract_old_discord_from_embed(self, msg: discord.Message) -> int | None:
        if not msg.embeds:
            return None

        emb = msg.embeds[0]

        # 1) tenta pelo texto do motivo no description
        if emb.description:
            m_old = re.search(r"Discord antigo:\s*(\d{17,20})", emb.description)
            if m_old:
                try:
                    return int(m_old.group(1))
                except Exception:
                    pass

        # 2) tenta pelos campos
        for f in emb.fields:
            value = str(f.value or "")
            m_old = re.search(r"Discord antigo:\s*(\d{17,20})", value)
            if m_old:
                try:
                    return int(m_old.group(1))
                except Exception:
                    pass

        return None

    async def _apply_approved_roles(self, guild: discord.Guild, member: discord.Member, ingame: str | None):
        waiting_role = guild.get_role(ROLE_WAITING_ID)
        survivor_role = guild.get_role(ROLE_SURVIVOR_ID)

        if ingame:
            try:
                await member.edit(nick=ingame)
            except Exception:
                pass

        try:
            if waiting_role and waiting_role in member.roles:
                await member.remove_roles(waiting_role, reason="Whitelist aprovada (staff)")
        except Exception:
            pass

        try:
            if survivor_role and survivor_role not in member.roles:
                await member.add_roles(survivor_role, reason="Whitelist aprovada (staff)")
        except Exception:
            pass

    @discord.ui.button(label="✅ Aprovar", style=discord.ButtonStyle.success, custom_id="doom:approve_btn")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild, member, steam, ingame = await self._get_member_from_message(interaction.message)
        if not guild or not member:
            await interaction.followup.send("Usuário não encontrado no servidor.", ephemeral=True)
            return

        await self._apply_approved_roles(guild, member, ingame)

        await upsert_whitelist(
            member.id,
            steam or "N/A",
            ingame or member.display_name,
            "aprovado",
            "Aprovado manualmente (staff)"
        )

        log_ch = await get_thread(self.bot, guild, WHITELIST_LOG_THREAD_ID)
        if log_ch:
            await log_ch.send(
                embed=build_log_embed(
                    member,
                    steam or "N/A",
                    ingame or member.display_name,
                    "aprovado",
                    "Aprovado manualmente (staff)"
                )
            )

        await self._mark_decided(interaction, "Aprovado ✅")
        await interaction.followup.send("✅ Aprovado.", ephemeral=True)

    @discord.ui.button(label="🔁 Transferir", style=discord.ButtonStyle.primary, custom_id="doom:transfer_btn")
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild, member, steam, ingame = await self._get_member_from_message(interaction.message)
        if not guild or not member or not steam:
            await interaction.followup.send("Não consegui identificar os dados da whitelist.", ephemeral=True)
            return

        old_discord_id = self._extract_old_discord_from_embed(interaction.message)
        if not old_discord_id:
            await interaction.followup.send(
                "Não encontrei o Discord antigo nesse pedido.\n"
                "Confira se o motivo do conflito está presente no embed.",
                ephemeral=True,
            )
            return

        if old_discord_id == member.id:
            await interaction.followup.send("A conta antiga e a nova são a mesma. Nada para transferir.", ephemeral=True)
            return

        await transfer_whitelist_to_new_discord(
            old_discord_id=old_discord_id,
            new_discord_id=member.id,
            steam_id=steam,
            ingame_name=ingame or member.display_name,
            reason=f"Transferência manual feita por staff {interaction.user.id}",
        )

        await self._apply_approved_roles(guild, member, ingame)

        log_ch = await get_thread(self.bot, guild, WHITELIST_LOG_THREAD_ID)
        if log_ch:
            await log_ch.send(
                embed=build_log_embed(
                    member,
                    steam,
                    ingame or member.display_name,
                    "aprovado",
                    f"Steam transferido do Discord {old_discord_id} para {member.id} por {interaction.user.id}"
                )
            )

        await self._mark_decided(interaction, "Transferido 🔁")
        await interaction.followup.send(
            f"✅ Whitelist transferida com sucesso.\n"
            f"SteamID movido de `{old_discord_id}` para `{member.id}`.",
            ephemeral=True,
        )

    @discord.ui.button(label="❌ Rejeitar", style=discord.ButtonStyle.danger, custom_id="doom:reject_btn")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction):
            await interaction.response.send_message("Sem permissão.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild, member, steam, ingame = await self._get_member_from_message(interaction.message)
        if not guild or not member:
            await interaction.followup.send("Usuário não encontrado no servidor.", ephemeral=True)
            return

        await upsert_whitelist(
            member.id,
            steam or "N/A",
            ingame or member.display_name,
            "rejeitado",
            "Rejeitado manualmente (staff)"
        )

        log_ch = await get_thread(self.bot, guild, WHITELIST_LOG_THREAD_ID)
        if log_ch:
            await log_ch.send(
                embed=build_log_embed(
                    member,
                    steam or "N/A",
                    ingame or member.display_name,
                    "rejeitado",
                    "Rejeitado manualmente (staff)"
                )
            )

        await self._mark_decided(interaction, "Rejeitado ❌")
        await interaction.followup.send("❌ Rejeitado.", ephemeral=True)

class WhitelistModal(discord.ui.Modal, title="Whitelist"):
    steam_id = discord.ui.TextInput(label="ID STEAM (SteamID64)", placeholder="7656119...", required=True, max_length=32)
    ingame_name = discord.ui.TextInput(label="NOME IN-GAME", placeholder="DoomDiana", required=True, max_length=32)

    def __init__(self, bot: commands.Bot, guild_id: int, user_id: int, source_message: discord.Message | None):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.source_message = source_message

    async def _disable_dm_button(self):
        try:
            if self.source_message and isinstance(self.source_message.channel, discord.DMChannel):
                v = discord.ui.View()
                v.add_item(discord.ui.Button(label="Whitelist enviada ✅", style=discord.ButtonStyle.secondary, disabled=True))
                await self.source_message.edit(view=v)
        except Exception:
            pass

    async def on_submit(self, interaction: discord.Interaction):
        guild = self.bot.get_guild(self.guild_id)
        if not guild:
            await interaction.response.send_message("Erro: servidor não encontrado.", ephemeral=True)
            return

        member = guild.get_member(self.user_id)
        if not member:
            await interaction.response.send_message("Erro: não encontrei você no servidor.", ephemeral=True)
            return

        steam = str(self.steam_id.value).strip()
        ingame = str(self.ingame_name.value).strip()

        if not is_valid_steamid64(steam):
            await interaction.response.send_message(
                "❌ **SteamID inválido.**\n\n"
                "Envie seu **SteamID64** (somente números, **17 dígitos**).\n\n"
                "Exemplo:\n"
                "`7656119XXXXXXXXXX`\n\n"
                "Clique novamente no botão e tente de novo.",
                ephemeral=True,
            )
            return

        log_ch = await get_thread(self.bot, guild, WHITELIST_LOG_THREAD_ID)
        appr_ch = await get_thread(self.bot, guild, WHITELIST_APPROVAL_THREAD_ID)

        # conflito: steam já usado em outro discord
        conflict = await get_active_whitelist_conflict_by_steam_id(steam, member.id)
        if conflict:
            old_discord_id = int(conflict["discord_id"])
            old_status = str(conflict.get("status") or "").lower().strip()

            motivo = (
                f"SteamID já vinculado a outro Discord.\n"
                f"Discord atual: {member.id}\n"
                f"Discord antigo: {old_discord_id}\n"
                f"Status antigo: {old_status}"
            )

            await upsert_whitelist(member.id, steam, ingame, "pendente", motivo)

            if log_ch:
                await log_ch.send(
                    embed=build_log_embed(member, steam, ingame, "pendente", motivo)
                )

            if appr_ch:
                await appr_ch.send(
                    content="⚠️ **Whitelist bloqueada para revisão manual (SteamID duplicado em outro Discord)**",
                    embed=build_approval_embed(
                        member,
                        steam,
                        ingame,
                        motivo,
                        old_discord_id=old_discord_id,
                        old_status=old_status,
                    ),
                    view=ApproveRejectView(self.bot, GUILD_ID),
                )

            await self._disable_dm_button()
            await interaction.response.send_message(
                "⚠️ Este SteamID já está associado a outra conta Discord.\n"
                "Sua whitelist foi enviada para análise manual da staff.",
                ephemeral=True,
            )
            return

        # anti-duplicado por discord
        existing = await get_whitelist_status(member.id)
        if existing:
            status, created_at = existing
            if status in ("pendente", "aprovado"):
                await interaction.response.send_message(
                    f"✅ Você já enviou whitelist (**{status}**) em `{created_at}`.",
                    ephemeral=True,
                )
                await self._disable_dm_button()
                return

        # 2) checa banimentos
        api_key = os.getenv("STEAM_API_KEY", "").strip()
        bans = await get_player_bans(api_key, steam)

        if bans is None:
            ban_info = "Não consegui checar banimentos agora (sem API Key ou erro na Steam API)."
            precisa_aprovacao = True
        else:
            precisa_aprovacao, ban_info = format_ban_details(bans)

        # 3) decide fluxo
        if precisa_aprovacao:
            await upsert_whitelist(member.id, steam, ingame, "pendente", ban_info)

            if log_ch:
                await log_ch.send(embed=build_log_embed(member, steam, ingame, "pendente", ban_info))
            if appr_ch:
                await appr_ch.send(
                    content="⚠️ **Pendente para aprovação**",
                    embed=build_approval_embed(member, steam, ingame, ban_info),
                    view=ApproveRejectView(self.bot, GUILD_ID),
                )

            await self._disable_dm_button()
            await interaction.response.send_message(
                "✅ Recebi sua whitelist!\nEla foi enviada para **aprovação de administradores**.",
                ephemeral=True,
            )
            return

        # 4) aprova automático (sem ban)
        waiting_role = guild.get_role(ROLE_WAITING_ID)
        survivor_role = guild.get_role(ROLE_SURVIVOR_ID)

        try:
            await member.edit(nick=ingame)
        except Exception:
            pass

        try:
            if waiting_role and waiting_role in member.roles:
                await member.remove_roles(waiting_role, reason="Whitelist aprovada automática")
        except Exception:
            pass

        try:
            if survivor_role and survivor_role not in member.roles:
                await member.add_roles(survivor_role, reason="Whitelist aprovada automática")
        except Exception:
            pass

        await upsert_whitelist(member.id, steam, ingame, "aprovado", ban_info)

        if log_ch:
            await log_ch.send(embed=build_log_embed(member, steam, ingame, "aprovado", ban_info))

        await self._disable_dm_button()
        await interaction.response.send_message(
            "✅ **Whitelist aprovada automaticamente!** Você já recebeu o cargo e acesso.",
            ephemeral=True,
        )

class WhitelistView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id

    @discord.ui.button(label="✅ Iniciar Whitelist", style=discord.ButtonStyle.success, custom_id="doom:whitelist_btn")
    async def whitelist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # bloqueia duplicado
        existing = await get_whitelist_status(interaction.user.id)
        if existing:
            status, created_at = existing
            if status in ("pendente", "aprovado"):
                await interaction.response.send_message(
                    f"✅ Você já enviou whitelist (**{status}**) em `{created_at}`.\n"
                    "Se precisar alterar dados, chame a staff.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(
            WhitelistModal(self.bot, self.guild_id, interaction.user.id, source_message=interaction.message)
        )


class Onboarding(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.view = WhitelistView(bot, GUILD_ID)
        self._panel_recreate_lock = asyncio.Lock()

    async def cog_load(self):
        # mantém botão persistente após reinício
        self.bot.add_view(self.view)
        self.bot.add_view(ApproveRejectView(self.bot, GUILD_ID))

    async def _maybe_restore_panel_by_deleted_message(self, guild_id: int | None, channel_id: int, message_id: int):
        # só reage no seu servidor/canal do painel
        if guild_id != GUILD_ID:
            return
        if channel_id != WHITELIST_CHANNEL_ID:
            return

        saved = await get_config(PANEL_MSG_KEY)
        if not saved:
            return

        try:
            saved_id = int(saved)
        except Exception:
            return

        if message_id != saved_id:
            return

        # evita corrida / spam se rolar delete em massa
        async with self._panel_recreate_lock:
            # double-check (alguém pode ter recriado entre o evento e o lock)
            saved2 = await get_config(PANEL_MSG_KEY)
            if not saved2:
                return
            try:
                if int(saved2) != message_id:
                    return
            except Exception:
                return

            await self.ensure_whitelist_panel()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self._maybe_restore_panel_by_deleted_message(
            payload.guild_id,
            payload.channel_id,
            payload.message_id,
        )

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        # se alguém limpar mensagens do canal, checa todas
        for mid in payload.message_ids:
            await self._maybe_restore_panel_by_deleted_message(
                payload.guild_id,
                payload.channel_id,
                mid,
            )

    async def ensure_whitelist_panel(self):
        guild = self.bot.get_guild(GUILD_ID)
        if not guild:
            return

        channel = guild.get_channel(WHITELIST_CHANNEL_ID)
        if not channel:
            return

        # tenta buscar msg salva
        saved = await get_config(PANEL_MSG_KEY)
        if saved:
            try:
                await channel.fetch_message(int(saved))
                return
            except Exception:
                pass

        # se não existe: repostar
        msg = await channel.send(embed=build_panel_embed(guild), view=self.view)
        try:
            await msg.pin(reason="Painel de whitelist")
        except Exception:
            pass

        await set_config(PANEL_MSG_KEY, str(msg.id))

    async def send_onboarding_dm(self, member: discord.Member) -> bool:
        try:
            await member.send(embed=build_dm_embed(member.guild), view=self.view)
            return True
        except Exception:
            return False

    @commands.command(name="teste_whitelist")
    async def teste_whitelist(self, ctx: commands.Context, membro: discord.Member | None = None):
        if ctx.author.id not in ALLOWED_TESTER_IDS:
            return
        ok_me = await self.send_onboarding_dm(ctx.author)
        ok_other = None
        if membro is not None:
            if membro.id not in ALLOWED_TESTER_IDS:
                await ctx.reply("Esse usuário não está autorizado para teste.", delete_after=8)
                return
            ok_other = await self.send_onboarding_dm(membro)

        msg = "✅ DM enviada pra você." if ok_me else "⚠️ Não consegui te mandar DM (privacidade)."
        if ok_other is not None:
            msg += "\n✅ DM enviada pro outro tester." if ok_other else "\n⚠️ DM do outro tester bloqueada."
        await ctx.reply(msg, delete_after=10)

    @commands.Cog.listener()
    async def on_ready(self):
        # garante painel sempre que o bot liga
        await self.ensure_whitelist_panel()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        waiting_role = member.guild.get_role(ROLE_WAITING_ID)
        if waiting_role:
            try:
                await member.add_roles(waiting_role, reason="Entrou no servidor (onboarding)")
            except Exception:
                pass

        channel = member.guild.get_channel(WELCOME_CHANNEL_ID)
        if channel:
            try:
                survivor_num = member.guild.member_count
                subtitle = member.display_name
                line = f"SOBREVIVENTE N {survivor_num}"
                card = await make_welcome_card(
                    member,
                    subtitle_name=subtitle,
                    survivor_line=line,
                    animated=True
                )

                head = card.getvalue()[:6]
                is_gif = head in (b"GIF87a", b"GIF89a")

                filename = "welcome.gif" if is_gif else "welcome.png"
                file = discord.File(fp=card, filename=filename)

                await channel.send(content=f"👋 {member.mention}", file=file)
            except Exception:
                pass

        ok = await self.send_onboarding_dm(member)
        if not ok and channel:
            await channel.send(
                f"{member.mention} não consegui te mandar DM (provavelmente bloqueada). "
                f"Use o botão em {ch_mention(member.guild, WHITELIST_CHANNEL_ID)} ✅"
            )

    @app_commands.command(
        name="reenviar_onboarding",
        description="Reenvia DM apenas para quem está em 'Esperando whitelist' (mais seguro)."
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def reenviar_onboarding(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "⏳ Reenviando DM apenas para quem está em **Esperando whitelist**.",
            ephemeral=True,
        )
        guild = interaction.guild
        if not guild:
            return

        waiting_role = guild.get_role(ROLE_WAITING_ID)
        if not waiting_role:
            await interaction.followup.send("Cargo 'Esperando whitelist' não encontrado.", ephemeral=True)
            return

        sent, failed = 0, 0
        for m in waiting_role.members:
            if m.bot:
                continue
            ok = await self.send_onboarding_dm(m)
            sent += 1 if ok else 0
            failed += 0 if ok else 1
            await asyncio.sleep(0.6)

        await interaction.followup.send(
            f"✅ Finalizado.\nEnviadas: **{sent}**\nFalharam (DM bloqueada): **{failed}**",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))