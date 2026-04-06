from __future__ import annotations

import os
import discord
from discord.ext import commands

# ==============================================================================
# IMPORTS DO SEU BANCO DE DADOS
# ==============================================================================
from utils.db import (
    regenerate_link_code,
    get_link_code_by_discord_id,
    get_whitelist_status,
    touch_link_code_sent,
    get_active_player_link,
    deactivate_player_link_by_discord_id,
    delete_link_code_by_discord_id,
    get_config,
    set_config,
)

# ==============================================================================
# CONFIGURAÇÕES E CONSTANTES
# ==============================================================================
LINK_PANEL_CHANNEL_KEY = "link_panel_channel_id"
LINK_PANEL_MESSAGE_KEY = "link_panel_message_id"

DEFAULT_LINK_PANEL_CHANNEL_ID = int(os.getenv("LINK_PANEL_CHANNEL_ID", "1482819959892480100"))
TICKET_THREADS_CHANNEL_ID = int(os.getenv("TICKET_THREADS_CHANNEL_ID", "1474977729647607878"))

BTN_GENERATE = "linkpanel:generate_code"
BTN_UNLINK = "linkpanel:unlink_account"
BTN_HELP = "linkpanel:help"
BTN_DM_UNLINK = "linkpanel_dm:unlink_account"

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
        icon_url=(guild.icon.url if guild and guild.icon else None),
    )
    return e

# ==============================================================================
# CONSTRUTORES DE EMBEDS
# ==============================================================================
def build_link_panel_embed(guild: discord.Guild) -> discord.Embed:
    desc = (
        f"{_badge(BRAND_NAME)} • {_badge('Vínculo')}\n"
        f"{_mono(_line_bold())}\n"
        "Clique no botão abaixo para gerar o seu código pessoal de vínculo.\n"
        "Esse código será usado para conectar o seu personagem do jogo ao seu perfil no Discord.\n"
        f"{_mono(_line_bold())}"
    )

    e = _base_embed(title="📌 Painel de Vínculo", description=desc, color=EMBED_RED, guild=guild)

    e.add_field(
        name="🧾 Como funciona",
        value="• Clique em **Vincular**\n• O código será enviado por **DM**\n• Depois, informe esse código na interface dentro do jogo\n" + _spacer(),
        inline=True,
    )

    e.add_field(
        name="🛡️ Regras do código",
        value="• O seu código é **único e pessoal**\n• Ele **não expira sozinho**\n• Ao gerar outro, o código anterior é invalidado\n• **Não partilhe** esse código",
        inline=True,
    )

    e.add_field(
        name="🎁 O que esse vínculo liberta",
        value="• Perfil no Discord\n• Loja e resgates\n• VIP e recompensas\n• Economia e futuras integrações",
        inline=False,
    )

    e.add_field(
        name="🆘 Revisão / ajuda",
        value="Se tiver qualquer dúvida, clique em **Preciso de ajuda**.\nIsso vai abrir um atendimento privado com a staff.",
        inline=False,
    )

    _theme_footer(e, "Vínculo Panel")
    return e

def build_dm_embed(user: discord.abc.User, code: str, guild: discord.Guild | None = None) -> discord.Embed:
    desc = (
        f"{_badge(BRAND_NAME)} • {_badge('Código de Vínculo')}\n"
        f"{_mono(_line_bold())}\n"
        "O seu código pessoal foi gerado com sucesso.\n"
        "Guarde esta mensagem para usar quando a interface dentro do jogo estiver disponível.\n"
        f"{_mono(_line_bold())}"
    )

    e = _base_embed(title="📌 Código de Vínculo", description=desc, color=EMBED_RED, guild=guild)

    e.add_field(name="🎫 O seu código", value=f"```ansi\n\u001b[1;33m{code}\u001b[0m\n```", inline=False)
    e.add_field(name="🕹️ Como usar", value="1. Entre no servidor\n2. Abra a interface de vínculo dentro do jogo\n3. Informe o código acima\n4. Confirme o vínculo", inline=False)
    e.add_field(name="⚠️ Importante", value="Esse código identifica a sua conta do Discord.\n**Não partilhe** com ninguém.\nSe gerar outro código, este deixa de funcionar.\nSe precisar de ajuda, volte ao painel e clique em **Preciso de ajuda**.", inline=False)

    _theme_footer(e, f"Código de {user.display_name}")
    return e

