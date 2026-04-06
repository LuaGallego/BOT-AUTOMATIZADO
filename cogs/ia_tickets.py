import os
import re
import json
import time
import random
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional

import discord
from discord.ext import commands


# =========================
# CONFIG (via .env)
# =========================
# Canal público onde fica o painel (embed + botão)
PANEL_CHANNEL_ID = int(os.getenv("AI_PANEL_CHANNEL_ID", "0"))

# Pode ser 1 ou vários cargos (separados por vírgula no .env)
# Ex: AI_STAFF_ROLE_ID=111111111111111111,222222222222222222
def parse_id_list(env_name: str) -> list[int]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return []
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids

STAFF_ROLE_IDS = parse_id_list("AI_STAFF_ROLE_ID")

# Base de conhecimento
BASE_DIR = Path(__file__).resolve().parent.parent
KB_PATH = BASE_DIR / "knowledge_base.json"

# Persistência
AI_THREADS_PATH = BASE_DIR / "ai_threads_map.json"
PANEL_STATE_PATH = BASE_DIR / "ai_panel_state.json"   # salva message_id do painel

# Limites
HISTORY_LIMIT = 16

# OpenAI (opcional)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

_openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import AsyncOpenAI
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        _openai_client = None


# =========================
# Helpers JSON
# =========================
def load_json(path: Path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# =========================
# Base de conhecimento / intenção
# =========================
def carregar_base_conhecimento() -> List[Dict[str, Any]]:
    return load_json(KB_PATH, [])


def detectar_intencao(texto: str) -> str:
    t = (texto or "").lower().strip()

    saudacoes = ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "tudo bem", "e ai", "e aí", "opa", "fala"]
    urgencia = ["socorro", "urgente", "agora", "rápido", "rapido", "help", "pelo amor", "me ajuda agora"]
    frustracao = ["nada funciona", "bugou", "ruim", "perdi", "droga", "aff", "não funciona", "nao funciona", "tá osso", "ta osso", "que ódio", "que odio"]
    problema = ["erro", "não caiu", "nao caiu", "não apareceu", "nao apareceu", "travou", "bug", "problema", "não abre", "nao abre"]
    pergunta = ["como", "onde", "qual", "pq", "por que", "porque", "?"]

    if any(x in t for x in urgencia):
        return "urgente"
    if any(x in t for x in frustracao):
        return "frustracao"
    if any(x in t for x in problema):
        return "problema"
    if any(x in t for x in saudacoes) and len(t.split()) <= 8:
        return "saudacao"
    if any(x in t for x in pergunta):
        return "duvida"
    return "geral"


STOPWORDS_KB = {
    "a", "o", "e", "de", "da", "do", "das", "dos", "na", "no", "nas", "nos",
    "um", "uma", "pra", "pro", "com", "sem", "por", "que", "como", "não", "nao"
}

def tokenize_text(texto: str) -> set[str]:
    tokens = re.findall(r"\b\w+\b", (texto or "").lower(), flags=re.UNICODE)
    return set(t for t in tokens if len(t) >= 3 and t not in STOPWORDS_KB)


def buscar_conhecimento(pergunta: str, kb: List[Dict[str, Any]], limite: int = 3) -> List[Dict[str, Any]]:
    pergunta_lower = (pergunta or "").lower()
    pergunta_tokens = tokenize_text(pergunta_lower)

    resultados = []

    for item in kb:
        score = 0
        kws = item.get("palavras_chave", [])

        for kw in kws:
            kw_lower = str(kw).lower().strip()
            if not kw_lower:
                continue

            # Match exato da frase inteira
            if kw_lower in pergunta_lower:
                score += 6
                continue

            # Match por tokens (evita casar só por "não")
            kw_tokens = tokenize_text(kw_lower)
            comuns = pergunta_tokens.intersection(kw_tokens)

            if len(comuns) >= 2:
                score += 4 + len(comuns)
            elif len(comuns) == 1:
                tok = next(iter(comuns))
                if len(tok) >= 4:
                    score += 2

        if score >= 3:
            resultados.append((score, item))

    resultados.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in resultados[:limite]]


def mensagem_vaga(texto: str) -> bool:
    t = (texto or "").lower().strip()
    vagas = {
        "nao sei", "não sei", "sei la", "sei lá", "sla", "hm", "hmm", "uh", "ue", "ué",
        "ok", "blz", "beleza", "ata", "ah", "sim", "não", "nao", "kk", "kkkk", "k", "rs", "rsrs"
    }
    return t in vagas or len(t) <= 2


