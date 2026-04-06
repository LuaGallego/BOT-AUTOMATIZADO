import asyncio
import random
import uuid

import discord
from discord.ext import commands
from discord import app_commands

import db


THREAD_ID = 1485391543476158575  # postagem do fórum "Lootbox / VIP"

ADMIN_PANEL_CUSTOM_ID = "lootbox_admin_generate"
DM_OPEN_CUSTOM_ID = "lootbox_dm_open"
PANEL_MARKER = "doom_lootbox_admin_panel_v1"


def log(msg: str):
    print(f"[Lootbox] {msg}")


class GerarLootboxModal(discord.ui.Modal, title="Gerar Lootbox"):
    jogador_id = discord.ui.TextInput(
        label="ID do Discord do jogador",
        placeholder="Ex: 123456789012345678",
        required=True,
        max_length=32,
    )
    quantidade = discord.ui.TextInput(
        label="Quantidade de Lootboxes",
        placeholder="Ex: 1",
        default="1",
        required=True,
        max_length=5,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target_id = int(str(self.jogador_id.value).strip())
            qtd = int(str(self.quantidade.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ O ID do jogador e a quantidade precisam ser números.",
                ephemeral=True,
            )
            return

        if qtd <= 0:
            await interaction.response.send_message(
                "❌ A quantidade precisa ser maior que 0.",
                ephemeral=True,
            )
            return

        await db.add_user_lootbox(target_id, qtd)
        saldo = await db.get_user_lootbox_count(target_id)

        dm_status = ""
        try:
            user = interaction.client.get_user(target_id) or await interaction.client.fetch_user(target_id)

            embed = discord.Embed(
                title="🎁 Você recebeu Lootbox",
                description=(
                    f"Foram adicionadas **{qtd} lootbox(es)** para a sua conta.\n\n"
                    f"Clique no botão abaixo para abrir.\n"
                    f"Saldo atual: **{saldo}**"
                ),
                color=discord.Color.gold(),
            )
            embed.add_field(
                name="Como funciona",
                value="Cada clique abre 1 lootbox e envia a recompensa para a fila de entrega do servidor.",
                inline=False,
            )
            embed.set_footer(text="Botão persistente de abertura.")

            await user.send(embed=embed, view=LootboxDMView())
            dm_status = " DM enviada com sucesso."
        except discord.Forbidden:
            dm_status = " O jogador está com a DM fechada."
        except Exception as e:
            dm_status = f" Falha ao enviar DM: {e}"

        confirm = discord.Embed(
            title="✅ Lootbox gerada",
            color=discord.Color.green(),
        )
        confirm.add_field(name="Jogador", value=f"<@{target_id}> (`{target_id}`)", inline=False)
        confirm.add_field(name="Quantidade adicionada", value=str(qtd), inline=True)
        confirm.add_field(name="Saldo atual", value=str(saldo), inline=True)
        confirm.set_footer(text=f"Gerado por {interaction.user} • {interaction.user.id}")

        await interaction.response.send_message(
            content=f"✅ Lootbox gerada para <@{target_id}>.{dm_status}",
            embed=confirm,
            ephemeral=True,
        )


class AdminLootboxView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Gerar Lootbox",
        style=discord.ButtonStyle.success,
        custom_id=ADMIN_PANEL_CUSTOM_ID,
        emoji="🎁",
    )
    async def btn_gerar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Você não tem permissão para usar isso.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(GerarLootboxModal())


class LootboxDMView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Abrir Lootbox",
        style=discord.ButtonStyle.primary,
        custom_id=DM_OPEN_CUSTOM_ID,
        emoji="🎲",
    )
    async def btn_abrir(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id

        active_link = await db.get_active_player_link(user_id)
        if not active_link:
            await interaction.response.send_message(
                "❌ Você precisa vincular sua conta do jogo antes de abrir a lootbox.",
                ephemeral=True,
            )
            return

        steam_id = str(
            active_link.get("pz_reported_steam_id")
            or active_link.get("official_steam_id")
            or ""
        ).strip()

        if not steam_id:
            await interaction.response.send_message(
                "❌ Seu vínculo está sem SteamID operacional no momento.",
                ephemeral=True,
            )
            return

        if not await db.consume_user_lootbox(user_id):
            await interaction.response.send_message(
                "❌ Você não tem lootboxes disponíveis para abrir.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        itens_pool = await db.get_expedition_items()
        if not itens_pool:
            await db.add_user_lootbox(user_id, 1)
            await interaction.followup.send(
                "❌ A tabela de loot está vazia. A lootbox foi devolvida.",
                ephemeral=True,
            )
            return

        pesos = [max(1, int(item.get("rarity_weight", 1))) for item in itens_pool]
        item_sorteado = random.choices(itens_pool, weights=pesos, k=1)[0]

        request_id = str(uuid.uuid4())
        await db.create_pending_loot_reward(
            request_id=request_id,
            discord_id=user_id,
            steam_id=steam_id,
            reward_type="lootbox",
            item_id=str(item_sorteado["item_id"]),
            quantity=int(item_sorteado["quantity"]),
            source="lootbox_dm",
        )

        restantes = await db.get_user_lootbox_count(user_id)

        embed = discord.Embed(
            title="🎉 Lootbox aberta",
            description=(
                f"Você recebeu:\n\n"
                f"> {item_sorteado['emoji']} **{item_sorteado['quantity']}x {item_sorteado['name']}**\n\n"
                f"A recompensa foi enviada para a fila de entrega do servidor."
            ),
            color=discord.Color.green(),
        )
        embed.add_field(name="Lootboxes restantes", value=str(restantes), inline=True)
        embed.add_field(name="Request ID", value=f"`{request_id}`", inline=False)
        embed.set_footer(text="Cada clique consome 1 lootbox.")

        await interaction.followup.send(embed=embed, ephemeral=True)


class LootboxCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_lock = asyncio.Lock()

    async def _get_lootbox_thread(self) -> discord.Thread | None:
        ch = self.bot.get_channel(THREAD_ID)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(THREAD_ID)
            except Exception as e:
                log(f"Não consegui buscar a postagem/thread {THREAD_ID}: {e}")
                return None

        log(f"Canal achado: {ch} | tipo={type(ch).__name__}")

        if not isinstance(ch, discord.Thread):
            log(f"O THREAD_ID {THREAD_ID} não é uma postagem/thread.")
            return None

        try:
            await ch.join()
        except Exception as e:
            log(f"join aviso: {e}")

        try:
            if ch.archived:
                await ch.edit(archived=False)
                log("Thread desarquivada.")
        except Exception as e:
            log(f"Não consegui desarquivar a postagem {ch.id}: {e}")

        return ch

    def _admin_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎁 Painel de Lootbox / VIP",
            description=(
                "Use o botão abaixo para gerar lootboxes para um jogador.\n\n"
                "**Fluxo:**\n"
                "1. O staff informa para quem e a quantidade.\n"
                "2. O bot registra no SQL.\n"
                "3. O jogador recebe uma DM com botão persistente.\n"
                "4. Cada clique abre 1 lootbox."
            ),
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="Observação",
            value="Se apagarem esta mensagem, o bot recria.",
            inline=False,
        )
        embed.set_footer(text=PANEL_MARKER)
        return embed

    async def _find_existing_panel_message(self, thread: discord.Thread) -> discord.Message | None:
        try:
            async for msg in thread.history(limit=50):
                if msg.author.id != self.bot.user.id:
                    continue
                if not msg.embeds:
                    continue
                footer = msg.embeds[0].footer
                if footer and footer.text == PANEL_MARKER:
                    return msg
        except Exception as e:
            log(f"Erro ao procurar mensagem do painel: {e}")

        return None

    async def ensure_admin_panel(self):
        async with self._panel_lock:
            thread = await self._get_lootbox_thread()
            if thread is None:
                return

            panel_msg = await self._find_existing_panel_message(thread)

            if panel_msg:
                try:
                    await panel_msg.edit(embed=self._admin_embed(), view=AdminLootboxView())
                    log(f"Painel sincronizado na postagem {thread.id}.")
                    return
                except Exception as e:
                    log(f"Falha ao editar painel existente: {e}")

            try:
                msg = await thread.send(embed=self._admin_embed(), view=AdminLootboxView())
                log(f"Painel criado na postagem {thread.id}. Msg={msg.id}")
            except Exception as e:
                log(f"Falha ao enviar painel na postagem {thread.id}: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        log("on_ready")
        self.bot.add_view(AdminLootboxView())
        self.bot.add_view(LootboxDMView())

        await self.bot.wait_until_ready()
        await asyncio.sleep(2)
        await self.ensure_admin_panel()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.channel_id != THREAD_ID:
            return
        await asyncio.sleep(2)
        await self.ensure_admin_panel()

    @app_commands.command(
        name="painel_lootbox",
        description="[Admin] Recria ou sincroniza o painel de lootbox na postagem do fórum.",
    )
    @app_commands.default_permissions(administrator=True)
    async def painel_lootbox(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Você não tem permissão.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.ensure_admin_panel()
        await interaction.followup.send("✅ Painel sincronizado.", ephemeral=True)

    @app_commands.command(
        name="teste_postagem_forum",
        description="[Admin] Testa envio de mensagem na postagem do lootbox.",
    )
    @app_commands.default_permissions(administrator=True)
    async def teste_postagem_forum(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Você não tem permissão.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        thread = await self._get_lootbox_thread()
        if thread is None:
            await interaction.followup.send(
                "❌ O THREAD_ID não está apontando para uma postagem/thread válida.",
                ephemeral=True,
            )
            return

        try:
            msg = await thread.send("✅ Teste manual do painel de lootbox.")
            await interaction.followup.send(
                f"✅ Consegui enviar na postagem `{thread.id}`. Msg: `{msg.id}`",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"❌ Falhei ao enviar na postagem `{thread.id}`.\nErro: `{e}`",
                ephemeral=True,
            )


async def setup(bot):
    await bot.add_cog(LootboxCog(bot))