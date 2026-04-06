from __future__ import annotations

import os
import asyncio
import discord
from discord.ext import commands

from utils.db import (
    get_discord_balance,
    has_active_player_link,
    get_config,
    set_config,
)

# ==============================================================================
# CONFIGURAÇÕES E CONSTANTES
# ==============================================================================
ECONOMIA_PANEL_CHANNEL_KEY = "economia_panel_channel_id"
ECONOMIA_PANEL_MESSAGE_KEY = "economia_panel_message_id"

# Puxa do seu .env! Se não existir, fica 0 (desativado até configurar)
DEFAULT_ECONOMIA_PANEL_CHANNEL_ID = int(os.getenv("ECONOMIA_PANEL_CHANNEL_ID", "0"))

BTN_CHECK_BALANCE = "economia:check_balance"

BRAND_NAME = "Doom Project"
BRAND_FOOTER = "DOOM BOT • Painel Executivo"
EMBED_RED = discord.Color.from_rgb(220, 53, 69)

# ==============================================================================
# UTILITÁRIOS VISUAIS
# ==============================================================================
def _line_bold() -> str:
    return "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"

def _spacer() -> str:
    return "‎"

def _mono(text: str) -> str:
    return f"`{text}`"

def _badge(text: str) -> str:
    return f"` {text} `"

def _theme_footer(embed: discord.Embed, label: str):
    embed.set_footer(text=f"{BRAND_FOOTER} • {label} • Hoje às {discord.utils.utcnow().strftime('%H:%M')}")

def _base_embed(
    *,
    title: str,
    description: str,
    color: discord.Color,
    guild: discord.Guild | None = None,
) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color)
    e.set_author(
        name="Centro de Operações da Staff" if guild else "Centro de Operações da Staff",
        icon_url=(guild.icon.url if guild and guild.icon else discord.Embed.Empty),
    )
    return e

# ==============================================================================
# CONSTRUTORES DE EMBEDS
# ==============================================================================
def build_economia_panel_embed(guild: discord.Guild) -> discord.Embed:
    desc = (
        f"{_badge(BRAND_NAME)} • {_badge('Banco Central')}\n"
        f"{_mono(_line_bold())}\n"
        "Bem-vindo(a) ao Banco Central de Sobrevivência.\n"
        "Aqui você pode consultar o saldo da sua conta de forma privada e segura.\n"
        f"{_mono(_line_bold())}"
    )

    e = _base_embed(
        title="📌 Painel Financeiro",
        description=desc,
        color=EMBED_RED,
        guild=guild,
    )

    e.add_field(
        name="🧾 Como funciona o dinheiro?",
        value=(
            "Ao jogar no servidor, você ganha coins automaticamente das seguintes formas:\n\n"
            "• 🧟 **Matar Zumbis:** `+100 Coins` por abate.\n"
            "• ⚔️ **Abater Sobreviventes (PvP):** `+300 Coins` por abate.\n"
            "• ⏳ **Tempo Sobrevivido:** `+10 Coins` por minuto logado *(pago automaticamente ao sair do jogo)*.\n\n"
            "*O seu saldo fica guardado no Discord, totalmente protegido, mesmo se o seu personagem morrer.*"
        ),
        inline=False,
    )

    e.add_field(
        name="🔒 Privacidade",
        value=(
            "Ao clicar no botão abaixo, uma janela que **apenas você consegue ver** será aberta.\n"
            "Ela vai se autodestruir após 4 minutos para a sua segurança."
        ),
        inline=False,
    )

    _theme_footer(e, "Painel de Economia")
    return e

