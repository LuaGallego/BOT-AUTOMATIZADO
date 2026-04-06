import os
import discord
import aiohttp
import asyncio
import traceback
from discord.ext import commands, tasks
from utils.db import get_config, set_config

# Importação compatível com rcon 2.4.9
try:
    from rcon.asyncio import rcon as rcon_execute
except ImportError:
    from rcon import rcon as rcon_execute

# --- FUNÇÃO DE CONTROLE RAZEHOST (Segura no .env) ---
async def ptero_control(signal: str):
    url = f"{os.getenv('PTERO_URL')}/api/client/servers/{os.getenv('PTERO_SERVER_ID')}/power"
    headers = {
        "Authorization": f"Bearer {os.getenv('PTERO_API_KEY')}",
        "Content-Type": "application/json",
        "Accept": "application/vnd.pterodactyl.v1+json"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"signal": signal}, headers=headers, timeout=12) as resp:
                return resp.status in (204, 200)
    except Exception as e:
        print(f"[AdminPZ] Erro API RazeHost: {e}")
        return False

# --- MODAIS ESTILIZADOS ---
class GlobalMsgModal(discord.ui.Modal, title="📣 ENVIAR ALERTA GLOBAL"):
    msg = discord.ui.TextInput(
        label="Texto da Mensagem (Broadcast)", 
        style=discord.TextStyle.paragraph, 
        placeholder="Servidor será reiniciado",
        min_length=5, max_length=500
    )
    def __init__(self, cog): super().__init__(); self.cog = cog
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await rcon_execute(f"servermsg \"{self.msg.value}\"", host=os.getenv("PZ_RCON_HOST"), port=int(os.getenv("PZ_RCON_PORT")), passwd=os.getenv("PZ_RCON_PASSWORD"))
            await interaction.followup.send("✅ Alerta RCON enviado com sucesso!", ephemeral=True)
        except: await interaction.followup.send("❌ Servidor Offline ou RCON inacessível.", ephemeral=True)