# ==============================================================================
# FORMULÁRIO MODAL DE AJUDA E BOTÕES
# ==============================================================================
class LinkHelpModal(discord.ui.Modal, title="Solicitar Ajuda de Vínculo"):
    problem = discord.ui.TextInput(
        label="Qual é a sua dúvida ou problema?",
        style=discord.TextStyle.paragraph,
        placeholder="Ex: Não consigo colocar o código no jogo ou perdi a minha conta...",
        required=True,
        min_length=10,
        max_length=500
    )

    def __init__(self, cog: "LinkPanelCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.process_help_ticket(interaction, self.problem.value)

class DMUnlinkView(discord.ui.View):
    def __init__(self, cog: "LinkPanelCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Desvincular conta", style=discord.ButtonStyle.danger, custom_id=BTN_DM_UNLINK, emoji="🔒")
    async def unlink_account_dm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_unlink_request(interaction)

class LinkPanelView(discord.ui.View):
    def __init__(self, cog: "LinkPanelCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Vincular", style=discord.ButtonStyle.success, custom_id=BTN_GENERATE, emoji="🔐")
    async def generate_code(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_generate_code(interaction)

    @discord.ui.button(label="Desvincular conta", style=discord.ButtonStyle.danger, custom_id=BTN_UNLINK, emoji="🔓")
    async def unlink_account(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_unlink_request(interaction)

    @discord.ui.button(label="Preciso de ajuda", style=discord.ButtonStyle.secondary, custom_id=BTN_HELP, emoji="🆘")
    async def help_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(LinkHelpModal(self.cog))

# ==============================================================================
# COG PRINCIPAL
# ==============================================================================
class LinkPanelCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.panel_view = LinkPanelView(self)
        self.dm_view = DMUnlinkView(self)
        self._restore_lock = False

    async def cog_load(self) -> None:
        self.bot.add_view(self.panel_view)
        self.bot.add_view(self.dm_view)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.restore_link_panel()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        try:
            saved_id = await get_config(LINK_PANEL_MESSAGE_KEY)
            if saved_id and payload.message_id == int(saved_id):
                await set_config(LINK_PANEL_MESSAGE_KEY, "")
                await self.restore_link_panel()
        except Exception:
            pass

    async def restore_link_panel(self) -> None:
        if self._restore_lock: return
        self._restore_lock = True
        try:
            channel_id = await self._resolve_panel_channel_id()
            if not channel_id: return
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            if not isinstance(channel, discord.TextChannel): return

            embed = build_link_panel_embed(channel.guild)
            message_id_raw = await get_config(LINK_PANEL_MESSAGE_KEY)

            if message_id_raw:
                try:
                    msg = await channel.fetch_message(int(message_id_raw))
                    await msg.edit(embed=embed, view=self.panel_view)
                    return
                except Exception:
                    await set_config(LINK_PANEL_MESSAGE_KEY, "")

            msg = await channel.send(embed=embed, view=self.panel_view)
            await set_config(LINK_PANEL_MESSAGE_KEY, str(msg.id))
        finally:
            self._restore_lock = False

    async def _resolve_panel_channel_id(self) -> int:
        configured = await get_config(LINK_PANEL_CHANNEL_KEY)
        target_channel = DEFAULT_LINK_PANEL_CHANNEL_ID
        if configured and str(configured) != str(target_channel):
            await set_config(LINK_PANEL_MESSAGE_KEY, "")
        await set_config(LINK_PANEL_CHANNEL_KEY, str(target_channel))
        return target_channel

    # ==========================================================================
    # LOGICA MELHORADA - COM FEEDBACK VISUAL DE ERROS DE DATABASE
    # ==========================================================================
    async def handle_generate_code(self, interaction: discord.Interaction) -> None:
        assert interaction.user is not None
        
        print(f"\n[DEBUG] --- NOVO CLIQUE EM VINCULAR ---")
        print(f"[DEBUG] Usuário: {interaction.user.name} ({interaction.user.id})")
        
        await interaction.response.send_message("⏳ A comunicar com o banco de dados...", ephemeral=True)
        print("[DEBUG] 1. Mensagem de carregamento enviada no Discord.")
        
        try:
            print("[DEBUG] 2. Entrando na função regenerate_link_code (Aguardando Banco de Dados)...")
            code_row = await regenerate_link_code(interaction.user.id)
            code = str(code_row["link_code"])
            print(f"[DEBUG] 3. Código gerado com sucesso: {code}")
            
            dm_embed = build_dm_embed(interaction.user, code, interaction.guild)
            print("[DEBUG] 4. Embed construída.")

            dm = interaction.user.dm_channel or await interaction.user.create_dm()
            print("[DEBUG] 5. Canal de DM aberto.")
            
            await dm.send(embed=dm_embed, view=self.dm_view)
            print("[DEBUG] 6. Mensagem enviada na DM.")
            
            await touch_link_code_sent(interaction.user.id)
            print("[DEBUG] 7. Data de envio atualizada no BD (touch).")
            
            active_link = await get_active_player_link(interaction.user.id)
            extra = "\n⚠️ *Nota: Já possui um vínculo ativo. Usar este novo código invalidará o anterior.*" if active_link else ""
            
            await interaction.edit_original_response(content=f"✅ **Enviei-lhe o seu código por DM.**{extra}")
            print("[DEBUG] 8. TUDO CERTO! Processo finalizado.\n")
            
        except discord.Forbidden:
            print("[DEBUG] ERRO: DM bloqueada pelo usuário.")
            await interaction.edit_original_response(content="❌ **Erro:** Não consegui enviar a DM! Ative as suas mensagens privadas do servidor para receber o código.")
        except Exception as e:
            print(f"[DEBUG] ERRO CRÍTICO NO CÓDIGO: {type(e).__name__} - {e}")
            await interaction.edit_original_response(content=f"❌ **ERRO GRAVE:**\n`{type(e).__name__}: {e}`")

    async def handle_unlink_request(self, interaction: discord.Interaction) -> None:
        assert interaction.user is not None
        
        # 1. Defer avoids the Discord "Interaction Failed" timeout
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            active_link = await get_active_player_link(interaction.user.id)
            if not active_link:
                await interaction.edit_original_response(content="⚠️ A sua conta não possui nenhum vínculo ativo no momento.")
                return

            result = await deactivate_player_link_by_discord_id(interaction.user.id)
            await delete_link_code_by_discord_id(interaction.user.id)
            
            # ==========================================================
            # 2. AVISAR O JOGO IMEDIATAMENTE QUE FOI DESVINCULADO
            # ==========================================================
            pz_steam_id = result.get("pz_reported_steam_id")
            username = active_link.get("username", "Desconhecido")
            
            if pz_steam_id:
                payload = {
                    "event_id": f"unlink_{interaction.user.id}",
                    "discord_id": interaction.user.id,
                    "steam_id": pz_steam_id,
                    "code": "",
                    "linked": False, # Sends the signal to drop the link in-game
                    "message": "Conta desvinculada pelo Discord",
                    "username": username
                }
                try:
                    import aiohttp
                    agent_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:9000")
                    agent_key = os.getenv("PZ_AGENT_API_KEY", "")
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.post(f"{agent_url}/link/result", json=payload, headers={"x-api-key": agent_key}) as resp:
                            pass 
                except Exception as api_err:
                    print(f"[UNLINK] Erro ao avisar a API: {api_err}")
            # ==========================================================

            await interaction.edit_original_response(content="✅ A sua conta foi desvinculada com sucesso em todos os sistemas.")
        except Exception as e:
            import traceback
            traceback.print_exc()
            await interaction.edit_original_response(content=f"❌ **Erro no banco de dados:** `{e}`")

    async def process_help_ticket(self, interaction: discord.Interaction, problem_description: str) -> None:
        assert interaction.user is not None
        
        if not interaction.response.is_done():
            await interaction.response.send_message("⏳ A criar o seu ticket...", ephemeral=True)
            
        try:
            tickets_cog = self.bot.get_cog("TicketThreadsCog")
            if not tickets_cog:
                await interaction.edit_original_response(content="❌ O sistema de tickets não está carregado agora. Contacte a staff.")
                return

            active_code = await get_link_code_by_discord_id(interaction.user.id)
            linked = await get_active_player_link(interaction.user.id)
            whitelist_status = await get_whitelist_status(interaction.user.id)

            initial_embed = discord.Embed(
                title="🆘 Ajuda de Vínculo Solicitada",
                description=f"**Membro:** {interaction.user.mention}\n\n**Problema Relatado:**\n```{problem_description}```\n\nEquipa Staff, utilizem esta thread para orientar o jogador.",
                color=EMBED_RED
            )
            _theme_footer(initial_embed, "Ticket de Suporte (Vínculo)")

            thread = await tickets_cog.open_link_help_ticket(
                member=interaction.user,
                whitelist_status=whitelist_status,
                active_code=active_code,
                linked=bool(linked),
                initial_embed=initial_embed,
                reason="panel_help_button",
                link_code=(active_code.get("link_code") if active_code else None),
                link_expires_at=None,
            )
            await interaction.edit_original_response(content=f"✅ O seu ticket privado com a staff foi aberto! Aceda aqui: {thread.mention}")
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ **Erro ao tentar abrir o seu ticket:** `{type(e).__name__} - {e}`")

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LinkPanelCog(bot))