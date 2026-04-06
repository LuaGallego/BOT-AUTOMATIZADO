import os
import asyncio
import discord
import aiosqlite
import aiohttp
import traceback
from rcon.source import rcon
from discord.ext import commands, tasks
from discord import app_commands
from typing import Optional, List

from utils.db import (
    DB_PATH,
    get_discord_balance,
    add_discord_balance,
    get_player_identity_by_discord_id,
    get_config,
    set_config
)

# ==============================================================================
# CONFIGURAÇÕES E CONSTANTES
# ==============================================================================
LOJA_PLAYER_CHANNEL_ID = int(os.getenv("LOJA_PLAYER_CHANNEL_ID", "0"))
LOJA_ADMIN_CHANNEL_ID = int(os.getenv("LOJA_ADMIN_CHANNEL_ID", "0"))
LOJA_VEICULOS_CHANNEL_ID = int(os.getenv("LOJA_VEICULOS_CHANNEL_ID", "1483862868850901164"))

LOJA_ADMIN_MSG_KEY = "loja_admin_msg_id"
LOJA_VEICULOS_MSG_KEY = "loja_veiculos_msg_id"

PZ_AGENT_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:9000")
PZ_AGENT_KEY = os.getenv("PZ_AGENT_API_KEY", "")

# Banner Premium do Topo
SHOP_BANNER_URL = ""

BRAND_NAME = "Doom Project"
BRAND_FOOTER = "DOOM BOT • Sistema de Ouro"
EMBED_RED = discord.Color.from_rgb(237, 66, 69)
EMBED_GOLD = discord.Color.from_rgb(255, 184, 0)
EMBED_DARK = discord.Color.from_rgb(43, 45, 49)
EMBED_BLUE = discord.Color.from_rgb(88, 101, 242)

TICKS = chr(96) * 3

# ==============================================================================
# UTILITÁRIOS
# ==============================================================================
def _line_bold() -> str:
    return "━━━━━━━━━━━━━━━━━━━━━━━━━━"

def _mono(text: str) -> str:
    return f"`{text}`"

def _badge(text: str) -> str:
    return f"` {text} `"

def _money_block(value: int) -> str:
    return f"{TICKS}ansi\n\u001b[1;33m{value} Gold\u001b[0m\n{TICKS}"

def _theme_footer(embed: discord.Embed, label: str):
    embed.set_footer(
        text=f"{BRAND_FOOTER} • {label} • {discord.utils.utcnow().strftime('%H:%M')}"
    )

def _base_embed(
    *,
    title: str,
    description: str,
    color: discord.Color,
    guild: discord.Guild | None = None,
) -> discord.Embed:
    e = discord.Embed(
        title=title,
        description=(
            f"**{BRAND_NAME} • SHOP PREMIUM**\n"
            f"**CENTRAL DE SUPRIMENTOS**\n\n"
            f"{description}\n"
            f"{_line_bold()}"
        ),
        color=color,
        timestamp=discord.utils.utcnow(),
    )

    try:
        if getattr(guild, "icon", None):
            e.set_author(
                name="Centro de Operações de Suprimentos",
                icon_url=guild.icon.url,
            )
            e.set_thumbnail(url=guild.icon.url)
        else:
            e.set_author(name="Centro de Operações de Suprimentos")
    except Exception:
        pass

    return e