class BanSteamModal(discord.ui.Modal, title="🔨 BANIMENTO DEFINITIVO"):
    sid = discord.ui.TextInput(
        label="SteamID64 do Alvo", 
        min_length=17, max_length=17, 
        placeholder="7656119xxxxxxxxxx",
        required=True
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Avisa o Discord que estamos processando (evita "interação falhou")
        await interaction.response.defer(ephemeral=True)
        steam_id = self.sid.value.strip()

        try:
            # 2. Comando RCON para o Project Zomboid
            cmd = f"banid {steam_id}"
            await rcon_execute(
                cmd, 
                host=os.getenv("PZ_RCON_HOST"), 
                port=int(os.getenv("PZ_RCON_PORT", 27015)), 
                passwd=os.getenv("PZ_RCON_PASSWORD")
            )

            # 3. Grava na sua tabela 'server_bans' (conforme o print do seu SQL)
            # Nota: Verifique se os nomes das colunas são exatamente esses
            from utils.db import execute
            await execute(
                "INSERT INTO server_bans (steamid, admin_id, date) VALUES (?, ?, datetime('now'))",
                (steam_id, str(interaction.user.id))
            )

            await interaction.followup.send(f"✅ Sucesso! O ID `{steam_id}` foi banido do jogo e registrado no SQL.", ephemeral=True)
            print(f"[AdminPZ] Banimento realizado: {steam_id} por {interaction.user.name}")

        except Exception as e:
            # Se der erro, ele vai imprimir no seu terminal pra gente saber o motivo
            print(f"[AdminPZ] ERRO NO BANIMENTO: {e}")
            traceback.print_exc() 
            await interaction.followup.send(f"❌ Erro ao banir! Verifique o console do bot.\n`{e}`", ephemeral=True)

# --- VIEW PREMIUM COM BOTÕES ORGANIZADOS ---
class AdminButtons(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def notify_all(self, title, message, color):
        """Envia alertas automáticos no canal de Alertas e no Chat Geral"""
        embed = discord.Embed(title=title, description=message, color=color)
        embed.set_author(name="DOOM PROJECT - SISTEMA", icon_url=self.cog.bot.user.display_avatar.url)
        
        # Canais centralizados no .env
        channels = [os.getenv("PZ_ALERT_CHANNEL_ID"), os.getenv("PZ_CHAT_GERAL_ID")]
        
        for c_id in channels:
            try:
                if c_id:
                    channel = self.cog.bot.get_channel(int(c_id))
                    if channel: await channel.send(embed=embed)
            except: continue

    # --- LINHA 0: ENERGIA (PRINCIPAL) ---
    @discord.ui.button(label="Ligar", emoji="🚀", style=discord.ButtonStyle.success, custom_id="pz:p_start", row=0)
    async def btn_start(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        if await ptero_control("start"):
            await self.notify_all("🚀 SERVIDOR ONLINE", "O servidor **DOOM PROJECT** já está pronto para receber os sobreviventes! Bom jogo.", discord.Color.green())
            await interaction.followup.send("🟢 Sinal enviado à RazeHost!", ephemeral=True)

    @discord.ui.button(label="Reiniciar", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="pz:p_restart", row=0)
    async def btn_restart(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        if await ptero_control("restart"):
            await self.notify_all("🔄 REINICIALIZAÇÃO", "O servidor está reiniciando para aplicar atualizações rápidas. Voltamos em instantes!", discord.Color.blue())
            await interaction.followup.send("🔵 Sinal enviado!", ephemeral=True)

    @discord.ui.button(label="Desligar", emoji="🛑", style=discord.ButtonStyle.danger, custom_id="pz:p_stop", row=0)
    async def btn_stop(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        if await ptero_control("stop"):
            await self.notify_all("🛑 SERVIDOR OFFLINE", "O servidor foi desligado por um administrador para manutenção técnica.", discord.Color.red())
            await interaction.followup.send("🔴 Sinal enviado!", ephemeral=True)

    # --- LINHA 1: COMANDOS SECUNDÁRIOS ---
    @discord.ui.button(label="Salvar", emoji="💾", style=discord.ButtonStyle.secondary, custom_id="pz:p_save", row=1)
    async def btn_save(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        try:
            await rcon_execute("save", host=os.getenv("PZ_RCON_HOST"), port=int(os.getenv("PZ_RCON_PORT")), passwd=os.getenv("PZ_RCON_PASSWORD"))
            await interaction.followup.send("💾 Progresso do mapa salvo via RCON!", ephemeral=True)
        except: await interaction.followup.send("❌ Server Offline.", ephemeral=True)

    @discord.ui.button(label="Aviso Global", emoji="📣", style=discord.ButtonStyle.secondary, custom_id="pz:p_msg", row=1)
    async def btn_msg(self, interaction, button):
        await interaction.response.send_modal(GlobalMsgModal(self.cog))

    @discord.ui.button(label="Banir", emoji="🔨", style=discord.ButtonStyle.danger, custom_id="pz:p_ban_sid", row=1)
    async def btn_ban(self, interaction, button):
        await interaction.response.send_modal(BanSteamModal(self.cog))

# --- COG PRINCIPAL ---
class AdminPZ(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sync_panel.start()

    @tasks.loop(minutes=5)
    async def sync_panel(self):
        try:
            forum_id = int(os.getenv("PZ_PANEL_FORUM_CHANNEL_ID", 0))
            forum = self.bot.get_channel(forum_id) or await self.bot.fetch_channel(forum_id)
            if not forum: return

            t_id = await get_config("pz_adm_thread_id")
            m_id = await get_config("pz_adm_msg_id")

            # EMBED DESIGN PREMIUM COM LATERAL VERMELHA
            embed = discord.Embed(
                title="🛰️ CENTRAL DE COMANDO E OPERAÇÕES",
                description=(
                    "**DOOM PROJECT - PAINEL ADMINISTRATIVO**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Bem-vindo à interface de gestão de alto nível. Use os controles abaixo para gerenciar a instância do servidor e segurança in-game.\n"
                ),
                color=0xED4245 # VERMELHO DO DISCORD (BAN/ALERTA)
            )
            
            embed.add_field(
                name="🔋 GESTÃO DE ENERGIA", 
                value="```yaml\n🚀 Ligar     • Inicializa o Java do Servidor\n🔄 Restart   • Reboot controlado da instância\n🛑 Desligar  • Shutdown de emergência```", 
                inline=False
            )
            
            embed.add_field(
                name="🎮 COMANDOS RCON", 
                value="> `Salvar` • Backup do Mundo\n> `Aviso` • Broadcast na tela", 
                inline=True
            )
            
            embed.add_field(
                name="🛡️ SEGURANÇA", 
                value="> `Banir` • Bloqueio via SteamID64", 
                inline=True
            )
            
            embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False) # Divisor invisível

            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
            embed.set_footer(text="Doom Project Hub • Sincronizado via RazeHost v4.5", icon_url="https://i.imgur.com/vHqY7bE.png")

            try:
                if t_id and m_id:
                    thread = self.bot.get_channel(int(t_id)) or await self.bot.fetch_channel(int(t_id))
                    msg = await thread.fetch_message(int(m_id))
                    await msg.edit(embed=embed, view=AdminButtons(self))
                    return
            except: pass

            new_thread = await forum.create_thread(name="🕹️ CONTROLO DO SERVIDOR", embed=embed, view=AdminButtons(self))
            await set_config("pz_adm_thread_id", str(new_thread.thread.id))
            await set_config("pz_adm_msg_id", str(new_thread.message.id))
        except: pass

    @sync_panel.before_loop
    async def before_sync(self): await self.bot.wait_until_ready()

async def setup(bot):
    cog = AdminPZ(bot)
    bot.add_view(AdminButtons(cog))
    await bot.add_cog(cog)