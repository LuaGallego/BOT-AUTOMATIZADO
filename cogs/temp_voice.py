import os
import re
import asyncio
import discord
from discord.ext import commands

# ================== CONFIG ==================

# SALAS TEMP NORMAIS
TRIGGER_VOICE_CHANNEL_ID = int(os.getenv("TRIGGER_VOICE_CHANNEL_ID", "0"))  # canal gatilho "➕ Criar Sala"
TEMP_CATEGORY_ID = int(os.getenv("TEMP_CATEGORY_ID", "0"))                  # categoria "🎧 Salas Temporárias"

# SALAS DE SUPORTE
SUPPORT_TRIGGER_VOICE_CHANNEL_ID = int(os.getenv("SUPPORT_TRIGGER_VOICE_CHANNEL_ID", "0"))  # canal gatilho suporte
SUPPORT_CATEGORY_ID = int(os.getenv("SUPPORT_CATEGORY_ID", "0"))                             # categoria suporte

# VÁRIOS CARGOS DE STAFF (IDs separados por vírgula no .env)
# Ex: SUPPORT_STAFF_ROLE_IDS=111,222,333
SUPPORT_STAFF_ROLE_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("SUPPORT_STAFF_ROLE_IDS", "").split(",")
    if x.strip().isdigit()
}

# CARGOS PREMIUM / DONATE (IDs separados por vírgula no .env)
# Ex: DONOR_ROLE_IDS=111,222
DONOR_ROLE_IDS: set[int] = {
    int(x.strip())
    for x in os.getenv("DONOR_ROLE_IDS", "").split(",")
    if x.strip().isdigit()
}

# ================== REGEX ==================

VOICE_NAME_RE = re.compile(r"^Canal de Voz (\d+)$", re.IGNORECASE)
SUPPORT_VOICE_RE = re.compile(r"^Suporte (\d+)$", re.IGNORECASE)

# ================== UTILS ==================

def next_number(category: discord.CategoryChannel, regex: re.Pattern) -> int:
    used = set()
    for ch in category.channels:
        if isinstance(ch, discord.VoiceChannel):
            m = regex.match(ch.name.strip())
            if m:
                used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return n


# ================== PREMIUM UI (DM) ==================

class LimitModal(discord.ui.Modal, title="Definir limite de pessoas"):
    limit = discord.ui.TextInput(
        label="Quantidade (0 = sem limite)",
        placeholder="Ex: 2, 5, 10 (ou 0)",
        min_length=1,
        max_length=3,
        required=True
    )

    def __init__(self, apply_cb):
        super().__init__()
        self._apply_cb = apply_cb

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.limit.value).strip()
        if not raw.isdigit():
            return await interaction.response.send_message("❌ Digite apenas números (ex: 0, 5, 10).", ephemeral=True)

        value = int(raw)
        if value < 0 or value > 99:
            return await interaction.response.send_message("❌ Use um número entre 0 e 99.", ephemeral=True)

        ok, msg = await self._apply_cb(interaction, value)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg, ephemeral=True)


class RenameModal(discord.ui.Modal, title="Renomear sua sala"):
    name = discord.ui.TextInput(
        label="Novo nome",
        placeholder="Ex: Sala da Luanda ✨",
        min_length=1,
        max_length=70,
        required=True
    )

    def __init__(self, apply_cb):
        super().__init__()
        self._apply_cb = apply_cb

    async def on_submit(self, interaction: discord.Interaction):
        new_name = str(self.name.value).strip()
        ok, msg = await self._apply_cb(interaction, new_name)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg, ephemeral=True)


class KickSelect(discord.ui.Select):
    def __init__(self, members: list[discord.Member], apply_cb):
        options = []
        for m in members[:25]:
            options.append(discord.SelectOption(label=m.display_name, value=str(m.id)))

        super().__init__(
            placeholder="Selecione alguém para expulsar…",
            min_values=1,
            max_values=1,
            options=options
        )
        self._apply_cb = apply_cb

    async def callback(self, interaction: discord.Interaction):
        target_id = int(self.values[0])
        ok, msg = await self._apply_cb(interaction, target_id)
        await interaction.response.send_message(("✅ " if ok else "❌ ") + msg, ephemeral=True)