def _is_staff(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_guild

# ==============================================================================
# BANCO DE DADOS (COM MIGRAÇÃO AUTOMÁTICA)
# ==============================================================================
async def ensure_shop_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS shop_items (
                key_name TEXT PRIMARY KEY,
                nome TEXT NOT NULL,
                preco INTEGER NOT NULL,
                pz_id TEXT NOT NULL,
                tipo TEXT NOT NULL,
                image_url TEXT,
                online_required INTEGER DEFAULT 1,
                safe_pos_required INTEGER DEFAULT 0,
                semana INTEGER DEFAULT 0
            )''')

        cur = await db.execute("PRAGMA table_info(shop_items)")
        rows = await cur.fetchall()
        existing_cols = {r[1] for r in rows}

        if "image_url" not in existing_cols:
            await db.execute("ALTER TABLE shop_items ADD COLUMN image_url TEXT")
        if "online_required" not in existing_cols:
            await db.execute("ALTER TABLE shop_items ADD COLUMN online_required INTEGER DEFAULT 1")
        if "safe_pos_required" not in existing_cols:
            await db.execute("ALTER TABLE shop_items ADD COLUMN safe_pos_required INTEGER DEFAULT 0")
        if "semana" not in existing_cols:
            await db.execute("ALTER TABLE shop_items ADD COLUMN semana INTEGER DEFAULT 0")

        await db.commit()

async def get_dynamic_items():
    dia = discord.utils.utcnow().day
    semana_atual = ((dia - 1) // 7) + 1
    if semana_atual > 4:
        semana_atual = 4

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shop_items WHERE tipo = 'item' AND semana = ? LIMIT 10", (semana_atual,))
        return await cur.fetchall()

async def get_static_vehicles():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shop_items WHERE tipo = 'veiculo' ORDER BY preco ASC")
        return await cur.fetchall()

async def get_all_shop_items():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM shop_items ORDER BY tipo ASC, preco ASC")
        return await cur.fetchall()

# ==============================================================================
# LOGICA DE ENTREGA E HISTÓRICO (VIA VERDE)
# ==============================================================================
class ConfirmPurchaseView(discord.ui.View):
    def __init__(self, item_dict, identity):
        super().__init__(timeout=60)
        self.item = item_dict
        self.identity = identity

    @discord.ui.button(label="Confirmar Compra", style=discord.ButtonStyle.danger, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        user_id = interaction.user.id
        steam_id = self.identity.get("steam_id", "")
        username_jogo = self.identity.get('username', 'N/A')
        preco_item = int(self.item.get("preco", 0))
        item_nome = self.item.get('nome', 'Item')

        print(f"\n[LOJA DEBUG] 1. Resgate iniciado para: {username_jogo} ({user_id})")

        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT online, is_alive, inventory_weight, carry_capacity FROM player_profiles WHERE steam_id = ? LIMIT 1", (steam_id,))
                row = await cur.fetchone()

                if not row:
                    return await interaction.edit_original_response(content="❌ Personagem não encontrado no jogo.")

                profile = {"online": row[0], "is_alive": row[1], "weight": row[2], "cap": row[3]}

                if profile["is_alive"] == 0:
                    return await interaction.edit_original_response(content="💀 Erro: Personagem morto!")
                if self.item.get("online_required", 1) == 1 and profile["online"] == 0:
                    return await interaction.edit_original_response(content="🌐 Erro: Precisas estar Online no servidor!")

                cur_s = await db.execute("SELECT balance FROM discord_economy WHERE discord_id = ?", (user_id,))
                row_s = await cur_s.fetchone()
                saldo = row_s[0] if row_s else 0

                if saldo < preco_item:
                    return await interaction.edit_original_response(content="❌ Gold insuficiente.")

                await db.execute("UPDATE discord_economy SET balance = balance - ? WHERE discord_id = ?", (preco_item, user_id))
                await db.commit()
                print(f"[LOJA DEBUG] 2. Gold descontado de {username_jogo}")

        except Exception as e:
            return await interaction.edit_original_response(content=f"❌ Erro de Banco: {e}")

        # ==========================================================
        # PONTE RCON DIRETA E TRAVA ANTI-ROUBO
        # ==========================================================
        try:
            rcon_host = os.getenv("PZ_RCON_HOST", "sp-18.raze.host")
            rcon_port = int(os.getenv("PZ_RCON_PORT", 27015))
            rcon_pass = os.getenv("PZ_RCON_PASSWORD", "")
            item_pz_id = self.item.get("pz_id", "")

            # AQUI ESTÁ A CORREÇÃO: O Zomboid pede o ID do carro ANTES do nome do jogador!
            if self.item.get("tipo") == "veiculo":
                comando = f'addvehicle "{item_pz_id}" "{username_jogo}"'
            else:
                comando = f'additem "{username_jogo}" "{item_pz_id}"'

            print(f"[LOJA DEBUG] 3. Disparando RCON: {comando}")
            resp_rcon = await rcon(comando, host=rcon_host, port=rcon_port, passwd=rcon_pass, timeout=5)

            # --- 🚨 TRAVA ANTI-ROUBO ATUALIZADA 🚨 ---
            resp_str = str(resp_rcon).strip().lower()
            palavras_erro = ["not found", "no such", "error", "unknown", "failed", "dead", "can't find", "use: /addvehicle", "spawn a vehicle"]

            if any(palavra in resp_str for palavra in palavras_erro):
                raise Exception(f"Servidor recusou a entrega: '{str(resp_rcon).strip()}' (Você está online e na rua?)")
            # ----------------------------------------------

            # REGISTRO DE SUCESSO NO HISTÓRICO
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("""
                    INSERT INTO shop_deliveries (discord_id, steam_id, game_username, item_name, item_price, status)
                    VALUES (?, ?, ?, ?, ?, 'SUCCESS')
                """, (user_id, steam_id, username_jogo, item_nome, preco_item))
                await db.commit()

            await interaction.edit_original_response(content=f"✅ **{item_nome}** entregue a **{username_jogo}** no jogo!")
            print(f"[LOJA DEBUG] 4. Sucesso total: {resp_rcon}")

        except Exception as e:
            print(f"[LOJA DEBUG] ERRO RCON/TRAVA: {e}")
            # DEVOLUÇÃO DO OURO E LOG DE FALHA
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE discord_economy SET balance = balance + ? WHERE discord_id = ?", (preco_item, user_id))
                await db.execute("""
                    INSERT INTO shop_deliveries (discord_id, steam_id, game_username, item_name, item_price, status, error_message)
                    VALUES (?, ?, ?, ?, ?, 'FAILED', ?)
                """, (user_id, steam_id, username_jogo, item_nome, preco_item, str(e)))
                await db.commit()

            await interaction.edit_original_response(content=f"❌ Erro na entrega: `{e}`\n💰 **O seu Ouro foi devolvido!**")

    @discord.ui.button(label="Cancelar Operação", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Operação cancelada.", view=None, embed=None)

# ==============================================================================
# UI: BOTÃO INDIVIDUAL (LOJA DE ITENS DINÂMICA)
# ==============================================================================
class SingleItemBuyView(discord.ui.View):
    def __init__(self, item_dict):
        super().__init__(timeout=None)
        self.item = item_dict

    @discord.ui.button(label="Resgatar Agora", style=discord.ButtonStyle.danger, emoji="🛒")
    async def buy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            identity = await get_player_identity_by_discord_id(interaction.user.id)
            if not identity:
                return await interaction.followup.send("⚠️ **Conta não vinculada!** Por favor, gere um código no Painel de Vínculo e coloque no jogo primeiro.", ephemeral=True)

            on_status = "Sim" if self.item.get("online_required", 1) else "Não"
            safe_status = "Sim" if self.item.get("safe_pos_required", 0) else "Não"
            username_jogo = identity.get("username") or "Nick Desconhecido"
            codigo_item = self.item.get("pz_id", "Desconhecido")

            desc = (
                f"## {self.item.get('nome', 'Item')}\n"
                f"💰 **Preço:** {_money_block(self.item.get('preco', 0))}\n"
                f"👤 **Personagem:** `{username_jogo}`\n"
                f"📦 **Código PZ:** `{codigo_item}`\n"
                f"🌐 **Precisa estar online:** `{on_status}`\n"
                f"🛡️ **Precisa local seguro:** `{safe_status}`\n\n"
                "Confirme abaixo para concluir o resgate."
            )

            preview_emb = _base_embed(
                title="🛒 Confirmar Compra",
                description=desc,
                color=EMBED_RED,
                guild=interaction.guild
            )
            img_val = str(self.item.get("image_url", "")).strip()
            if img_val and (img_val.startswith("http://") or img_val.startswith("https://")):
                preview_emb.set_image(url=img_val)

            await interaction.followup.send(embed=preview_emb, view=ConfirmPurchaseView(self.item, identity), ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ Erro interno ao processar seleção: {e}", ephemeral=True)

# ==============================================================================
# UI: MENU DE SELEÇÃO (LOJA ESTÁTICA DE VEÍCULOS)
# ==============================================================================
class LojaSelect(discord.ui.Select):
    def __init__(self, itens):
        options = []
        for it in itens[:25]:
            it_dict = dict(it)
            options.append(discord.SelectOption(
                label=it_dict.get('nome', 'Desconhecido'),
                description=f"Preço: {it_dict.get('preco', 0)} Gold",
                value=it_dict.get('key_name', ''),
                emoji="🏎️"
            ))
        super().__init__(placeholder="Escolha a sua viatura...", min_values=1, max_values=1, options=options, custom_id="loja:select_vehicle")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM shop_items WHERE key_name = ?", (self.values[0],))
                row = await cur.fetchone()

            if not row:
                return await interaction.followup.send("❌ Veículo não encontrado.", ephemeral=True)

            item = dict(row)
            identity = await get_player_identity_by_discord_id(interaction.user.id)
            if not identity:
                return await interaction.followup.send("⚠️ **Conta não vinculada!**", ephemeral=True)

            username_jogo = identity.get("username") or "Nick Desconhecido"
            desc = (
                f"## {item.get('nome', 'Veículo')}\n"
                f"💰 **Preço:** {_money_block(item.get('preco', 0))}\n"
                f"👤 **Personagem:** `{username_jogo}`\n"
                f"🚗 **Tipo:** `Veículo`\n\n"
                "Confirme abaixo para solicitar a entrega da viatura."
            )

            preview_emb = _base_embed(
                title="🏎️ Confirmar Compra",
                description=desc,
                color=EMBED_GOLD,
                guild=interaction.guild
            )
            if item.get("image_url"):
                preview_emb.set_image(url=item.get("image_url"))

            await interaction.followup.send(embed=preview_emb, view=ConfirmPurchaseView(item, identity), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Erro: {e}", ephemeral=True)

class LojaPlayerView(discord.ui.View):
    def __init__(self, itens):
        super().__init__(timeout=None)
        if itens:
            self.add_item(LojaSelect(itens))

# ==============================================================================
# UI: MODAIS DE ADIÇÃO E PAINEL ADMIN
# ==============================================================================
class AddItemModal(discord.ui.Modal, title="📦 Adicionar Item ao Estoque"):
    nome = discord.ui.TextInput(label="Nome no Painel", placeholder="ex: Katana de Marfim", required=True)
    preco = discord.ui.TextInput(label="Preço em Gold", placeholder="1000", required=True)
    pz_id = discord.ui.TextInput(label="ID do Project Zomboid", placeholder="Base.Axe", required=True)
    semana = discord.ui.TextInput(label="Semana de Rotação (1 a 4)", placeholder="Ex: 1", default="1", max_length=1)
    img = discord.ui.TextInput(label="URL da Imagem (Opcional)", required=False)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_staff(interaction.user):
            return
        try:
            k = self.nome.value.lower().replace(" ", "_")
            p = int(self.preco.value)
            s = int(self.semana.value) if self.semana.value.isdigit() else 1
            tipo = "item"

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO shop_items (key_name, nome, preco, pz_id, tipo, image_url, semana) VALUES (?,?,?,?,?,?,?)",
                    (k, self.nome.value, p, self.pz_id.value, tipo, self.img.value, s)
                )
                await db.commit()

            await interaction.response.send_message(f"✅ O item **{self.nome.value}** foi adicionado à Semana {s}!", ephemeral=True)
            await self.cog.force_update()
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro ao processar: {e}", ephemeral=True)

class AddVehicleModal(discord.ui.Modal, title="🏎️ Adicionar Veículo"):
    nome = discord.ui.TextInput(label="Nome do Veículo", placeholder="ex: Viatura Nyala", required=True)
    preco = discord.ui.TextInput(label="Preço em Gold", placeholder="10000", required=True)
    pz_id = discord.ui.TextInput(label="ID do Project Zomboid", placeholder="Base.CarNormal", required=True)
    img = discord.ui.TextInput(label="URL da Imagem (Opcional)", required=False)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if not _is_staff(interaction.user):
            return
        try:
            k = self.nome.value.lower().replace(" ", "_")
            p = int(self.preco.value)
            tipo = "veiculo"

            async with aiosqlite.connect(DB_PATH) as db:
                # Veículos são sempre semana 0
                await db.execute(
                    "INSERT OR REPLACE INTO shop_items (key_name, nome, preco, pz_id, tipo, image_url, semana) VALUES (?,?,?,?,?,?,?)",
                    (k, self.nome.value, p, self.pz_id.value, tipo, self.img.value, 0)
                )
                await db.commit()

            await interaction.response.send_message(f"✅ O veículo **{self.nome.value}** foi adicionado à Concessionária!", ephemeral=True)
            await self.cog.force_update()
        except Exception as e:
            await interaction.response.send_message(f"❌ Erro ao processar: {e}", ephemeral=True)

class AdminLojaView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Adicionar Item", style=discord.ButtonStyle.danger, emoji="📦", custom_id="admin:loja:add_item")
    async def add_item(self, interaction: discord.Interaction, b: discord.ui.Button):
        await interaction.response.send_modal(AddItemModal(self.cog))

    @discord.ui.button(label="Adicionar Veículo", style=discord.ButtonStyle.primary, emoji="🏎️", custom_id="admin:loja:add_veic")
    async def add_veic(self, interaction: discord.Interaction, b: discord.ui.Button):
        await interaction.response.send_modal(AddVehicleModal(self.cog))

    @discord.ui.button(label="Esvaziar Loja", style=discord.ButtonStyle.secondary, emoji="🗑️", custom_id="admin:loja:clear")
    async def clear(self, interaction: discord.Interaction, b: discord.ui.Button):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM shop_items")
            await db.commit()
        await interaction.response.send_message("✅ O estoque foi totalmente limpo.", ephemeral=True)
        await self.cog.force_update()

# ==============================================================================
# COG PRINCIPAL
# ==============================================================================
class LojaCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.admin_view = AdminLojaView(self)
        self._lock = asyncio.Lock()

        self.semana_vigente = 1

    async def cog_load(self):
        await ensure_shop_db()
        self.bot.add_view(self.admin_view)

        # Guarda a semana atual quando o bot liga
        dia = discord.utils.utcnow().day
        self.semana_vigente = ((dia - 1) // 7) + 1
        if self.semana_vigente > 4:
            self.semana_vigente = 4

        self.auto_rotacao.start()

    async def cog_unload(self):
        self.auto_rotacao.cancel()

    @app_commands.command(name="shop_update", description="[Admin] Força a reconstrução de todas as vitrines da loja.")
    @app_commands.checks.has_permissions(administrator=True)
    async def shop_update(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.force_update()
        await interaction.followup.send("✅ As vitrines da loja foram reconstruídas e atualizadas com sucesso!")

    @app_commands.command(name="addgold", description="[Admin] Adiciona Gold a um jogador.")
    @app_commands.describe(membro="O jogador", quantidade="Quantidade de Gold")
    @app_commands.checks.has_permissions(administrator=True)
    async def addgold(self, interaction: discord.Interaction, membro: discord.Member, quantidade: int):
        await add_discord_balance(membro.id, quantidade)
        novo_saldo = await get_discord_balance(membro.id)
        await interaction.response.send_message(f"✅ Adicionaste `{quantidade} Gold` a {membro.mention}.\n💰 Novo saldo: `{novo_saldo} Gold`.", ephemeral=True)

    @app_commands.command(name="removegold", description="[Admin] Remove Gold de um jogador.")
    @app_commands.describe(membro="O jogador", quantidade="Quantidade de Gold a remover")
    @app_commands.checks.has_permissions(administrator=True)
    async def removegold(self, interaction: discord.Interaction, membro: discord.Member, quantidade: int):
        await add_discord_balance(membro.id, -quantidade)
        novo_saldo = await get_discord_balance(membro.id)
        await interaction.response.send_message(f"✅ Removeste `{quantidade} Gold` de {membro.mention}.\n💰 Novo saldo: `{novo_saldo} Gold`.", ephemeral=True)

    @app_commands.command(name="saldo", description="Verifica o teu saldo de Gold.")
    async def saldo(self, interaction: discord.Interaction):
        saldo = await get_discord_balance(interaction.user.id)
        await interaction.response.send_message(f"💰 O teu saldo atual é de `{saldo} Gold`.", ephemeral=True)

    @commands.Cog.listener()
    async def on_ready(self):
        await asyncio.sleep(2)
        await self.force_update()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        a_id = await get_config(LOJA_ADMIN_MSG_KEY)
        v_id = await get_config(LOJA_VEICULOS_MSG_KEY)

        if (a_id and payload.message_id == int(a_id)) or (v_id and payload.message_id == int(v_id)):
            await asyncio.sleep(1)
            await self.force_update()

    # ==========================================================
    # TAREFA EM LOOP: Verifica se mudou de semana a cada hora
    # ==========================================================
    @tasks.loop(hours=1)
    async def auto_rotacao(self):
        dia = discord.utils.utcnow().day
        semana_agora = ((dia - 1) // 7) + 1
        if semana_agora > 4:
            semana_agora = 4

        if self.semana_vigente != semana_agora:
            print(f"[LOJA] Rotação ativada! Mudando da semana {self.semana_vigente} para {semana_agora}.")
            self.semana_vigente = semana_agora
            await self.force_update()

    @auto_rotacao.before_loop
    async def before_auto_rotacao(self):
        await self.bot.wait_until_ready()

    async def force_update(self):
        async with self._lock:
            try:
                itens_semana = await get_dynamic_items()
                veiculos_estaticos = await get_static_vehicles()
                todos_itens = await get_all_shop_items()

                # ==========================================
                # 1. GERA A LOJA DE ITENS (CANAL PRINCIPAL)
                # ==========================================
                if LOJA_PLAYER_CHANNEL_ID:
                    try:
                        chan_itens = self.bot.get_channel(LOJA_PLAYER_CHANNEL_ID) or await self.bot.fetch_channel(LOJA_PLAYER_CHANNEL_ID)
                        if chan_itens:
                            try:
                                await chan_itens.purge(limit=50, check=lambda m: m.author == self.bot.user)
                            except:
                                pass

                            if not itens_semana:
                                emb_vazio = _base_embed(
                                    title="📦 Mercado de Itens",
                                    description="_Nenhum item em rotação esta semana..._",
                                    color=EMBED_RED,
                                    guild=chan_itens.guild if hasattr(chan_itens, 'guild') else None
                                )
                                await chan_itens.send(embed=emb_vazio)
                            else:
                                for row in itens_semana:
                                    it_dict = dict(row)
                                    emoji = "🪓" if "axe" in str(it_dict.get('pz_id', '')).lower() else "🔫"

                                    desc = (
                                        f"💰 **Valor:** {_money_block(it_dict.get('preco', 0))}\n"
                                        f"📦 **Entrega:** `Automática`\n"
                                        f"🎯 **Categoria:** `Item da rotação`\n\n"
                                        "Use o botão abaixo para abrir a confirmação de compra."
                                    )
                                    emb_item = _base_embed(
                                        title=f"{emoji} {it_dict.get('nome')}",
                                        description=desc,
                                        color=EMBED_RED,
                                        guild=chan_itens.guild if hasattr(chan_itens, 'guild') else None
                                    )

                                    if it_dict.get('image_url'):
                                        emb_item.set_image(url=it_dict['image_url'])

                                    _theme_footer(emb_item, f"Oferta da Semana {(discord.utils.utcnow().day - 1) // 7 + 1}")

                                    view = SingleItemBuyView(it_dict)
                                    await chan_itens.send(embed=emb_item, view=view)
                    except Exception as ev:
                        print(f"[LOJA] Erro ao carregar canal de itens: {ev}")

                # ==========================================
                # 2. GERA A LOJA DE VEÍCULOS (NA THREAD SEPARADA)
                # ==========================================
                if LOJA_VEICULOS_CHANNEL_ID:
                    try:
                        chan_veic = self.bot.get_channel(LOJA_VEICULOS_CHANNEL_ID) or await self.bot.fetch_channel(LOJA_VEICULOS_CHANNEL_ID)
                        if chan_veic:
                            if not veiculos_estaticos:
                                emb_veic = _base_embed(
                                    title="🏎️ Concessionária Doom",
                                    description="_Nenhum veículo em estoque no momento..._",
                                    color=EMBED_RED,
                                    guild=chan_veic.guild if hasattr(chan_veic, 'guild') else None
                                )
                                view_veic = None
                            else:
                                emb_veic = _base_embed(
                                    title="🏎️ Concessionária Doom",
                                    description=(
                                        "Escolha sua viatura no menu abaixo.\n"
                                        "Após selecionar, você receberá a tela de confirmação no privado.\n\n"
                                        "🚘 **Catálogo:** `Permanente`\n"
                                        "📩 **Recibo:** `Enviado por DM`\n"
                                        "🛠️ **Entrega:** `Via servidor`"
                                    ),
                                    color=EMBED_RED,
                                    guild=chan_veic.guild if hasattr(chan_veic, 'guild') else None
                                )
                                if SHOP_BANNER_URL and SHOP_BANNER_URL.startswith("http"):
                                    emb_veic.set_image(url=SHOP_BANNER_URL)
                                _theme_footer(emb_veic, "Catálogo Permanente")
                                view_veic = LojaPlayerView(veiculos_estaticos)

                            saved_v = await get_config(LOJA_VEICULOS_MSG_KEY)
                            msg_v = None
                            if saved_v:
                                try:
                                    msg_v = await chan_veic.fetch_message(int(saved_v))
                                except:
                                    msg_v = None

                            if msg_v:
                                if view_veic:
                                    await msg_v.edit(embed=emb_veic, view=view_veic)
                                else:
                                    await msg_v.edit(embed=emb_veic, view=None)
                            else:
                                if view_veic:
                                    msg_v = await chan_veic.send(embed=emb_veic, view=view_veic)
                                else:
                                    msg_v = await chan_veic.send(embed=emb_veic)
                                await set_config(LOJA_VEICULOS_MSG_KEY, str(msg_v.id))
                    except Exception as ev:
                        print(f"[LOJA] Erro ao carregar thread de veiculos: {ev}")

                # ==========================================
                # 3. PAINEL DO ADMIN
                # ==========================================
                if LOJA_ADMIN_CHANNEL_ID:
                    try:
                        chan_adm = self.bot.get_channel(LOJA_ADMIN_CHANNEL_ID) or await self.bot.fetch_channel(LOJA_ADMIN_CHANNEL_ID)
                        if chan_adm:
                            lista_adm = ""
                            if not todos_itens:
                                lista_adm = "❌ Nenhum item/veículo no sistema."
                            else:
                                for it in todos_itens:
                                    it_dict = dict(it)
                                    icon = "🏎️" if it_dict['tipo'] == 'veiculo' else "📦"
                                    sem = f" (Semana {it_dict['semana']})" if it_dict['tipo'] == 'item' else " (Fixo)"
                                    lista_adm += f"{icon} **{it_dict['nome']}** (`{it_dict['preco']} Gold`){sem}\n"

                            emb_adm = _base_embed(
                                title="⚙️ Central de Comando - Loja",
                                description=(
                                    "Gerencie os itens e veículos da loja em tempo real.\n\n"
                                    f"📦 **Estoque total:** `{len(todos_itens)}`\n\n"
                                    f"{lista_adm or '❌ Nenhum item cadastrado.'}"
                                ),
                                color=EMBED_DARK,
                                guild=chan_adm.guild if hasattr(chan_adm, 'guild') else None
                            )
                            _theme_footer(emb_adm, "Modo Administrativo")

                            saved_a = await get_config(LOJA_ADMIN_MSG_KEY)
                            msg_a = None
                            if saved_a:
                                try:
                                    msg_a = await chan_adm.fetch_message(int(saved_a))
                                except:
                                    msg_a = None

                            if msg_a:
                                await msg_a.edit(embed=emb_adm, view=self.admin_view)
                            else:
                                msg_a = await chan_adm.send(embed=emb_adm, view=self.admin_view)
                                await set_config(LOJA_ADMIN_MSG_KEY, str(msg_a.id))
                    except Exception as ev:
                        print(f"[LOJA] Erro ao carregar canal de admin: {ev}")
            except Exception as e:
                print(f"[LOJA] Erro fatal durante a atualização do painel: {e}")
                traceback.print_exc()

async def setup(bot: commands.Bot):
    await bot.add_cog(LojaCog(bot))