def nome_bonito(display_name: str) -> str:
    nome = (display_name or "").strip()
    nome = re.sub(r"^\[[^\]]+\]\s*", "", nome).strip()  # remove tags tipo [DOOM]
    return nome or display_name


async def coletar_historico_thread(thread: discord.Thread, limite: int = HISTORY_LIMIT) -> str:
    msgs = []
    async for msg in thread.history(limit=limite, oldest_first=False):
        if msg.author.bot and not msg.content:
            continue
        nome = getattr(msg.author, "display_name", msg.author.name)
        conteudo = (msg.content or "").strip()
        if not conteudo:
            continue
        msgs.append(f"{nome}: {conteudo}")
    msgs.reverse()
    return "\n".join(msgs[-limite:])


# =========================
# Texto / tom / IA
# =========================
def pick_greeting(nome: str, intent: str) -> str:
    base = {
        "saudacao": [
            f"Oi, {nome}! 😊 Tudo bem por aí?",
            f"Opa, {nome}! Tudo certinho? 😄",
            f"E aí, {nome}! Como você tá? 👋",
            f"Fala, {nome}! Bora resolver sua dúvida? ✨",
        ],
        "duvida": [
            f"Claro, {nome}! 💙",
            f"Boa, {nome}! Vamos nessa 👇",
            f"Show, {nome}! Te explico rapidinho:",
            f"Perfeito, {nome}! Olha só:",
        ],
        "problema": [
            f"Poxa, {nome} 😕 vamos resolver isso juntas.",
            f"Entendi, {nome}. Vamos por partes pra destravar isso.",
            f"Tranquilo, {nome} — a gente ajeita isso agora.",
            f"Beleza, {nome}. Tenta esse passo primeiro 👇",
        ],
        "frustracao": [
            f"Entendi, {nome} 😔 calma que eu vou te ajudar certinho.",
            f"Tá, {nome}… respira comigo 😅 vamos por etapas.",
            f"Eu peguei, {nome}. Vamos focar no passo a passo.",
            f"Poxa, {nome}… vamos resolver isso agora 💙",
        ],
        "urgente": [
            f"{nome}, vamos direto ao ponto:",
            f"Fechado, {nome}. Vou ser objetiva aqui:",
            f"Ok, {nome}. Me passa isso e já resolvemos:",
            f"Entendi, {nome}. Vamos rápido e organizado:",
        ],
        "geral": [
            f"Beleza, {nome}! 😊",
            f"Entendi, {nome}.",
            f"Certo, {nome}! 👌",
            f"Show, {nome}.",
        ],
    }
    return random.choice(base.get(intent, base["geral"]))


def montar_resposta_local(nome: str, intent: str, texto: str, itens_kb: List[Dict[str, Any]]) -> str:
    # Saudação natural
    if intent == "saudacao":
        opcoes = [
            f"Oi, {nome}! 😊 Tudo certinho? Me conta sua dúvida que eu te ajudo por aqui.",
            f"Opa, {nome}! ✨ Tudo bem? Pode mandar sua dúvida.",
            f"Fala, {nome}! 👋 Manda sua dúvida que eu vejo com você.",
            f"Oi, {nome}! 💙 Pode me contar o que aconteceu?"
        ]
        return random.choice(opcoes)

    # Mensagens vagas / brincadeira sem contexto
    if mensagem_vaga(texto):
        opcoes = [
            f"Sem problema, {nome} 💙 Me fala em uma frase o que aconteceu (ex.: \"meu carro não caiu\" ou \"como pega steamid64?\") que eu tento te ajudar certinho.",
            f"Tranquilo, {nome} 😄 Se quiser, me conta melhor a dúvida e eu vejo com você.",
            f"Sem estresse, {nome}. Me dá um contexto rapidinho e eu já tento te orientar ✨",
        ]
        return random.choice(opcoes)

    # Sem KB encontrada: ainda conversa como IA “geral”, mas com limite
    if not itens_kb:
        # tom mais humano + sem inventar regra do servidor
        if intent in {"frustracao", "problema"}:
            return (
                f"{pick_greeting(nome, intent)}\n"
                "Eu não achei essa informação específica na minha base do servidor ainda.\n"
                "Se for algo geral, me explica melhor que eu tento te orientar. "
                "Se for regra/decisão do servidor, eu posso chamar a staff pra confirmar certinho."
            )

        return (
            f"{pick_greeting(nome, intent)}\n"
            "Eu ainda não tenho isso cadastrado na base do servidor 😕\n"
            "Se você quiser, me explica melhor e eu tento ajudar no geral. "
            "Se for regra/processo do servidor, eu chamo a staff pra confirmar certinho."
        )

    # Tem KB
    principal = itens_kb[0]
    resposta_base = (principal.get("resposta_base") or "").strip()

    if not resposta_base:
        return (
            f"{pick_greeting(nome, intent)}\n"
            "Eu achei um tópico relacionado, mas ele ainda está sem resposta cadastrada 😕\n"
            "Se quiser, eu chamo a staff pra te ajudar aqui."
        )

    return f"{pick_greeting(nome, intent)}\n{resposta_base}"