class KickView(discord.ui.View):
    def __init__(self, members: list[discord.Member], apply_cb):
        super().__init__(timeout=120)
        self.add_item(KickSelect(members, apply_cb))


class PremiumControlsView(discord.ui.View):
    def __init__(self, cog: "TempVoice", owner_user_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.owner_user_id = owner_user_id

    async def _guard(self, interaction: discord.Interaction):
        # Interação vem no DM (guild = None). A gente valida pelo mapping salvo no cog.
        if interaction.user.id != self.owner_user_id:
            return False, "Esse painel não é seu."

        ctx = self.cog.premium_voice_ctx.get(self.owner_user_id)
        if not ctx:
            return False, "Não encontrei sua sala premium (talvez ela já tenha sido apagada)."

        guild = self.cog.bot.get_guild(ctx["guild_id"])
        if not guild:
            return False, "Não consegui acessar o servidor dessa sala."

        member = guild.get_member(self.owner_user_id)
        if not member:
            return False, "Você não está mais no servidor."

        if not self.cog._is_donor(member):
            return False, "Você não tem mais o cargo premium necessário."

        ch = guild.get_channel(ctx["voice_id"])
        if not isinstance(ch, discord.VoiceChannel):
            return False, "Sua sala não existe mais."

        # precisa estar dentro da própria sala
        if not member.voice or member.voice.channel.id != ch.id:
            return False, "Você precisa estar dentro da sua sala para usar esses botões."

        # precisa ser o dono salvo
        if self.cog.temp_voice_owner.get(ch.id) != member.id:
            return False, "Você não é a dona dessa sala (ou ela foi recriada)."

        return True, (guild, member, ch)

    @discord.ui.button(label="🔒 Trancar", style=discord.ButtonStyle.danger)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, data = await self._guard(interaction)
        if not ok:
            return await interaction.response.send_message(f"❌ {data}", ephemeral=True)

        guild, member, ch = data
        try:
            ow = ch.overwrites_for(guild.default_role)
            ow.view_channel = True
            ow.connect = False
            await ch.set_permissions(guild.default_role, overwrite=ow, reason="Premium: trancar sala")
            await interaction.response.send_message("Sala trancada. Só quem já está dentro fica, e ninguém novo entra. 🔒", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Erro ao trancar: {e}", ephemeral=True)

    @discord.ui.button(label="🔓 Destrancar", style=discord.ButtonStyle.success)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, data = await self._guard(interaction)
        if not ok:
            return await interaction.response.send_message(f"❌ {data}", ephemeral=True)

        guild, member, ch = data
        try:
            ow = ch.overwrites_for(guild.default_role)
            ow.view_channel = True
            ow.connect = True
            await ch.set_permissions(guild.default_role, overwrite=ow, reason="Premium: destrancar sala")
            await interaction.response.send_message("Sala destrancada. Agora qualquer um pode entrar. 🔓", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Erro ao destrancar: {e}", ephemeral=True)

    @discord.ui.button(label="👥 Definir limite", style=discord.ButtonStyle.primary)
    async def set_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def apply_cb(_interaction: discord.Interaction, value: int):
            ok, data = await self._guard(_interaction)
            if not ok:
                return False, data
            _, _, ch = data
            try:
                await ch.edit(user_limit=value, reason="Premium: definir limite")
                if value == 0:
                    return True, "Limite removido (sem limite)."
                return True, f"Limite definido para {value} pessoa(s)."
            except Exception as e:
                return False, f"Erro ao definir limite: {e}"

        await interaction.response.send_modal(LimitModal(apply_cb))

    @discord.ui.button(label="✏️ Renomear", style=discord.ButtonStyle.secondary)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def apply_cb(_interaction: discord.Interaction, new_name: str):
            ok, data = await self._guard(_interaction)
            if not ok:
                return False, data
            _, _, ch = data
            try:
                await ch.edit(name=new_name, reason="Premium: renomear sala")
                return True, f"Nome alterado para **{new_name}**."
            except Exception as e:
                return False, f"Erro ao renomear: {e}"

        await interaction.response.send_modal(RenameModal(apply_cb))

    @discord.ui.button(label="🚫 Expulsar alguém", style=discord.ButtonStyle.danger)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, data = await self._guard(interaction)
        if not ok:
            return await interaction.response.send_message(f"❌ {data}", ephemeral=True)

        guild, member, ch = data

        members = [m for m in ch.members if m.id != member.id]
        if not members:
            return await interaction.response.send_message("Não tem ninguém na sala pra expulsar. 😌", ephemeral=True)

        async def apply_cb(_interaction: discord.Interaction, target_id: int):
            ok2, data2 = await self._guard(_interaction)
            if not ok2:
                return False, data2
            _, owner_member, owner_ch = data2

            target = guild.get_member(target_id)
            if not target or not target.voice or target.voice.channel.id != owner_ch.id:
                return False, "Essa pessoa não está mais na sua sala."

            try:
                # Tenta mandar pro canal gatilho (ou desconectar se não der)
                trigger = guild.get_channel(TRIGGER_VOICE_CHANNEL_ID)
                if isinstance(trigger, discord.VoiceChannel):
                    await target.move_to(trigger, reason="Premium: expulso da sala")
                else:
                    await target.move_to(None, reason="Premium: expulso da sala")

                return True, f"**{target.display_name}** foi expulso(a) da sala."
            except Exception as e:
                return False, f"Erro ao expulsar: {e}"

        await interaction.response.send_message(
            "Escolhe quem você quer expulsar:",
            view=KickView(members, apply_cb),
            ephemeral=True
        )


# ================== COG ==================

class TempVoice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.temp_voice_owner: dict[int, int] = {}     # voice_id -> owner_id (salas normais)
        self.support_voice_owner: dict[int, int] = {}  # voice_id -> owner_id (salas suporte)
        self._cleaned_once = False

        # user_id -> {guild_id, voice_id}
        self.premium_voice_ctx: dict[int, dict[str, int]] = {}

    def _is_temp_voice_channel(self, ch: discord.abc.GuildChannel) -> bool:
        return (
            isinstance(ch, discord.VoiceChannel)
            and ch.category_id == TEMP_CATEGORY_ID
            and ch.id != TRIGGER_VOICE_CHANNEL_ID
        )

    def _is_support_voice_channel(self, ch: discord.abc.GuildChannel) -> bool:
        return (
            isinstance(ch, discord.VoiceChannel)
            and ch.category_id == SUPPORT_CATEGORY_ID
            and ch.id != SUPPORT_TRIGGER_VOICE_CHANNEL_ID
        )

    def _is_donor(self, member: discord.Member) -> bool:
        if not DONOR_ROLE_IDS:
            return False
        return any(r.id in DONOR_ROLE_IDS for r in member.roles)

    async def _send_premium_dm(self, member: discord.Member, voice: discord.VoiceChannel):
        desc = (
            f"**DOOM PROJECT - PAINEL EXECUTIVO**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Olá, **{member.display_name}**! Sua sala privada foi ativada com sucesso.\n"
            f"Use este painel exclusivo para gerenciar os acessos da sua sala.\n\n"
            f"🎧 **Sua Sala:** `{voice.name}`\n"
            f"🆔 **ID da Sala:** `{voice.id}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

        # Aqui definimos a lateral vermelha e o título solicitado
        embed = discord.Embed(
            title="Benefícios VIPs",
            description=desc,
            color=0xED4245 # Vermelho Padrão Discord (Lateral)
        )
        
        # O "D do Doom" no topo da Embed
        embed.set_author(
            name="Painel Doom Project Recursos Vips", 
            icon_url=self.bot.user.display_avatar.url
            
            
        )

        embed.add_field(
            name="🔐 CONTROLE DE ACESSO",
            value=(
                "> `🔒 Trancar` • Bloqueia novas entradas.\n"
                "> `🔓 Destrancar` • Libera o acesso geral.\n"
                "> `👥 Limite` • Define a capacidade máxima."
            ),
            inline=False
        )

        embed.add_field(
            name="🎛️ MODERAÇÃO DA SALA",
            value=(
                "> `✏️ Renomear` • Altera o nome da sala.\n"
                "> `🚫 Expulsar` • Remove e desconecta um usuário."
            ),
            inline=False
        )

        embed.add_field(
            name="⚠️ REGRAS DO SISTEMA",
            value=(
                "```yaml\n"
                "1. O painel só funciona se você estiver dentro da sala.\n"
                "2. A sala é deletada automaticamente quando ficar vazia.\n"
                "```"
            ),
            inline=False
        )

        embed.set_thumbnail(url=member.display_avatar.url)
        
        # Banner em GIF (você pode trocar a URL pelo banner VIP oficial de vocês)
        embed.set_image(url="https://media1.giphy.com/media/v1.Y2lkPTc5MGI3NjExcXR6cHlweDNrODN5YWx6NnJheDVjMHQ3MGZhM3R2OXFid3F3ZmY5MCZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/2yAVR8EY1MVBb1s9sO/giphy.gif")
        
        embed.set_footer(
            text="Doom Project VIP Hub • Acesso Exclusivo",
        )

        try:
            await member.send(embed=embed, view=PremiumControlsView(self, member.id))
        except discord.Forbidden:
            pass # Usuário bloqueou as DMs
        except Exception as e:
            print(f"❌ Erro ao enviar DM premium: {e}")
    # ==========================================================
    # LIMPEZA PÓS RESTART (se o bot cair, ao voltar apaga vazias)
    # ==========================================================
    @commands.Cog.listener()
    async def on_ready(self):
        if self._cleaned_once:
            return
        self._cleaned_once = True

        for guild in self.bot.guilds:

            # ---------- LIMPA/RESTAURA SALAS TEMP ----------
            temp_cat = guild.get_channel(TEMP_CATEGORY_ID)
            if isinstance(temp_cat, discord.CategoryChannel):
                for ch in list(temp_cat.channels):
                    if self._is_temp_voice_channel(ch):
                        if len(ch.members) == 0:
                            try:
                                await ch.delete(reason="Limpeza pós-restart (temp vazia)")
                            except Exception as e:
                                print(f"❌ Erro deletando {ch.name}: {e}")
                        else:
                            # Mantém compatibilidade: primeiro membro presente vira owner restaurado
                            self.temp_voice_owner[ch.id] = ch.members[0].id

                            # Se esse owner for donor, restaura contexto premium também
                            owner_member = guild.get_member(ch.members[0].id)
                            if owner_member and self._is_donor(owner_member):
                                self.premium_voice_ctx[owner_member.id] = {
                                    "guild_id": guild.id,
                                    "voice_id": ch.id
                                }

            # ---------- LIMPA/RESTAURA SALAS SUPORTE ----------
            sup_cat = guild.get_channel(SUPPORT_CATEGORY_ID)
            if isinstance(sup_cat, discord.CategoryChannel):
                for ch in list(sup_cat.channels):
                    if self._is_support_voice_channel(ch):
                        if len(ch.members) == 0:
                            try:
                                await ch.delete(reason="Limpeza pós-restart (suporte vazio)")
                            except Exception as e:
                                print(f"❌ Erro deletando suporte {ch.name}: {e}")
                        else:
                            self.support_voice_owner[ch.id] = ch.members[0].id

        print("✅ TempVoice: limpeza/restore pós-restart concluída")

    # ==========================================================
    # EVENTO PRINCIPAL
    # ==========================================================
    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ):
        guild = member.guild

        # ======================================================
        # 1) Entrou no canal gatilho NORMAL => cria sala normal
        # ======================================================
        if after.channel and after.channel.id == TRIGGER_VOICE_CHANNEL_ID:
            category = guild.get_channel(TEMP_CATEGORY_ID)

            if not isinstance(category, discord.CategoryChannel):
                print("❌ TEMP_CATEGORY_ID inválido ou não é categoria.")
                return

            is_donor = self._is_donor(member)

            # Permissões:
            # - todo mundo entra normal
            # - DONOR ganha controles (manage_channels/move_members etc.)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                    use_voice_activation=True,
                    stream=True,
                ),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                    use_voice_activation=True,
                    stream=True,
                    manage_channels=is_donor,
                    move_members=is_donor,
                    mute_members=is_donor,
                    deafen_members=is_donor,
                ),
            }

            n = next_number(category, VOICE_NAME_RE)
            name = f"Canal de Voz {n}"

            temp_channel = await guild.create_voice_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason="Sala temporária criada"
            )

            self.temp_voice_owner[temp_channel.id] = member.id

            try:
                await member.move_to(temp_channel, reason="Mover para sala temporária")
            except Exception as e:
                print(f"❌ Erro ao mover membro (temp): {e}")

            # Se for donor: salva contexto e manda DM premium
            if is_donor:
                self.premium_voice_ctx[member.id] = {"guild_id": guild.id, "voice_id": temp_channel.id}
                await self._send_premium_dm(member, temp_channel)

        # ======================================================
        # 2) Entrou no canal gatilho SUPORTE => cria sala suporte
        # ======================================================
        if after.channel and after.channel.id == SUPPORT_TRIGGER_VOICE_CHANNEL_ID:
            category = guild.get_channel(SUPPORT_CATEGORY_ID)

            if not isinstance(category, discord.CategoryChannel):
                print("❌ SUPPORT_CATEGORY_ID inválido ou não é categoria.")
                return

            # Permissões:
            # - todo mundo vê, mas não entra
            # - o user entra e gerencia
            # - cargos de staff entram e gerenciam
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=False,
                    speak=False,
                ),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    connect=True,
                    speak=True,
                    use_voice_activation=True,
                    stream=True,
                    manage_channels=True,
                    move_members=True,
                    mute_members=True,
                    deafen_members=True,
                ),
            }

            # adiciona TODOS os cargos de staff
            for rid in SUPPORT_STAFF_ROLE_IDS:
                role = guild.get_role(rid)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        use_voice_activation=True,
                        stream=True,
                        manage_channels=True,
                        move_members=True,
                        mute_members=True,
                        deafen_members=True,
                    )

            n = next_number(category, SUPPORT_VOICE_RE)
            name = f"Suporte {n}"

            support_channel = await guild.create_voice_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason="Sala de suporte criada"
            )

            self.support_voice_owner[support_channel.id] = member.id

            try:
                await member.move_to(support_channel, reason="Mover para sala de suporte")
            except Exception as e:
                print(f"❌ Erro ao mover membro (suporte): {e}")



        # ======================================================
        # 3) Saiu de canal TEMP => apaga se vazio
        # ======================================================
        if before.channel and self._is_temp_voice_channel(before.channel):
            ch = before.channel
            await asyncio.sleep(3)

            if len(ch.members) == 0:
                try:
                    await ch.delete(reason="Sala temporária vazia")
                except Exception as e:
                    print(f"❌ Erro ao deletar canal temp: {e}")

                self.temp_voice_owner.pop(ch.id, None)

                # limpa premium ctx se apontava pra esse canal
                owner_id = None
                for uid, ctx in list(self.premium_voice_ctx.items()):
                    if ctx.get("voice_id") == ch.id:
                        owner_id = uid
                        break
                if owner_id:
                    self.premium_voice_ctx.pop(owner_id, None)

        # ======================================================
        # 4) Saiu de canal SUPORTE => apaga se vazio
        # ======================================================
        if before.channel and self._is_support_voice_channel(before.channel):
            ch = before.channel
            await asyncio.sleep(3)

            if len(ch.members) == 0:
                try:
                    await ch.delete(reason="Sala de suporte vazia")
                except Exception as e:
                    print(f"❌ Erro ao deletar canal suporte: {e}")

                self.support_voice_owner.pop(ch.id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoice(bot))