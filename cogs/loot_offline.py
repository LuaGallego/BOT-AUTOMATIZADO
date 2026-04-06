import discord
from discord.ext import commands
import time
import uuid
import random
import os
import asyncio
from utils import db # Importamos o teu banco de dados
from utils.pz_rcon import enviar_comando_rcon

class ResgateLootView(discord.ui.View):
    """View Efêmera (Invisível) onde o jogador clica para resgatar o item ganho."""
    def __init__(self, item_sorteado: dict):
        super().__init__(timeout=None)
        self.item_sorteado = item_sorteado

    @discord.ui.button(label="Resgatar para o Inventário", style=discord.ButtonStyle.success, custom_id="btn_resgatar_loot", emoji="🎒")
    async def btn_resgatar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        
        user_id = interaction.user.id
        item_id = self.item_sorteado["item_id"]
        qtd = self.item_sorteado["quantity"]
        nome_item = self.item_sorteado["name"]

        button.disabled = True
        await interaction.edit_original_response(view=self)

        identity = await db.get_player_identity_by_discord_id(user_id) 
        if not identity or not identity.get("steam_id"):
            await interaction.followup.send("❌ Precisas de vincular a tua conta Steam primeiro!", ephemeral=True)
            return
            
        steam_id = identity["steam_id"]
        username = identity.get("username") # Precisamos do username para o comando RCON

        perfil = await db.get_player_profile(steam_id)
        is_online = perfil and perfil.get("online") == 1

        request_id = f"lootoff:{user_id}:{int(time.time())}:{uuid.uuid4().hex[:4]}"

        # --- NOVA LÓGICA DE DUPLA VALIDAÇÃO ---
        if is_online:
            if not username:
                username = steam_id # Fallback caso não tenha username salvo
                
            # Monta o comando do Zomboid (ex: additem "Gallego" "Base.Axe")
            comando = f'additem "{username}" "{item_id}"'
            
            # Executa o comando as várias vezes que a quantidade mandar
            resposta_rcon = ""
            for _ in range(qtd):
                resposta_rcon = await enviar_comando_rcon(comando)
            
            # Se o RCON responder que não achou o jogador ou der erro
            if "not found" in resposta_rcon.lower() or "error" in resposta_rcon.lower() or resposta_rcon == "ERROR":
                print(f"[Loot] Falso positivo de online. Jogador {username} não está no servidor. Enviando para fila.")
                is_online = False # Forçamos para falso para ele ir para a fila de espera!

        if is_online:
            await interaction.followup.send(f"✅ Estás online! **{qtd}x {nome_item}** foi entregue no teu inventário in-game.", ephemeral=True)
        else:
            await db.create_pending_loot_reward(
                request_id=request_id,
                discord_id=user_id,
                steam_id=steam_id,
                reward_type="item",
                item_id=item_id,
                quantity=qtd,
                source="expedicao_diaria"
            )
            await interaction.followup.send(f"⏳ Pareces estar offline no jogo! O teu loot (**{qtd}x {nome_item}**) foi reservado e será entregue quando entrares no servidor.", ephemeral=True)


class PainelExploracaoView(discord.ui.View):
    """View Fixa (Permanente) que fica no canal do Discord."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Sair para Explorar", style=discord.ButtonStyle.primary, custom_id="btn_explorar_loot", emoji="🏕️")
    async def btn_explorar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id
        
        # 1. Verificar limite diário de 2 usos
        usos_hoje = await db.get_today_loot_claim_count(user_id)
        usos_hoje = 2 # Valor simulado para testar
        
        if usos_hoje >= 10:
            await interaction.followup.send("❌ Já exploraste o máximo possível hoje (2/2). Descansa e volta amanhã!", ephemeral=True)
            return
            
        # 2. Consumir um uso na base de dados
        await db.consume_daily_loot_claim(user_id)
        
        # 3. Buscar os itens da base de dados
        itens_db = await db.get_expedition_items()
        itens_db = [{"item_id": "Base.Axe", "name": "Machado de Bombeiro", "emoji": "🪓", "quantity": 1, "rarity_weight": 10}] # Simulado
        
        if not itens_db:
            await interaction.followup.send("❌ A tabela de itens está vazia. Avisa a staff!", ephemeral=True)
            return

        # 4. Magia dos Pesos: Sortear o item baseado na raridade
        pesos = [item["rarity_weight"] for item in itens_db]
        item_sorteado = random.choices(itens_db, weights=pesos, k=1)[0]
        
        # 5. História aleatória
        historias = [
            "Tu entraste numa casa abandonada e, depois de vasculhar os armários da cozinha, encontraste algo útil!",
            "Enquanto fugias de uma horda, tropeçaste numa mochila largada no meio da rua. Dentro dela havia:",
            "Após horas a caminhar pela floresta, achaste um acampamento militar abandonado com alguns suprimentos."
        ]
        historia = random.choice(historias)
        
        descricao = f"{historia}\n\n**{item_sorteado['emoji']} {item_sorteado['quantity']}x {item_sorteado['name']}**\n\nClica no botão abaixo para guardares este item no teu inventário."
        
        embed = discord.Embed(
            title="🗺️ Expedição Concluída!",
            description=descricao,
            color=discord.Color.dark_gold()
        )
        
        await interaction.followup.send(embed=embed, view=ResgateLootView(item_sorteado), ephemeral=True)


class LootOfflineCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.canal_id = int(os.getenv("CANAL_EXPEDICAO_ID", 1484973503215177738))

    async def cog_load(self):
        # Regista o botão principal para persistência
        self.bot.add_view(PainelExploracaoView())
        # Inicia a tarefa de limpeza e geração
        self.bot.loop.create_task(self.verificar_gerar_painel())

    async def verificar_gerar_painel(self):
        await self.bot.wait_until_ready()
        canal = self.bot.get_channel(self.canal_id)
        if not canal:
            print(f"[Loot Offline] AVISO: Não encontrei a sala {self.canal_id}")
            return

        # Limpa o canal para deixar só o painel novo
        try:
            await canal.purge(limit=100)
        except Exception as e:
            print(f"[Loot Offline] Aviso ao limpar sala: {e}")

        # Gera o painel limpo
        embed = discord.Embed(
            title="🌲 ZONA DE EXPLORAÇÃO (OFFLINE)",
            description=(
                "Tens coragem de sair para procurar mantimentos?\n\n"
                "• Podes explorar **2 vezes por dia**.\n"
                "• O loot varia entre Comum, Raro e Épico.\n"
                "• Se estiveres offline no jogo, o item fica reservado e é entregue quando entrares!"
            ),
            color=discord.Color.dark_theme()
        )
        await canal.send(embed=embed, view=PainelExploracaoView())
        print("[Loot Offline] Painel gerado com sucesso!")

async def setup(bot):
    await bot.add_cog(LootOfflineCog(bot))