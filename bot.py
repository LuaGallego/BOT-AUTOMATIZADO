import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv
from utils.db import init_db


load_dotenv()
TOKEN = os.getenv("TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

_synced_once = False  # evita repetir em reconexões

@bot.command()
async def ping(ctx: commands.Context):
    await ctx.send("pong ✅")

@bot.event
async def on_ready():
    global _synced_once
    if _synced_once:
        return
    _synced_once = True

    print(f"Bot online como {bot.user}")

    guild_id = int(os.getenv("GUILD_ID", "0"))  # coloca no .env

    try:
        if guild_id:
            guild_obj = discord.Object(id=guild_id)

            # Remove o comando antigo (context menu) da GUILD antes de sincronizar
            bot.tree.remove_command(
                "Aplicar Advertência",
                type=discord.AppCommandType.user,
                guild=guild_obj,
            )
            bot.tree.remove_command(
                "Aplicar Advertencia",
                type=discord.AppCommandType.user,
                guild=guild_obj,
            )

            synced = await bot.tree.sync(guild=guild_obj)
            print(f"✅ Sync (GUILD) ok: {len(synced)} comandos")
        else:
            synced = await bot.tree.sync()
            print(f"✅ Sync (GLOBAL) ok: {len(synced)} comandos")

    except Exception as e:
        print(f"Erro ao syncar: {type(e).__name__}: {e}")

@bot.event
async def on_message(message: discord.Message):
    # ignora o próprio bot
    if message.author.bot:
        return

    # DEBUG: mostra no terminal que o bot está vendo a mensagem
    print(f"[MSG] #{message.channel} {message.author}: {message.content}")

    # ISSO é obrigatório se existir qualquer on_message em algum lugar
    await bot.process_commands(message)

async def load_cogs():
    for filename in os.listdir("./cogs"):
        if filename.endswith(".py"):
            extension = f"cogs.{filename[:-3]}"
            try:
                await bot.load_extension(extension)
                print(f"[OK] Carregado: {extension}")
            except Exception as e:
                print(f"[ERRO] Falha ao carregar {extension}: {type(e).__name__}: {e}")

@bot.event
async def on_command(ctx: commands.Context):
    print(f"[CMD] {ctx.author} executou: {ctx.message.content} em #{ctx.channel}")

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    print(f"[CMD-ERRO] {ctx.author} tentou: {ctx.message.content}")
    print(f"[CMD-ERRO] {type(error).__name__}: {error}")
    try:
        await ctx.send(f"⚠️ Erro: `{type(error).__name__}`")
    except Exception as e:
        print(f"[CMD-ERRO] Não consegui enviar erro no Discord: {type(e).__name__}: {e}")

async def main():
    async with bot:
        # 1. PRIMEIRO: Inicializa o banco (Cria as tabelas/migrações)
        await init_db()
        print("✅ Banco de dados inicializado e tabelas criadas.")
        
        # 2. DEPOIS: Carrega os cogs (Agora eles vão encontrar as tabelas prontas)
        await load_cogs()
        
        # 3. POR FIM: Liga o bot
        await bot.start(TOKEN)

asyncio.run(main())