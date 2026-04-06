import os
import asyncio
import discord
import aiosqlite
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone, timedelta

from utils.db import (
    get_staff_dashboard,
    set_staff_dashboard,
    update_staff_dashboard_message_id,
    count_whitelist_pending,
    count_whitelist_approved,
    count_whitelist_rejected,
    log_ticket_event,
    count_ticket_events,

)


def _is_staff(member: discord.Member) -> bool:
    p = member.guild_permissions
    return (
        p.administrator
        or p.moderate_members
        or p.manage_guild
        or p.manage_roles
        or p.kick_members
        or p.ban_members
    )


AJUDA_PREFIXES = ("ajuda", "help")
DENUNCIA_PREFIXES = ("denúncia", "denuncia", "report")
TZ = timezone(timedelta(hours=-3))

def now_br() -> datetime:
    return datetime.now(TZ)

def start_of_today_br() -> datetime:
    n = now_br()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)

def fmt_rel(dt_utc: datetime | None) -> str:
    if not dt_utc:
        return "—"
    return f"<t:{int(dt_utc.timestamp())}:R>"

def infer_ticket_type(name: str) -> str:
    n = (name or "").strip().lower()
    if n.startswith(AJUDA_PREFIXES) or "ajuda" in n:
        return "ajuda"
    if n.startswith(DENUNCIA_PREFIXES) or "denun" in n:
        return "denuncia"
    return "outros"

def is_open_thread(t: discord.Thread) -> bool:
    return (t is not None) and (not t.archived) and (not t.locked)

def bar(value: int, max_value: int, width: int = 12) -> str:
    max_value = max(1, max_value)
    v = max(0, min(value, max_value))
    filled = int(round((v / max_value) * width))
    return "▰" * filled + "▱" * (width - filled)

def status_emoji(level: str) -> str:
    return {"ok": "🟢", "warn": "🟠", "bad": "🔴"}.get(level, "🟢")