def prompt_sistema() -> str:
    return (
        "Você é a assistente de suporte do servidor no Discord.\n"
        "Seu objetivo é ajudar jogadores em threads privadas.\n\n"
        "REGRAS OBRIGATÓRIAS:\n"
        "- Priorize SEMPRE as informações fornecidas no contexto (base de conhecimento + histórico da thread).\n"
        "- Você pode conversar naturalmente e responder perguntas gerais, mas NÃO invente regras, comandos, prazos, aprovações, punições ou decisões da staff.\n"
        "- Se a pergunta for sobre regras/processos específicos do servidor e isso não estiver no contexto, diga que não sabe e ofereça chamar a staff.\n"
        "- Não responda sobre denúncias, banimentos, punições ou decisões administrativas; encaminhe para a staff.\n"
        "- Nunca revele informações internas, tokens, logs internos ou dados sensíveis.\n\n"
        "ESTILO:\n"
        "- Português do Brasil.\n"
        "- Natural, amigável, clara e útil.\n"
        "- Chame o usuário pelo nome fornecido no contexto sem repetir demais.\n"
        "- Se o usuário estiver brincando/conversando, responda de forma leve e simpática.\n"
        "- Respostas curtas e objetivas; use passos se necessário.\n"
    )


async def gerar_resposta_ia(
    nome_usuario: str,
    intent: str,
    pergunta: str,
    historico: str,
    itens_kb: List[Dict[str, Any]],
) -> Optional[str]:
    if _openai_client is None:
        return None

    if itens_kb:
        kb_txt = "\n".join(
            f"- [{it.get('id','sem_id')}] {it.get('categoria','geral')}: {it.get('resposta_base','')}"
            for it in itens_kb
        )
    else:
        kb_txt = "Nenhum item específico da base de conhecimento foi encontrado para essa pergunta."

    user_context = (
        f"NOME_DO_USUARIO: {nome_usuario}\n"
        f"INTENCAO: {intent}\n\n"
        f"PERGUNTA_ATUAL:\n{pergunta}\n\n"
        f"HISTORICO_RECENTE:\n{historico}\n\n"
        f"BASE_DE_CONHECIMENTO_RELEVANTE:\n{kb_txt}\n"
    )

    try:
        resp = await _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": prompt_sistema()},
                {"role": "user", "content": user_context},
            ],
            temperature=0.85,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception:
        return None