def build_saldo_embed(user: discord.abc.User, saldo: int, guild: discord.Guild | None = None) -> discord.Embed:
    desc = (
        f"{_badge(BRAND_NAME)} • {_badge('Extrato de Conta')}\n"
        f"{_mono(_line_bold())}\n"
        f"Olá {user.mention}!\n"
        "Este é o extrato atualizado da sua carteira.\n"
        f"{_mono(_line_bold())}"
    )

    e = _base_embed(
        title="💳 A sua Carteira",
        description=desc,
        color=EMBED_RED,
        guild=guild,
    )

    e.add_field(
        name="💰 Saldo Atual",
        value=f"```ansi\n\u001b[1;33m{saldo} Coins\u001b[0m\n```",
        inline=False,
    )
    
    e.add_field(
        name="⚠️ Informação",
        value="Esta mensagem vai desaparecer em 4 minutos.",
        inline=False,
    )

    e.set_thumbnail(url=user.display_avatar.url)
    _theme_footer(e, f"Carteira de {user.display_name}")
    return e

# ==============================================================================
# BOTÕES E INTERFACE
# ==============================================================================
class EconomiaPanelView(discord.ui.View):
    def __init__(self, cog: "EconomiaCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Consultar Saldo",
        style=discord.ButtonStyle.success,
        custom_id=BTN_CHECK_BALANCE,
        emoji="💳"
    )
    async def check_balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_linked = await has_active_player_link(interaction.user.id)
        if not is_linked:
            await interaction.response.send_message(
                "⚠️ **Conta não vinculada!**\nVocê precisa vincular a sua conta do Discord ao jogo no Painel de Vínculo para acessar o banco.",
                ephemeral=True
            )
            return

        saldo_atual = await get_discord_balance(interaction.user.id)
        embed = build_saldo_embed(interaction.user, saldo_atual, interaction.guild)

        # Envia de forma "Efémera" (Só o usuário vê)
        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Temporizador de 4 Minutos para apagar a janela
        await asyncio.sleep(240)
        try:
            await interaction.delete_original_response()
        except discord.NotFound:
            pass

# ==============================================================================
# COG PRINCIPAL
# ==============================================================================
class EconomiaCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.panel_view = EconomiaPanelView(self)
        self._restore_lock = False

    async def cog_load(self) -> None:
        self.bot.add_view(self.panel_view)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.restore_economia_panel()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        try:
            saved_id = await get_config(ECONOMIA_PANEL_MESSAGE_KEY)
            if saved_id and payload.message_id == int(saved_id):
                await set_config(ECONOMIA_PANEL_MESSAGE_KEY, "")
                await self.restore_economia_panel()
        except Exception:
            pass

    async def restore_economia_panel(self) -> None:
        if self._restore_lock:
            return
        self._restore_lock = True
        try:
            channel_id = await self._resolve_panel_channel_id()
            if not channel_id or channel_id == 0:
                return

            channel = self.bot.get_channel(channel_id)
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except Exception:
                    return

            # Permite tanto canais de texto padrão quanto tópicos de Fórum/Threads
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return

            embed = build_economia_panel_embed(channel.guild)
            message_id_raw = await get_config(ECONOMIA_PANEL_MESSAGE_KEY)

            if message_id_raw:
                try:
                    msg = await channel.fetch_message(int(message_id_raw))
                    await msg.edit(embed=embed, view=self.panel_view)
                    return
                except Exception:
                    await set_config(ECONOMIA_PANEL_MESSAGE_KEY, "")

            msg = await channel.send(embed=embed, view=self.panel_view)
            await set_config(ECONOMIA_PANEL_MESSAGE_KEY, str(msg.id))
        finally:
            self._restore_lock = False

    async def _resolve_panel_channel_id(self) -> int:
        configured = await get_config(ECONOMIA_PANEL_CHANNEL_KEY)
        target_channel = DEFAULT_ECONOMIA_PANEL_CHANNEL_ID
        
        if configured and str(configured) != str(target_channel):
            await set_config(ECONOMIA_PANEL_MESSAGE_KEY, "")
            
        await set_config(ECONOMIA_PANEL_CHANNEL_KEY, str(target_channel))
        return target_channel

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomiaCog(bot))