class StaffDashboard(commands.Cog):
    """
    Dashboard em POST de Fórum (thread) — persistente.

    Como funciona (bem importante):
    - STAFF_DASHBOARD_CHANNEL_ID pode ser:
      - ID de uma THREAD (postagem do fórum) -> fica 100% fixo (recomendado)
      - ID de um FÓRUM -> o bot cria 1 postagem e salva o ID dela no banco; depois SEMPRE reutiliza
      - ID de um TextChannel -> posta normal (fallback)

    Persistência:
    - Se apagarem a mensagem do dashboard: o bot recria a mensagem na mesma thread.
    - Se apagarem a thread do dashboard: o bot recria uma nova postagem e atualiza o ID salvo.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._update_lock = asyncio.Lock()
        self._debounce_task: asyncio.Task | None = None
        self._heartbeat_started = False

        self.guild_id = int(os.getenv("GUILD_ID", "0"))
        self.dashboard_target_id = int(os.getenv("STAFF_DASHBOARD_CHANNEL_ID", "0"))
        self.dashboard_post_name = (os.getenv("STAFF_DASHBOARD_POST_NAME") or "📊 Dashboard Staff").strip()

        self.tickets_parent_id = int(os.getenv("TICKETS_FORUM_ID", "0")) or int(os.getenv("TICKET_THREADS_CHANNEL_ID", "0"))
        self.whitelist_parent_id = int(os.getenv("WHITELIST_FORUM_ID", "0"))

        self.support_vc_category_id = int(os.getenv("SUPPORT_VC_CATEGORY_ID", "0")) or int(os.getenv("SUPPORT_CATEGORY_ID", "0"))
        self.support_trigger_vc_id = int(os.getenv("SUPPORT_TRIGGER_VOICE_CHANNEL_ID", "0")) or int(os.getenv("TRIGGER_VOICE_CHANNEL_ID", "0"))

        support_vc_ids_raw = (os.getenv("SUPPORT_VC_IDS") or "").strip()
        self.support_vc_ids: set[int] = set()
        if support_vc_ids_raw:
            for part in support_vc_ids_raw.split(","):
                part = part.strip()
                if part.isdigit():
                    self.support_vc_ids.add(int(part))

        # Thread onde staff aprova whitelists (se existir)
        self.whitelist_approval_thread_id = int(os.getenv("WHITELIST_APPROVAL_THREAD_ID", "0"))

        # Canal/Forum principal de tickets (pra link no painel)
        self.ticket_threads_channel_id = int(os.getenv("TICKET_THREADS_CHANNEL_ID", "0"))

    def schedule_update(self, delay: float = 1.5) -> None:
        if self._debounce_task and not self._debounce_task.done():
            return

        async def _run():
            await asyncio.sleep(delay)
            await self.update_dashboard()

        self._debounce_task = asyncio.create_task(_run())

    async def cog_load(self):
        self.schedule_update(delay=2.0)
        if not self._heartbeat_started:
            self._heartbeat.start()
            self._heartbeat_started = True

    def cog_unload(self):
        try:
            self._heartbeat.cancel()
        except Exception:
            pass

    async def _get_guild(self) -> discord.Guild | None:
        if not self.guild_id:
            return None
        return self.bot.get_guild(self.guild_id)

    async def _try_resolve_saved_destination(
        self, guild: discord.Guild
    ) -> tuple[discord.abc.Messageable, int, str] | None:
        """
        Se já existe um destino salvo no DB, tenta reutilizar primeiro.
        Isso evita criar novas postagens no fórum por mismatch de nome/emoji/etc.
        """
        saved = await get_staff_dashboard(guild.id)
        if not saved:
            return None

        saved_dest_id, _saved_message_id = saved

        # Pode ser thread ou canal texto
        ch = guild.get_channel(saved_dest_id) or self.bot.get_channel(saved_dest_id)
        if isinstance(ch, discord.Thread):
            try:
                if ch.archived:
                    await ch.edit(archived=False)
            except discord.Forbidden:
                pass
            return ch, ch.id, "thread"

        if isinstance(ch, discord.TextChannel):
            return ch, ch.id, "text"

        return None

    async def _resolve_dashboard_destination(
        self, guild: discord.Guild
    ) -> tuple[discord.abc.Messageable, int, str]:
        """
        Retorna (destino_messageable, dest_id, tipo)
        tipo: "thread" | "text"
        """
        if not self.dashboard_target_id:
            raise RuntimeError("STAFF_DASHBOARD_CHANNEL_ID não definido no .env")

        ch = guild.get_channel(self.dashboard_target_id) or self.bot.get_channel(self.dashboard_target_id)

        # THREAD (postagem) — fixo
        if isinstance(ch, discord.Thread):
            try:
                if ch.archived:
                    await ch.edit(archived=False)
            except discord.Forbidden:
                pass
            return ch, ch.id, "thread"

        # FÓRUM (cria/acha thread)
        if isinstance(ch, discord.ForumChannel):
            forum = ch

            # procura ativa pelo nome
            for th in forum.threads:
                if th.name == self.dashboard_post_name:
                    try:
                        if th.archived:
                            await th.edit(archived=False)
                    except discord.Forbidden:
                        pass
                    return th, th.id, "thread"

            # procura arquivada pelo nome
            try:
                async for th in forum.archived_threads(limit=50):
                    if th.name == self.dashboard_post_name:
                        try:
                            if th.archived:
                                await th.edit(archived=False)
                        except discord.Forbidden:
                            pass
                        return th, th.id, "thread"
            except discord.Forbidden:
                pass

            # cria o post no fórum
            embed = discord.Embed(
                title="✦ Painel da Staff",
                description=(
                    "```ansi\n"
                    "\u001b[1;30m⎯⎯⎯ Inicializando painel premium ⎯⎯⎯\u001b[0m\n"
                    "```"
                    "\n**Aguarde alguns segundos...** preparando métricas, sinais e status da staff."
                ),
                color=discord.Color.from_rgb(10, 10, 10),
            )

            # Logo do servidor
            if guild.icon:
                embed.set_thumbnail(url=guild.icon.url)
                embed.set_author(name=guild.name, icon_url=guild.icon.url)
            else:
                embed.set_author(name=guild.name)

            try:
                created = await forum.create_thread(name=self.dashboard_post_name, embed=embed)
            except discord.Forbidden as e:
                raise RuntimeError(
                    "Sem permissão para criar postagem no FÓRUM do dashboard. "
                    "Dê ao bot: View Channel, Send Messages, Create Posts/Threads, Embed Links."
                ) from e

            # created pode ser Thread ou ThreadWithMessage
            if hasattr(created, "thread"):
                thread = created.thread  # type: ignore
            else:
                thread = created  # type: ignore

            try:
                if thread.archived:
                    await thread.edit(archived=False)
            except discord.Forbidden:
                pass

            return thread, thread.id, "thread"

        # CANAL TEXTO normal
        if isinstance(ch, discord.TextChannel):
            return ch, ch.id, "text"

        raise RuntimeError("STAFF_DASHBOARD_CHANNEL_ID não é Thread, nem Fórum, nem TextChannel.")

    async def _ensure_message(self, guild: discord.Guild) -> tuple[discord.abc.Messageable, int, discord.Message]:
        """
        1) tenta usar o destino salvo (DB) primeiro (persistência real)
        2) se não der, resolve pelo .env
        3) garante 1 única mensagem do dashboard e recria se deletarem
        """
        # 1) tenta reutilizar destino salvo
        resolved = await self._try_resolve_saved_destination(guild)
        if resolved:
            destination, dest_id, _dtype = resolved
        else:
            destination, dest_id, _dtype = await self._resolve_dashboard_destination(guild)

        saved = await get_staff_dashboard(guild.id)
        if saved:
            saved_dest_id, saved_message_id = saved

            # Se o destino mudou (ex: thread deletada e recriada), atualiza no DB
            if saved_dest_id != dest_id:
                await set_staff_dashboard(guild.id, dest_id, saved_message_id)
                saved_dest_id = dest_id

            if saved_dest_id == dest_id:
                try:
                    msg = await destination.fetch_message(saved_message_id)  # type: ignore
                    return destination, dest_id, msg
                except discord.NotFound:
                    # mensagem apagada -> recriar abaixo
                    pass
                except discord.Forbidden as e:
                    raise RuntimeError(
                        "Sem permissão para ler histórico do dashboard. "
                        "Dê ao bot: Read Message History (e em threads: Send Messages in Threads)."
                    ) from e
        # cria nova mensagem fixa (ou porque não tinha registro, ou porque apagaram)
        embed = discord.Embed(
            title="📊 Dashboard Staff",
            description="Inicializando…",
            color=discord.Color.blurple(),
        )
        msg = await destination.send(embed=embed)  # type: ignore

        if saved:
            # Mantém o mesmo dest_id, mas atualiza message_id
            await update_staff_dashboard_message_id(guild.id, msg.id)
        else:
            await set_staff_dashboard(guild.id, dest_id, msg.id)

        return destination, dest_id, msg




    async def _collect_counts(self, guild: discord.Guild) -> dict:
        total = ajuda = denuncia = outros = 0
        oldest_dt = None
        oldest_link = None

        for th in guild.threads:
            if self.tickets_parent_id and th.parent_id != self.tickets_parent_id:
                continue
            if not is_open_thread(th):
                continue

            total += 1
            ttype = infer_ticket_type(th.name)
            if ttype == "ajuda":
                ajuda += 1
            elif ttype == "denuncia":
                denuncia += 1
            else:
                outros += 1

            if th.created_at and (oldest_dt is None or th.created_at < oldest_dt):
                oldest_dt = th.created_at
                oldest_link = th.jump_url
        # Fechados (total) por tipo (via DB de eventos)
        # Obs: abertos = threads abertas agora (contado acima). fechados = eventos registrados no DB.
        epoch_utc = datetime(1970, 1, 1, tzinfo=timezone.utc)
        try:
            ajuda_closed_total = await count_ticket_events(guild.id, "closed", epoch_utc, "ajuda")
            denuncia_closed_total = await count_ticket_events(guild.id, "closed", epoch_utc, "denuncia")
            outros_closed_total = await count_ticket_events(guild.id, "closed", epoch_utc, "outros")
        except Exception:
            ajuda_closed_total = denuncia_closed_total = outros_closed_total = 0


        # Whitelist pendente:
        # - Se WHITELIST_FORUM_ID existe, conta threads abertas nesse fórum (pendentes)
        # - Senão, usa count_whitelist_pending() (fallback do seu DB)
        if self.whitelist_parent_id:
            whitelist_pending = 0
            for th in guild.threads:
                if th.parent_id != self.whitelist_parent_id:
                    continue
                if is_open_thread(th):
                    whitelist_pending += 1
        else:
            whitelist_pending = await count_whitelist_pending()

        
        # Aprovadas/Reprovadas (via DB) — hoje e total
        try:
            since_utc = start_of_today_br().astimezone(timezone.utc)
            whitelist_approved_today = await count_whitelist_approved(since_utc)
            whitelist_rejected_today = await count_whitelist_rejected(since_utc)
            whitelist_approved_total = await count_whitelist_approved(None)
            whitelist_rejected_total = await count_whitelist_rejected(None)
        except Exception:
            # Se não existir tabela/func ou qualquer falha, cai pra 0
            whitelist_approved_today = whitelist_rejected_today = 0
            whitelist_approved_total = whitelist_rejected_total = 0

# VOZ suporte: categoria + ignora canal gatilho
        voice_channels: list[discord.VoiceChannel] = []

        if self.support_vc_ids:
            for cid in self.support_vc_ids:
                ch = guild.get_channel(cid)
                if isinstance(ch, discord.VoiceChannel):
                    voice_channels.append(ch)
        else:
            if self.support_vc_category_id:
                cat = guild.get_channel(self.support_vc_category_id)
                if isinstance(cat, discord.CategoryChannel):
                    voice_channels.extend(cat.voice_channels)

        if self.support_trigger_vc_id:
            voice_channels = [vc for vc in voice_channels if vc.id != self.support_trigger_vc_id]

        support_vcs_active = 0
        support_people_in_call = 0
        for vc in voice_channels:
            humans = [m for m in vc.members if not m.bot]
            if humans:
                support_vcs_active += 1
                support_people_in_call += len(humans)

        cutoff = start_of_today_br()
        new_today = 0
        for m in guild.members:
            if m.joined_at:
                if m.joined_at.astimezone(TZ) >= cutoff:
                    new_today += 1

        return {
            "tickets_total": total,
            "tickets_ajuda": ajuda,
            "tickets_denuncia": denuncia,
            "tickets_outros": outros,
            "ajuda_closed_total": ajuda_closed_total,
            "denuncia_closed_total": denuncia_closed_total,
            "outros_closed_total": outros_closed_total,
            "oldest_dt": oldest_dt,
            "oldest_link": oldest_link,
            "whitelist_pending": whitelist_pending,
            "whitelist_approved_today": whitelist_approved_today,
            "whitelist_rejected_today": whitelist_rejected_today,
            "whitelist_approved_total": whitelist_approved_total,
            "whitelist_rejected_total": whitelist_rejected_total,
            "support_vcs_active": support_vcs_active,
            "support_people_in_call": support_people_in_call,
            "new_today": new_today,
            "voice_channels_total": len(voice_channels),
        }

    def _build_embed(self, guild: discord.Guild, c: dict) -> discord.Embed:
        updated = now_br()
        ping_ms = int(self.bot.latency * 1000)

        level = "ok"
        if c["tickets_total"] >= 10 or c["whitelist_pending"] >= 10:
            level = "warn"
        if c["tickets_total"] >= 20 or (c["oldest_dt"] and (datetime.now(timezone.utc) - c["oldest_dt"]) > timedelta(hours=12)):
            level = "bad"

        # Tema preto/luxo (mantendo lógica)
        base_color = discord.Color.from_rgb(8, 8, 8)
        if level == "warn":
            base_color = discord.Color.from_rgb(26, 18, 8)
        elif level == "bad":
            base_color = discord.Color.from_rgb(28, 8, 8)

        crown = {"ok": "✦", "warn": "✧", "bad": "✦"}[level]
        status = {"ok": "Estável", "warn": "Atenção", "bad": "Crítico"}[level]

        embed = discord.Embed(
            title=f"{crown}  Dashboard Staff",
            description=(
                "```ansi\n"
                f"\u001b[1;37m{guild.name}\u001b[0m  •  "
                f"\u001b[1;30mPainel Executivo\u001b[0m\n"
                "```"
                f"**Status:** {status_emoji(level)} **{status}**   •   "
                f"**Latência:** `{ping_ms}ms`   •   "
                f"**Atualizado:** <t:{int(updated.timestamp())}:T>"
            ),
            color=base_color,
        )

        if guild.icon:
            embed.set_author(name="Centro de Operações da Staff", icon_url=guild.icon.url)
            embed.set_thumbnail(url=guild.icon.url)
        else:
            embed.set_author(name="Centro de Operações da Staff")

        resumo_lines = [
            "╭─ **Visão Geral**",
            f"├ Tickets abertos: **{c['tickets_total']}**",
            f"├ Whitelist pendente: **{c['whitelist_pending']}**",
            f"╰ Entradas hoje: **{c['new_today']}**",
        ]
        suporte_lines = [
            "╭─ **Operação de Voz**",
            f"├ Calls ativas: **{c['support_vcs_active']}** / **{c['voice_channels_total']}**",
            f"├ Pessoas em call: **{c['support_people_in_call']}**",
            f"╰ Tickets: {f'<#{self.ticket_threads_channel_id}>' if self.ticket_threads_channel_id else '—'}",
        ]

        embed.add_field(name="◈ Resumo", value="\n".join(resumo_lines), inline=True)
        embed.add_field(name="◈ Suporte", value="\n".join(suporte_lines), inline=True)

        embed.add_field(name=" ", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)

        ajuda_open = c["tickets_ajuda"]
        denuncia_open = c["tickets_denuncia"]
        ajuda_closed = c.get("ajuda_closed_total", 0)
        denuncia_closed = c.get("denuncia_closed_total", 0)

        max_help = max(10, ajuda_open, ajuda_closed)
        max_den = max(10, denuncia_open, denuncia_closed)

        tickets_lines = [
            "✦ **Tickets — Distribuição & Fluxo**",
            "",
            "🛠️ **Ajuda**",
            f"└ Abertos   `{bar(ajuda_open, max_help)}`  **{ajuda_open}**",
            f"└ Fechados  `{bar(ajuda_closed, max_help)}`  **{ajuda_closed}**",
            "",
            "🚨 **Denúncia**",
            f"└ Abertos   `{bar(denuncia_open, max_den)}`  **{denuncia_open}**",
            f"└ Fechados  `{bar(denuncia_closed, max_den)}`  **{denuncia_closed}**",
        ]

        if c["oldest_dt"]:
            rel = fmt_rel(c["oldest_dt"])
            if c["oldest_link"]:
                tickets_lines += ["", f"⏳ Mais antigo: {rel} • [abrir ticket]({c['oldest_link']})"]
            else:
                tickets_lines += ["", f"⏳ Mais antigo: {rel}"]

        embed.add_field(name="🎫 Tickets", value="\n".join(tickets_lines), inline=False)

        embed.add_field(name=" ", value="━━━━━━━━━━━━━━━━━━━━━━━━━━━━", inline=False)

        wl_pending_bar = bar(c["whitelist_pending"], 15)
        wl_approved_bar = bar(c.get("whitelist_approved_today", 0), 20)
        wl_rejected_bar = bar(c.get("whitelist_rejected_today", 0), 20)

        wl_lines = [
            "✦ **Whitelist — Fila & Resultado Diário**",
            "",
            f"⏳ Pendentes        `{wl_pending_bar}`  **{c['whitelist_pending']}**",
            f"✅ Aprovadas hoje   `{wl_approved_bar}`  **{c.get('whitelist_approved_today', 0)}**",
            f"❌ Reprovadas hoje  `{wl_rejected_bar}`  **{c.get('whitelist_rejected_today', 0)}**",
            "",
            f"📊 **Histórico total:** ✅ **{c.get('whitelist_approved_total', 0)}**  •  ❌ **{c.get('whitelist_rejected_total', 0)}**",
        ]

        links = []
        if self.whitelist_parent_id:
            links.append(f"📥 Fila <#{self.whitelist_parent_id}>")
        if self.whitelist_approval_thread_id:
            links.append(f"🧑‍⚖️ Aprovação <#{self.whitelist_approval_thread_id}>")
        if links:
            wl_lines += ["", " • ".join(links)]

        embed.add_field(name="🧾 Whitelist", value="\n".join(wl_lines), inline=False)

        alerts = []
        if c["tickets_total"] >= 10:
            alerts.append("🟠 Volume alto de tickets")
        if c["oldest_dt"] and (datetime.now(timezone.utc) - c["oldest_dt"]) > timedelta(hours=6):
            alerts.append("🟠 Ticket antigo aguardando ação")
        if c["whitelist_pending"] >= 10:
            alerts.append("🟠 Fila de whitelist elevada")
        if not alerts:
            alerts.append("🟢 Operação estável")

        sinais_val = "\n".join([f"• {a}" for a in alerts])
        sinais_val += "\n\n```ansi\n\u001b[1;30mMonitoramento contínuo ativo\u001b[0m\n```"
        embed.add_field(name="🚦 Sinais", value=sinais_val, inline=False)

        embed.set_footer(text="Painel Luxo • preto premium • persistente • auto-recuperação")
        return embed

    @tasks.loop(seconds=10)
    async def _heartbeat(self):
        # auto-cura: garante atualização mesmo sem eventos
        self.schedule_update(delay=0.5)

    @_heartbeat.before_loop
    async def _before_heartbeat(self):
        await self.bot.wait_until_ready()

    async def update_dashboard(self) -> None:
        async with self._update_lock:
            guild = await self._get_guild()
            if not guild:
                return
            try:
                destination, dest_id, msg = await self._ensure_message(guild)

                counts = await self._collect_counts(guild)
                embed = self._build_embed(guild, counts)
                await msg.edit(embed=embed)
            except Exception as e:
                print(f"[DASH] ERRO: {type(e).__name__}: {e}")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """
        Se apagarem a MENSAGEM do dashboard, o bot recria automaticamente.
        Usamos RAW porque funciona mesmo sem cache.
        """
        try:
            if not payload.guild_id or payload.guild_id != self.guild_id:
                return

            # Dashboard principal (staff)
            saved = await get_staff_dashboard(payload.guild_id)
            if saved:
                _saved_dest_id, saved_message_id = saved
                if payload.message_id == saved_message_id:
                    self.schedule_update(delay=0.8)
                    return
        except Exception as e:
            print(f"[DASH] ERRO on_raw_message_delete: {type(e).__name__}: {e}")


    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        """
        Se apagarem a THREAD/POST do dashboard (especialmente em fórum), agenda recriação.
        Usa RAW porque on_thread_delete pode não disparar com cache.
        """
        try:
            if not payload.guild_id or payload.guild_id != self.guild_id:
                return

            saved = await get_staff_dashboard(payload.guild_id)
            if not saved:
                return

            saved_dest_id, _saved_message_id = saved
            deleted_thread_id = getattr(payload, "thread_id", None) or getattr(payload, "id", None)
            if deleted_thread_id != saved_dest_id:
                return

            parent_id = getattr(payload, "parent_id", None)
            if parent_id:
                # Se o target apontava para thread fixa e ela foi apagada, troca para o fórum pai em memória
                # para a auto-cura recriar um novo post.
                self.dashboard_target_id = int(parent_id)

            self.schedule_update(delay=0.8)

        except Exception as e:
            print(f"[DASH] ERRO on_raw_thread_delete: {type(e).__name__}: {e}")




    @commands.Cog.listener()
    async def on_ready(self):
        if self.guild_id and self.dashboard_target_id:
            self.schedule_update(delay=2.0)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.guild and member.guild.id == self.guild_id:
            self.schedule_update(delay=2.0)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if not (thread.guild and thread.guild.id == self.guild_id):
            return
        # log de métricas (tickets) — só para threads do fórum de tickets
        try:
            if self.tickets_parent_id and thread.parent_id == self.tickets_parent_id:
                ttype = infer_ticket_type(thread.name)
                await log_ticket_event(thread.guild.id, thread.id, ttype, "opened")
        except Exception as e:
            print(f"[DASH] ERRO log opened: {type(e).__name__}: {e}")
        self.schedule_update()

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        if after.guild and after.guild.id == self.guild_id:
            changed = (before.archived != after.archived) or (before.locked != after.locked) or (before.name != after.name)
            if changed:
                # log fechado quando arquiva
                try:
                    if (self.tickets_parent_id and after.parent_id == self.tickets_parent_id and (not before.archived) and after.archived):
                        ttype = infer_ticket_type(after.name)
                        await log_ticket_event(after.guild.id, after.id, ttype, "closed")
                except Exception as e:
                    print(f"[DASH] ERRO log closed: {type(e).__name__}: {e}")
                self.schedule_update()

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        if not (thread.guild and thread.guild.id == self.guild_id):
            return
        # log fechado quando deletam o ticket (ex: Ajuda)
        try:
            if self.tickets_parent_id and thread.parent_id == self.tickets_parent_id:
                ttype = infer_ticket_type(thread.name)
                await log_ticket_event(thread.guild.id, thread.id, ttype, "closed")
        except Exception as e:
            print(f"[DASH] ERRO log closed (delete): {type(e).__name__}: {e}")
        self.schedule_update()

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild and member.guild.id == self.guild_id:
            self.schedule_update(delay=3.0)


async def setup(bot: commands.Bot):
    await bot.add_cog(StaffDashboard(bot))