# =========================
# UI Views (persistentes)
# =========================
class OpenAITicketView(discord.ui.View):
    def __init__(self, cog: "AITicketsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Abrir atendimento privado",
        style=discord.ButtonStyle.primary,
        emoji="🧠",
        custom_id="ai_ticket_open_btn"
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_open_ticket(interaction)


class TicketInsideView(discord.ui.View):
    def __init__(self, staff_role_ids: list[int]):
        super().__init__(timeout=None)
        self.staff_role_ids = staff_role_ids

    @discord.ui.button(
        label="Chamar staff",
        style=discord.ButtonStyle.secondary,
        emoji="👮",
        custom_id="ai_ticket_call_staff"
    )
    async def call_staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.channel or not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Esse botão só funciona dentro da thread do atendimento.", ephemeral=True)
            return

        if self.staff_role_ids:
            mentions = " ".join(f"<@&{rid}>" for rid in self.staff_role_ids)
            await interaction.response.send_message(
                f"Chamando a staff {mentions}",
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        else:
            await interaction.response.send_message("Chamei a staff ✅", ephemeral=False)

    @discord.ui.button(
        label="Encerrar atendimento",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ai_ticket_close"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.channel or not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message("Esse botão só funciona dentro da thread do atendimento.", ephemeral=True)
            return

        await interaction.response.send_message("Atendimento encerrado ✅")
        try:
            await interaction.channel.edit(archived=True, locked=True)
        except Exception:
            pass


# =========================
# Cog principal
# =========================
class AITicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.kb = carregar_base_conhecimento()
        self.ai_threads = load_json(AI_THREADS_PATH, {})       # {"thread_id": {...}}
        self.panel_state = load_json(PANEL_STATE_PATH, {})     # {"channel_id":..., "message_id":...}
        self._panel_lock = asyncio.Lock()
        self._panel_task_started = False
        self._last_user_ticket = {}  # anti-spam simples (user_id -> timestamp)

        # Views persistentes (funcionam após restart)
        self.bot.add_view(OpenAITicketView(self))
        self.bot.add_view(TicketInsideView(STAFF_ROLE_IDS))

    # ---------- Persistência ----------
    def mark_ai_thread(self, thread_id: int, opener_id: int):
        self.ai_threads[str(thread_id)] = {
            "opener_id": opener_id,
            "created_at": int(time.time())
        }
        save_json(AI_THREADS_PATH, self.ai_threads)

    def is_ai_thread(self, thread: discord.Thread) -> bool:
        return str(thread.id) in self.ai_threads

    def save_panel_message(self, channel_id: int, message_id: int):
        self.panel_state = {"channel_id": channel_id, "message_id": message_id}
        save_json(PANEL_STATE_PATH, self.panel_state)

    # ---------- Painel premium ----------
    def build_panel_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="✦ Assistente Inteligente de Suporte",
            description=(
                "### Atendimento privado com IA + Staff\n"
                "Clique no botão abaixo para abrir um **atendimento privado**.\n\n"
                "🧠 **A assistente ajuda com dúvidas do servidor e do jogo**\n"
                "💬 **Conversa natural** (não é resposta engessada)\n"
                "🔒 **Privado**: só você, a equipe e o bot\n"
                "👮 **Staff acompanha** quando necessário\n\n"
                "> A assistente prioriza as informações do servidor.\n"
                "> Regras/decisões sensíveis são encaminhadas para a staff."
            ),
            color=discord.Color.from_rgb(88, 101, 242)
        )

        embed.add_field(
            name="✨ O que ela pode fazer",
            value=(
                "• Responder dúvidas comuns\n"
                "• Explicar passo a passo\n"
                "• Conversar naturalmente\n"
                "• Encaminhar pra staff quando necessário\n"
                "• Ajudar mesmo se você não souber explicar direito"
            ),
            inline=False
        )

        embed.add_field(
            name="⚡ Como funciona",
            value=(
                "1. Clique em **Abrir atendimento privado**\n"
                "2. O bot cria sua thread privada\n"
                "3. Converse normalmente por lá\n"
                "4. Se precisar, clique em **Chamar staff**"
            ),
            inline=False
        )

        embed.set_footer(text="Atendimento inteligente • Seguro • Privado")
        return embed

    async def ensure_panel(self):
        async with self._panel_lock:
            if PANEL_CHANNEL_ID == 0:
                print("[AI Tickets] AI_PANEL_CHANNEL_ID não configurado.")
                return

            guild_channel = self.bot.get_channel(PANEL_CHANNEL_ID)
            if not guild_channel or not isinstance(guild_channel, discord.TextChannel):
                print("[AI Tickets] Canal do painel inválido (precisa ser TextChannel).")
                return

            saved_channel_id = self.panel_state.get("channel_id")
            saved_message_id = self.panel_state.get("message_id")

            panel_message_exists = False
            if saved_channel_id == guild_channel.id and isinstance(saved_message_id, int):
                try:
                    _ = await guild_channel.fetch_message(saved_message_id)
                    panel_message_exists = True
                except discord.NotFound:
                    panel_message_exists = False
                except Exception:
                    panel_message_exists = False

            if not panel_message_exists:
                try:
                    msg = await guild_channel.send(
                        embed=self.build_panel_embed(),
                        view=OpenAITicketView(self)
                    )
                    self.save_panel_message(guild_channel.id, msg.id)
                    print(f"[AI Tickets] Painel criado/recriado em #{guild_channel.name} (msg {msg.id})")
                except Exception as e:
                    print(f"[AI Tickets] Falha ao criar painel: {e}")

    async def panel_watchdog_loop(self):
        await self.bot.wait_until_ready()
        await self.ensure_panel()

        while not self.bot.is_closed():
            try:
                await self.ensure_panel()
            except Exception as e:
                print(f"[AI Tickets] Erro no watchdog do painel: {e}")
            await asyncio.sleep(20)

    # ---------- Criação de ticket (NO MESMO CANAL DO PAINEL) ----------
    async def handle_open_ticket(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Esse botão só funciona dentro do servidor.", ephemeral=True)
            return

        # cooldown anti-spam básico
        now = time.time()
        last = self._last_user_ticket.get(interaction.user.id, 0)
        if now - last < 5:
            await interaction.response.send_message(
                "Calma 😅 espera alguns segundos e tenta de novo.",
                ephemeral=True
            )
            return
        self._last_user_ticket[interaction.user.id] = now

        # Usa o canal onde o botão foi clicado (o canal do painel)
        if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "Esse botão precisa estar em um canal de texto.",
                ephemeral=True
            )
            return

        parent = interaction.channel

        user = interaction.user
        display_raw = getattr(user, "display_name", user.name)
        display = nome_bonito(display_raw)
        safe = "".join(ch for ch in display.lower() if ch.isalnum() or ch in ("-", "_"))[:18] or "usuario"
        short = str(user.id)[-4:]
        thread_name = f"duvida-{safe}-{short}"

        # tenta achar thread já aberta da pessoa
        for t in parent.threads:
            if str(t.id) in self.ai_threads:
                info = self.ai_threads.get(str(t.id), {})
                if info.get("opener_id") == user.id and not t.archived:
                    await interaction.response.send_message(
                        f"Você já tem um atendimento aberto: {t.mention}",
                        ephemeral=True
                    )
                    return

        try:
            thread = await parent.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                auto_archive_duration=1440,
                invitable=False,
                reason="Atendimento privado com IA"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Eu não tenho permissão para criar **thread privada** nesse canal.\n"
                "Permissões necessárias: **Create Private Threads**, **Send Messages in Threads** e **View Channel**.",
                ephemeral=True
            )
            return
        except Exception as e:
            await interaction.response.send_message(f"Não consegui criar o atendimento: {e}", ephemeral=True)
            return

        try:
            await thread.add_user(user)
        except Exception:
            pass

        self.mark_ai_thread(thread.id, user.id)

        staff_mentions = " ".join(f"<@&{rid}>" for rid in STAFF_ROLE_IDS) if STAFF_ROLE_IDS else None

        welcome = discord.Embed(
            title="✦ Atendimento Privado Aberto",
            description=(
                f"Olá, **{display}**! 👋\n\n"
                "Seu atendimento foi aberto com sucesso.\n"
                "Pode mandar sua dúvida aqui do jeito que você quiser (explicando, resumindo, até brincando 😄).\n\n"
                "Eu vou te ajudar com base nas informações do servidor e no contexto da conversa.\n"
                "Se eu não tiver certeza de algo, eu aviso e você pode usar o botão **Chamar staff**."
            ),
            color=discord.Color.green()
        )
        welcome.add_field(
            name="💡 Dica",
            value="Quanto mais contexto você me der (o que tentou / o que apareceu / onde travou), melhor eu te ajudo.",
            inline=False
        )
        welcome.set_footer(text="Atendimento privado • Resposta inteligente • Staff disponível")

        await thread.send(
            content=staff_mentions,
            embed=welcome,
            view=TicketInsideView(STAFF_ROLE_IDS),
            allowed_mentions=discord.AllowedMentions(roles=True)
        )

        await interaction.response.send_message(
            f"Seu atendimento foi aberto: {thread.mention}",
            ephemeral=True
        )

    # ---------- Listener de conversa ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return
        if not isinstance(message.channel, discord.Thread):
            return
        if not self.is_ai_thread(message.channel):
            return

        texto = (message.content or "").strip()
        if not texto:
            return
        if texto.startswith("/") or texto.startswith("!"):
            return

        nome_raw = getattr(message.author, "display_name", message.author.name)
        nome = nome_bonito(nome_raw)

        intent = detectar_intencao(texto)
        itens = buscar_conhecimento(texto, self.kb)
        historico = await coletar_historico_thread(message.channel, limite=HISTORY_LIMIT)

        async with message.channel.typing():
            # Se tiver OpenAI, usa IA com contexto (mais natural)
            resp_ai = await gerar_resposta_ia(
                nome_usuario=nome,
                intent=intent,
                pergunta=texto,
                historico=historico,
                itens_kb=itens,
            )

            if resp_ai:
                await message.reply(resp_ai, mention_author=False)
                return

            # Fallback local
            resposta = montar_resposta_local(nome, intent, texto, itens)
            await message.reply(resposta, mention_author=False)

    # ---------- on_ready ----------
    @commands.Cog.listener()
    async def on_ready(self):
        self.kb = carregar_base_conhecimento()

        if not self._panel_task_started:
            self._panel_task_started = True
            self.bot.loop.create_task(self.panel_watchdog_loop())
            print("[AI Tickets] Watchdog do painel iniciado.")

        print(f"[AI Tickets] PANEL_CHANNEL_ID={PANEL_CHANNEL_ID}")
        print(f"[AI Tickets] STAFF_ROLE_IDS={STAFF_ROLE_IDS}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AITicketsCog(bot))