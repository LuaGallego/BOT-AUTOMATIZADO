from __future__ import annotations

import os
import time
import json
import traceback
import asyncio

import discord
import aiosqlite
import aiohttp

from discord.ext import commands, tasks
from utils.db import DB_PATH, get_config, set_config


class ServerStatusCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self._current_message_id: int | None = None

    async def cog_load(self):
        msg_id_val = await get_config("status_msg_id")
        try:
            self._current_message_id = int(msg_id_val) if msg_id_val and str(msg_id_val).isdigit() else None
        except Exception:
            self._current_message_id = None

        if not self.main_loop.is_running():
            self.main_loop.start()

    async def cog_unload(self):
        if self.main_loop.is_running():
            self.main_loop.cancel()

    async def check_api_health(self) -> bool:
        """Verifica se a API do servidor está respondendo."""
        try:
            base_url = os.getenv("PZ_AGENT_BASE_URL", "").strip().rstrip("/")
            api_key = os.getenv("PZ_AGENT_API_KEY", "").strip()

            if not base_url or not api_key:
                print("[status] Erro: PZ_AGENT_BASE_URL ou API_KEY não configurados.")
                return False

            url = f"{base_url}/health"
            headers = {"x-api-key": api_key}

            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    return 200 <= resp.status < 300
        except Exception as exc:
            print(f"[status] Falha ao consultar /health: {exc}")
            return False

    async def _find_existing_status_message(self, channel: discord.TextChannel | discord.Thread) -> discord.Message | None:
        """
        Procura a mensagem de status existente:
        1) tenta pelo ID salvo
        2) fallback: procura nas últimas mensagens uma embed do próprio bot
        """
        # 1) tenta pelo ID em memória / banco
        msg_id_candidates = []

        if self._current_message_id:
            msg_id_candidates.append(self._current_message_id)

        msg_id_val = await get_config("status_msg_id")
        if msg_id_val and str(msg_id_val).isdigit():
            mid = int(msg_id_val)
            if mid not in msg_id_candidates:
                msg_id_candidates.append(mid)

        for mid in msg_id_candidates:
            try:
                msg = await channel.fetch_message(mid)
                if msg.author.id == self.bot.user.id:
                    self._current_message_id = msg.id
                    return msg
            except discord.NotFound:
                continue
            except Exception as exc:
                print(f"[status] Falha transitória ao buscar mensagem {mid}: {exc}")
                # não cria nova ainda; tenta fallback por histórico
                break

        # 2) fallback por histórico
        try:
            found = []
            async for msg in channel.history(limit=30):
                if msg.author.id != self.bot.user.id:
                    continue
                if not msg.embeds:
                    continue

                emb = msg.embeds[0]
                title = str(getattr(emb, "title", "") or "")
                if "SISTEMA DE STATUS" in title:
                    found.append(msg)

            if not found:
                return None

            # mantém a mais recente, remove duplicatas antigas
            found.sort(key=lambda m: m.created_at, reverse=True)
            keep = found[0]

            for old in found[1:]:
                try:
                    await old.delete()
                    print(f"[status] Duplicata antiga removida: {old.id}")
                except Exception:
                    pass

            self._current_message_id = keep.id
            await set_config("status_msg_id", str(keep.id))
            return keep

        except Exception as exc:
            print(f"[status] Falha ao procurar mensagem por histórico: {exc}")
            return None

    @tasks.loop(seconds=20)
    async def main_loop(self):
        async with self.lock:
            try:
                channel_id_env = os.getenv("STATUS_CHANNEL_ID", "0")
                if not channel_id_env or channel_id_env == "0":
                    return

                channel_id = int(channel_id_env)
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)

                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    print(f"[status] Canal {channel_id} não é TextChannel/Thread.")
                    return

                # 2. Coleta de Dados do Servidor (API e Telemetria)
                api_online = await self.check_api_health()

                last_pulse_str = await get_config("last_pulse_unix")
                last_pulse = int(last_pulse_str) if (last_pulse_str and str(last_pulse_str).isdigit()) else 0

                agora = int(time.time())
                telemetry_fresh = (agora - last_pulse) < 90 and last_pulse != 0

                # 3. Busca dados no SQLite (server_state)
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute(
                        """
                        SELECT online_count, game_time, global_temperature, weather_json
                        FROM server_state
                        ORDER BY updated_at DESC
                        LIMIT 1
                        """
                    )
                    row = await cursor.fetchone()

                if row:
                    pop = row[0] or 0
                    g_time = row[1] or "00:00"
                    temp = row[2] or 0
                    weather_raw = row[3] or "{}"
                else:
                    pop, g_time, temp, weather_raw = 0, "--:--", 0, "{}"

                # 4. Lógica de Status e Cor
                if api_online and telemetry_fresh:
                    status_label = "🟢 ONLINE"
                    embed_color = discord.Color.green()
                elif api_online and not telemetry_fresh:
                    status_label = "🟡 ONLINE (SEM TELEMETRIA)"
                    embed_color = discord.Color.gold()
                else:
                    status_label = "⚫ OFFLINE / MANUTENÇÃO"
                    embed_color = discord.Color.from_rgb(43, 45, 49)

                # 5. Construção do Embed
                emb = discord.Embed(
                    title="🛰️ SISTEMA DE STATUS",
                    description="Atualização em tempo real do servidor.",
                    color=embed_color,
                    timestamp=discord.utils.utcnow()
                )

                v_pop = f"{pop} Jogadores" if api_online else "0 Jogadores"

                emb.add_field(name="🔌 Status", value=f"```\n{status_label}\n```", inline=True)
                emb.add_field(name="👥 População", value=f"```\n{v_pop}\n```", inline=True)

                if api_online and telemetry_fresh:
                    emb.add_field(name="🕒 Horário Local", value=f"`{g_time}`", inline=True)
                    emb.add_field(name="🌡️ Temperatura", value=f"`{float(temp):.1f}°C`", inline=True)

                    clima_text = "☀️ Céu Limpo"
                    try:
                        w_data = json.loads(weather_raw)
                        if w_data.get("is_raining"):
                            clima_text = "🌧️ Chuva"
                        elif float(w_data.get("cloud_intensity", 0)) > 0.6:
                            clima_text = "☁️ Nublado"
                    except Exception:
                        pass

                    emb.add_field(name="🌤️ Clima", value=f"`{clima_text}`", inline=True)

                elif api_online and not telemetry_fresh:
                    emb.add_field(
                        name="📡 Nota",
                        value="O servidor está aberto, mas o mod de telemetria não enviou dados recentemente.",
                        inline=False
                    )

                emb.set_footer(text="Sincronizado via Projeto Zomboid")

                # 6. PERSISTÊNCIA ROBUSTA: editar sempre a mesma mensagem
                target_msg = await self._find_existing_status_message(channel)

                if target_msg:
                    try:
                        await target_msg.edit(embed=emb)
                        self._current_message_id = target_msg.id
                        await set_config("status_msg_id", str(target_msg.id))
                    except discord.NotFound:
                        print("[status] Mensagem sumiu no momento do edit; criando nova.")
                        new_msg = await channel.send(embed=emb)
                        self._current_message_id = new_msg.id
                        await set_config("status_msg_id", str(new_msg.id))
                        print(f"[status] Nova mensagem de status criada com ID: {new_msg.id}")
                    except Exception as e:
                        print(f"[status] Erro ao editar mensagem existente: {e}")
                else:
                    new_msg = await channel.send(embed=emb)
                    self._current_message_id = new_msg.id
                    await set_config("status_msg_id", str(new_msg.id))
                    print(f"[status] Nova mensagem de status criada com ID: {new_msg.id}")

            except Exception:
                print("[status] Erro crítico no main_loop:")
                traceback.print_exc()

    @main_loop.before_loop
    async def before_main_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        try:
            saved_id = await get_config("status_msg_id")
            if saved_id and str(saved_id).isdigit() and payload.message_id == int(saved_id):
                await set_config("status_msg_id", "")
                self._current_message_id = None
                await asyncio.sleep(1)
                # recria imediatamente, mas o loop também continua saudável
                channel_id_env = os.getenv("STATUS_CHANNEL_ID", "0")
                if channel_id_env and channel_id_env != "0":
                    channel_id = int(channel_id_env)
                    channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                    if isinstance(channel, (discord.TextChannel, discord.Thread)):
                        await self.main_loop()
        except Exception:
            pass

    @commands.command(name="resetstatus")
    @commands.has_permissions(administrator=True)
    async def reset_status(self, ctx: commands.Context):
        """Limpa o ID da mensagem de status no banco para forçar uma nova postagem."""
        try:
            msg_id = await get_config("status_msg_id")
            await set_config("status_msg_id", "")
            self._current_message_id = None

            if msg_id and str(msg_id).isdigit():
                try:
                    old_msg = await ctx.channel.fetch_message(int(msg_id))
                    await old_msg.delete()
                except Exception:
                    pass

            await ctx.send("✅ **Status resetado!** O bot criará uma nova mensagem no próximo ciclo de 20s.", delete_after=10)

        except Exception as e:
            await ctx.send(f"❌ Erro ao resetar: {e}")
            traceback.print_exc()

    @reset_status.error
    async def reset_status_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("🚫 Você não tem permissão (Admin) para usar este comando.")


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerStatusCog(bot))