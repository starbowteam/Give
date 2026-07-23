import os
import disnake
from disnake.ext import commands
from disnake import ui
import asyncio
import sqlite3
import random
from datetime import datetime, timezone, timedelta
import re
import ast

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("❌ Ошибка: переменная окружения BOT_TOKEN не установлена.")
    exit(1)

LOG_CHANNEL_ID = 1462418981825810535

intents = disnake.Intents.default()
intents.members = True
intents.guilds = True
intents.invites = True
intents.message_content = True

bot = commands.InteractionBot(intents=intents)

MSK = timezone(timedelta(hours=3))

db = sqlite3.connect("giveaways.db", check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")

cur = db.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS giveaways (
    giveaway_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER,
    channel_id    INTEGER,
    message_id    INTEGER,
    host_id       INTEGER,
    prize         TEXT,
    description   TEXT,
    winners_count INTEGER,
    end_time      INTEGER,
    created_at    INTEGER,
    required_invites INTEGER DEFAULT 0,
    participants  TEXT,
    winners       TEXT,
    status        TEXT
);

CREATE TABLE IF NOT EXISTS invites (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    inviter_id  INTEGER,
    member_id   INTEGER,
    joined_at   INTEGER,
    is_bot      INTEGER DEFAULT 0,
    is_fake     INTEGER DEFAULT 0,
    left_at     INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS invites_snapshot (
    invite_code TEXT PRIMARY KEY,
    guild_id    INTEGER,
    uses        INTEGER,
    inviter_id  INTEGER
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS reaction_roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    channel_id  INTEGER,
    message_id  INTEGER,
    emoji       TEXT,
    role_id     INTEGER
);
""")
db.commit()

try:
    cur.execute("ALTER TABLE giveaways ADD COLUMN created_at INTEGER")
    db.commit()
except sqlite3.OperationalError:
    pass

# ================= LOGGING =================
async def log_to_channel(title: str, description: str, color: int = 0x00ff00, fields: list = None):
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if not channel:
        print(f"[WARN] Лог-канал {LOG_CHANNEL_ID} не найден")
        return
    embed = disnake.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[ERROR] Не удалось отправить лог: {e}")

# ================= UTILS =================

def now_ts():
    return int(datetime.now(timezone.utc).timestamp())

def list_from_str(data):
    if not data:
        return []
    try:
        return ast.literal_eval(data)
    except Exception:
        return []

def str_from_list(data):
    return str(data)

def parse_duration(duration_str: str):
    match = re.fullmatch(r'(\d+)\s*([mhd])', duration_str.strip().lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    if unit == 'm':
        return timedelta(minutes=value)
    if unit == 'h':
        return timedelta(hours=value)
    if unit == 'd':
        return timedelta(days=value)

def parse_emoji(emoji_str: str):
    try:
        return disnake.PartialEmoji.from_str(emoji_str)
    except Exception:
        return emoji_str

# ================= INVITE TRACKING =================

async def sync_invites(guild: disnake.Guild):
    try:
        invites = await guild.invites()
    except Exception:
        return
    for inv in invites:
        cur.execute(
            "REPLACE INTO invites_snapshot (invite_code, guild_id, uses, inviter_id) VALUES (?, ?, ?, ?)",
            (inv.code, guild.id, inv.uses, inv.inviter.id if inv.inviter else None)
        )
    db.commit()

# ================= AUTO-ROLE ON JOIN =================
@bot.event
async def on_member_join(member: disnake.Member):
    role = member.guild.get_role(1127428607606796290)
    if role:
        try:
            await member.add_roles(role)
            await log_to_channel(
                title="👤 Автороль выдана",
                description=f"Пользователь {member.mention} получил роль {role.mention}",
                color=0x00aaff
            )
        except Exception as e:
            print(f"[ERROR] Не удалось выдать роль: {e}")

    guild = member.guild
    snapshot_before = {row["invite_code"]: row for row in cur.execute("SELECT * FROM invites_snapshot WHERE guild_id=?", (guild.id,)).fetchall()}
    try:
        invites_now = await guild.invites()
    except Exception:
        return
    used_invite = None
    for inv in invites_now:
        old = snapshot_before.get(inv.code)
        if old and inv.uses > old["uses"]:
            used_invite = inv
            break
    for inv in invites_now:
        cur.execute("REPLACE INTO invites_snapshot (invite_code, guild_id, uses, inviter_id) VALUES (?, ?, ?, ?)",
                    (inv.code, guild.id, inv.uses, inv.inviter.id if inv.inviter else None))
    if not used_invite or not used_invite.inviter:
        db.commit()
        return
    inviter_id = used_invite.inviter.id
    is_bot = 1 if member.bot else 0
    joined_at = now_ts()
    cur.execute("INSERT INTO invites (guild_id, inviter_id, member_id, joined_at, is_bot) VALUES (?, ?, ?, ?, ?)",
                (guild.id, inviter_id, member.id, joined_at, is_bot))
    db.commit()

@bot.event
async def on_member_remove(member: disnake.Member):
    guild = member.guild
    cur.execute("UPDATE invites SET left_at=? WHERE guild_id=? AND member_id=? AND left_at IS NULL",
                (now_ts(), guild.id, member.id))
    row = cur.execute("SELECT joined_at FROM invites WHERE guild_id=? AND member_id=? ORDER BY joined_at DESC LIMIT 1",
                      (guild.id, member.id)).fetchone()
    if row and (now_ts() - row["joined_at"]) < 600:
        cur.execute("UPDATE invites SET is_fake=1 WHERE guild_id=? AND member_id=? AND is_fake=0",
                    (guild.id, member.id))
    db.commit()

@bot.event
async def on_invite_create(invite: disnake.Invite):
    cur.execute("REPLACE INTO invites_snapshot VALUES (?, ?, ?, ?)",
                (invite.code, invite.guild.id, invite.uses, invite.inviter.id if invite.inviter else None))
    db.commit()

@bot.event
async def on_invite_delete(invite: disnake.Invite):
    cur.execute("DELETE FROM invites_snapshot WHERE invite_code=?", (invite.code,))
    db.commit()

# ================= REACTION ROLE EVENTS =================
@bot.event
async def on_raw_reaction_add(payload: disnake.RawReactionActionEvent):
    if payload.member is None or payload.member.bot:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    row = cur.execute(
        "SELECT role_id FROM reaction_roles WHERE guild_id=? AND channel_id=? AND message_id=? AND emoji=?",
        (payload.guild_id, payload.channel_id, payload.message_id, str(payload.emoji))
    ).fetchone()
    if row:
        role = guild.get_role(row["role_id"])
        if role:
            try:
                await payload.member.add_roles(role)
                await log_to_channel(
                    title="✅ Выдана реакционная роль",
                    description=f"Пользователь {payload.member.mention} получил роль {role.mention} за реакцию {payload.emoji}",
                    color=0x00ff00
                )
            except Exception as e:
                print(f"[ERROR] Не удалось выдать роль: {e}")

@bot.event
async def on_raw_reaction_remove(payload: disnake.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return
    row = cur.execute(
        "SELECT role_id FROM reaction_roles WHERE guild_id=? AND channel_id=? AND message_id=? AND emoji=?",
        (payload.guild_id, payload.channel_id, payload.message_id, str(payload.emoji))
    ).fetchone()
    if row:
        role = guild.get_role(row["role_id"])
        if role:
            member = guild.get_member(payload.user_id)
            if member:
                try:
                    await member.remove_roles(role)
                    await log_to_channel(
                        title="❌ Снята реакционная роль",
                        description=f"Пользователь {member.mention} лишился роли {role.mention} (снял реакцию {payload.emoji})",
                        color=0xff0000
                    )
                except Exception as e:
                    print(f"[ERROR] Не удалось снять роль: {e}")

# ================= STATISTICS FUNCTIONS =================

async def get_invite_stats(guild: disnake.Guild, user: disnake.Member, giveaway_id: int = None):
    if giveaway_id is None:
        rows = cur.execute("SELECT is_bot, left_at, is_fake, member_id FROM invites WHERE guild_id=? AND inviter_id=?",
                           (guild.id, user.id)).fetchall()
    else:
        g_row = cur.execute("SELECT created_at, end_time FROM giveaways WHERE giveaway_id=? AND guild_id=?",
                            (giveaway_id, guild.id)).fetchone()
        if not g_row:
            return None
        rows = cur.execute("SELECT is_bot, left_at, is_fake, member_id FROM invites WHERE guild_id=? AND inviter_id=? AND joined_at BETWEEN ? AND ?",
                           (guild.id, user.id, g_row["created_at"], g_row["end_time"])).fetchall()
    total = len(rows)
    remaining = sum(1 for r in rows if r["left_at"] is None and r["member_id"] != 0)
    left = sum(1 for r in rows if r["left_at"] is not None)
    bots = sum(1 for r in rows if r["is_bot"] == 1)
    return {
        "total": total,
        "remaining": remaining,
        "left": left,
        "bots": bots,
    }

def build_giveaway_embeds(prize, description, winners_count, participants_count, end_dt, required_invites=0):
    end_ts = int(end_dt.timestamp())
    embed_banner = disnake.Embed(color=6776679)
    embed_banner.set_image(url="https://cdn.discordapp.com/attachments/1527006158282555412/1529678932742508674/image.png?ex=6a62d005&is=6a617e85&hm=b524115aa4edc9de6ec5aad871f9a32c7ffe37c6d45b5da1718b2803d986bd52&")
    desc_text = (f"{description}\n\n**Приз:** {prize}\n**Время окончания:** <t:{end_ts}:R>\n<t:{end_ts}:F> (МСК GMT+3)\n**Участвуют:** {participants_count}\n**Победителей:** {winners_count}")
    if required_invites:
        desc_text += f"\n**Требуется инвайтов:** {required_invites}"
    embed_main = disnake.Embed(title="🎉 Розыгрыш", description=desc_text, color=6776679)
    embed_main.set_image(url="https://cdn.discordapp.com/attachments/1223595469746475049/1459289685405728951/image_2026-01-10_00-22-10.png")
    return [embed_banner, embed_main]

def build_finished_giveaway_embed(prize, description, participants_count, winners_mentions, end_dt):
    end_ts = int(end_dt.timestamp())
    embed_banner = disnake.Embed(color=6776679)
    embed_banner.set_image(
        url="https://cdn.discordapp.com/attachments/1527006158282555412/1529712107824611369/image.png?ex=6a62eeeb&is=6a619d6b&hm=56fadbbd813b5be5ff9fdf40b0f834dde9b3f90d6ff74054f224f8d23640a530&"
    )

    desc_text = (
        f"{description}\n\n"
        f"**Приз:** {prize}\n"
        f"**Участвовали:** {participants_count}\n"
        f"**Победитель:** {winners_mentions}\n"
        f"**Закончено:** <t:{end_ts}:F>"
    )
    embed_main = disnake.Embed(
        title="🎉 Розыгрыш завершен!",
        description=desc_text,
        color=6776679
    )
    embed_main.set_image(
        url="https://cdn.discordapp.com/attachments/1223595469746475049/1459289685405728951/image_2026-01-10_00-22-10.png"
    )
    return [embed_banner, embed_main]

# ================= GIVEAWAY MODAL AND VIEWS =================

class GiveawayModal(ui.Modal):
    def __init__(self):
        components = [
            ui.TextInput(label="Приз", custom_id="prize", max_length=256),
            ui.TextInput(label="Описание", custom_id="description", style=disnake.TextInputStyle.paragraph, max_length=1024),
            ui.TextInput(label="Победителей (число)", custom_id="winners", max_length=3),
            ui.TextInput(label="Длительность (10m, 2h, 1d)", custom_id="duration", max_length=10, placeholder="Например: 30m, 2h, 7d"),
            ui.TextInput(label="Мин. инвайтов (0 или пусто = без ограничений)", custom_id="invites", required=False, max_length=10, placeholder="0"),
        ]
        super().__init__(title="Создание розыгрыша", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        prize = inter.text_values["prize"].strip()
        description = inter.text_values["description"].strip()
        try:
            winners_count = int(inter.text_values["winners"].strip())
            if winners_count < 1:
                raise ValueError
        except ValueError:
            return await inter.response.send_message("❌ Поле 'Победителей' должно быть целым числом ≥ 1.", ephemeral=True)
        duration_td = parse_duration(inter.text_values["duration"])
        if duration_td is None:
            return await inter.response.send_message("❌ Неверный формат длительности. Примеры: `30m`, `2h`, `7d`", ephemeral=True)
        invites_input = inter.text_values["invites"].strip().lower()
        if invites_input in ("0", "none", ""):
            required_invites = 0
        else:
            try:
                required_invites = int(invites_input)
                if required_invites < 0:
                    raise ValueError
            except ValueError:
                return await inter.response.send_message("❌ Поле 'Мин. инвайтов' должно быть числом ≥ 0.", ephemeral=True)
        end_dt = datetime.now(MSK) + duration_td
        end_dt_utc = end_dt.astimezone(timezone.utc)
        end_time = int(end_dt_utc.timestamp())
        created_at = now_ts()
        embeds = build_giveaway_embeds(prize, description, winners_count, 0, end_dt_utc, required_invites)
        await inter.response.send_message("⏳ Создаю розыгрыш...", ephemeral=True)
        msg = await inter.channel.send(embeds=embeds)
        cur.execute("""INSERT INTO giveaways (guild_id, channel_id, message_id, host_id, prize, description, winners_count, end_time, created_at, required_invites, participants, winners, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (inter.guild.id, inter.channel.id, msg.id, inter.author.id, prize, description, winners_count, end_time, created_at, required_invites, "[]", "[]", "active"))
        db.commit()
        gid = cur.lastrowid
        view = GiveawayView(gid)
        bot.add_view(view, message_id=msg.id)
        await msg.edit(view=view)
        asyncio.create_task(schedule_end(gid))
        await inter.edit_original_message(content=f"✅ Розыгрыш создан! **ID: `{gid}`**")

        await log_to_channel(
            title="🎉 Создан розыгрыш",
            description=f"**ID:** `{gid}`\n**Приз:** {prize}\n**Канал:** {inter.channel.mention}\n**Создал:** {inter.author.mention}\n**Завершится:** <t:{end_time}:F>",
            color=0x00ff00
        )

class GiveawayView(disnake.ui.View):
    def __init__(self, gid: int):
        super().__init__(timeout=None)
        self.add_item(JoinButton(gid))

class JoinButton(disnake.ui.Button):
    def __init__(self, gid: int):
        super().__init__(label="ㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤ🎉 Участвоватьㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤ", style=disnake.ButtonStyle.secondary, custom_id=f"giveaway_join_{gid}", row=0)
        self.gid = gid

    async def callback(self, inter: disnake.MessageInteraction):
        # Мгновенно подтверждаем, чтобы избежать таймаута
        await inter.response.defer(ephemeral=True)

        try:
            row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (self.gid,)).fetchone()
            if not row or row["status"] != "active":
                return await inter.edit_original_message(content="❌ Розыгрыш не найден или уже завершён.")

            participants = list_from_str(row["participants"])
            user_id = inter.user.id

            if user_id in participants:
                participants.remove(user_id)
                cur.execute("UPDATE giveaways SET participants=? WHERE giveaway_id=?", (str_from_list(participants), self.gid))
                db.commit()
                end_dt = datetime.fromtimestamp(row["end_time"], timezone.utc)
                embeds = build_giveaway_embeds(row["prize"], row["description"], row["winners_count"], len(participants), end_dt, row["required_invites"])
                try:
                    await inter.message.edit(embeds=embeds)
                except Exception:
                    pass
                return await inter.edit_original_message(content="❎ Ты вышел из розыгрыша.")

            participants.append(user_id)
            cur.execute("UPDATE giveaways SET participants=? WHERE giveaway_id=?", (str_from_list(participants), self.gid))
            db.commit()
            end_dt = datetime.fromtimestamp(row["end_time"], timezone.utc)
            embeds = build_giveaway_embeds(row["prize"], row["description"], row["winners_count"], len(participants), end_dt, row["required_invites"])
            try:
                await inter.message.edit(embeds=embeds)
            except Exception:
                pass
            await inter.edit_original_message(content="✅ Ты участвуешь!")
        except Exception as e:
            print(f"[ERROR] Ошибка в кнопке: {e}")
            await inter.edit_original_message(content="❌ Произошла ошибка. Попробуйте позже.")

async def schedule_end(gid: int):
    row = cur.execute("SELECT end_time FROM giveaways WHERE giveaway_id=?", (gid,)).fetchone()
    if not row:
        return
    delay = row["end_time"] - now_ts()
    if delay > 0:
        await asyncio.sleep(delay)
    await finish_giveaway(gid)

async def finish_giveaway(gid: int):
    try:
        row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (gid,)).fetchone()
        if not row or row["status"] != "active":
            return

        guild = bot.get_guild(row["guild_id"])
        if guild is None:
            print(f"[ERROR] Guild {row['guild_id']} не найден")
            await log_to_channel("❌ Ошибка завершения", f"Гильдия {row['guild_id']} не найдена", color=0xff0000)
            return

        channel = guild.get_channel(row["channel_id"]) or bot.get_channel(row["channel_id"])
        if channel is None:
            print(f"[ERROR] Канал {row['channel_id']} не найден")
            await log_to_channel("❌ Ошибка завершения", f"Канал {row['channel_id']} не найден", color=0xff0000)
            return

        participants = list_from_str(row["participants"])
        winners_count = row["winners_count"]
        required_invites = row["required_invites"]

        valid_participants = []
        if required_invites > 0:
            for uid in participants:
                member = guild.get_member(uid)
                if not member:
                    continue
                stats = await get_invite_stats(guild, member, giveaway_id=gid)
                if stats is None:
                    continue
                real_invites = stats["remaining"] - stats["bots"]
                if real_invites >= required_invites:
                    valid_participants.append(uid)
        else:
            valid_participants = participants.copy()

        pool = valid_participants.copy()
        random.shuffle(pool)
        winners = pool[:winners_count]

        cur.execute("UPDATE giveaways SET winners=?, status='finished' WHERE giveaway_id=?", (str_from_list(winners), gid))
        db.commit()

        winners_mentions = " ".join(f"<@{u}>" for u in winners) if winners else "Нет победителей 😔"

        # Удаляем оригинальное сообщение
        try:
            msg = await channel.fetch_message(row["message_id"])
            await msg.delete()
        except Exception as e:
            print(f"[WARN] Не удалось удалить сообщение: {e}")

        # Текстовое сообщение с пингом
        if winners:
            await channel.send(f"{winners_mentions} — выйграл! Напишите в течение 24 часов. После приз будет разыгран другому человеку.")
        else:
            await channel.send("😔 Победителей нет. Приз остаётся неразыгранным.")

        # Финальный embed
        end_dt = datetime.fromtimestamp(row["end_time"], timezone.utc)
        finished_embeds = build_finished_giveaway_embed(
            row["prize"],
            row["description"],
            len(participants),
            winners_mentions,
            end_dt
        )
        try:
            await channel.send(embeds=finished_embeds)
        except Exception as e:
            await channel.send(embed=finished_embeds[1])
            print(f"[ERROR] Не удалось отправить финальный embed: {e}")

        await log_to_channel(
            title="🏁 Розыгрыш завершён",
            description=f"**ID:** `{gid}`\n**Приз:** {row['prize']}\n**Победители:** {winners_mentions}",
            color=0xffaa00
        )
    except Exception as e:
        print(f"[ERROR] finish_giveaway: {e}")
        await log_to_channel("❌ Ошибка завершения розыгрыша", f"ID: {gid}\nОшибка: {e}", color=0xff0000)

# ================= COMMANDS =================

@bot.slash_command(name="giveaway", description="Создать розыгрыш", default_member_permissions=disnake.Permissions(administrator=True))
async def giveaway(inter: disnake.ApplicationCommandInteraction):
    await inter.response.send_modal(GiveawayModal())

@bot.slash_command(name="invites", description="Статистика инвайтов пользователя")
async def invites(inter: disnake.ApplicationCommandInteraction, user: disnake.Member = None, giveaway_id: int = None):
    user = user or inter.author
    if giveaway_id is not None:
        g_row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=? AND guild_id=?", (giveaway_id, inter.guild.id)).fetchone()
        if not g_row:
            return await inter.send("❌ Розыгрыш не найден.", ephemeral=True)
    stats = await get_invite_stats(inter.guild, user, giveaway_id)
    if stats is None:
        return await inter.send("❌ Не удалось получить статистику.", ephemeral=True)
    if giveaway_id:
        title = f"📨 Инвайты в розыгрыше #{giveaway_id} — {user.display_name}"
        footer = f"Период розыгрыша: <t:{g_row['created_at']}:d> – <t:{g_row['end_time']}:d>"
    else:
        title = f"📨 Инвайты — {user.display_name}"
        footer = "Статистика с текущего запуска"
    embed = disnake.Embed(
        title=title,
        color=6776679,
        description=(
            f"**На сервере:** {stats['remaining']}\n"
            f"**Ушло:** {stats['left']}\n"
            f"**Ботов:** {stats['bots']}\n"
            f"**Всего:** {stats['total']}"
        )
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=footer)
    await inter.send(embed=embed, ephemeral=True)

@bot.slash_command(name="del_invites", description="Удаление инвайтов за что-либо (сброс статистики пользователя)", default_member_permissions=disnake.Permissions(administrator=True))
async def del_invites(inter: disnake.ApplicationCommandInteraction, user: disnake.Member):
    cur.execute("DELETE FROM invites WHERE guild_id=? AND inviter_id=?", (inter.guild.id, user.id))
    db.commit()
    embed = disnake.Embed(title="✅ Статистика сброшена", description=f"Все данные по инвайтам для {user.mention} удалены.", color=0x00ff00)
    await inter.send(embed=embed, ephemeral=True)
    await log_to_channel(
        title="🗑️ Сброс инвайтов",
        description=f"Админ {inter.author.mention} сбросил статистику инвайтов для {user.mention}",
        color=0xff6600
    )

@bot.slash_command(
    name="reactionrole",
    description="Управление реакционными ролями",
    default_member_permissions=disnake.Permissions(administrator=True)
)
async def reactionrole(inter: disnake.ApplicationCommandInteraction):
    pass

@reactionrole.sub_command(name="add", description="Добавить реакционную роль")
async def reactionrole_add(
    inter: disnake.ApplicationCommandInteraction,
    message_id: str = commands.Param(description="ID сообщения (можно скопировать в Discord)"),
    emoji: str = commands.Param(description="Эмодзи (например, ✅ или <:имя:ID>)"),
    role: disnake.Role = commands.Param(description="Роль, которая будет выдаваться")
):
    try:
        msg = await inter.channel.fetch_message(int(message_id))
    except Exception:
        return await inter.send("❌ Сообщение не найдено в этом канале.", ephemeral=True)

    cur.execute(
        "INSERT INTO reaction_roles (guild_id, channel_id, message_id, emoji, role_id) VALUES (?, ?, ?, ?, ?)",
        (inter.guild.id, inter.channel.id, int(message_id), emoji, role.id)
    )
    db.commit()

    try:
        await msg.add_reaction(parse_emoji(emoji))
    except Exception as e:
        await inter.send(f"⚠️ Не удалось поставить реакцию: {e}", ephemeral=True)
        return

    embed = disnake.Embed(
        title="✅ Реакционная роль добавлена",
        description=f"На сообщение {msg.jump_url} добавлена реакция {emoji}, выдающая роль {role.mention}.",
        color=0x00ff00
    )
    await inter.send(embed=embed, ephemeral=True)

    await log_to_channel(
        title="➕ Добавлена реакционная роль",
        description=f"Админ {inter.author.mention} создал реакцию {emoji} → {role.mention} на [сообщение]({msg.jump_url})",
        color=0x00aaff
    )

@bot.slash_command(
    name="reroll",
    description="Выбрать нового победителя",
    default_member_permissions=disnake.Permissions(administrator=True)
)
async def reroll(inter: disnake.ApplicationCommandInteraction, giveaway_id: int):
    row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (giveaway_id,)).fetchone()
    if not row:
        return await inter.response.send_message("❌ Розыгрыш не найден.", ephemeral=True)
    if row["status"] == "active":
        return await inter.response.send_message("❌ Розыгрыш ещё не завершён.", ephemeral=True)
    guild = bot.get_guild(row["guild_id"])
    channel = guild.get_channel(row["channel_id"]) if guild else None
    participants = list_from_str(row["participants"])
    old_winners = list_from_str(row["winners"])
    pool = [u for u in participants if u not in old_winners]
    if not pool:
        return await inter.response.send_message("❌ Нет участников для reroll.", ephemeral=True)
    new_winner = random.choice(pool)
    old_winners.append(new_winner)
    cur.execute("UPDATE giveaways SET winners=? WHERE giveaway_id=?", (str_from_list(old_winners), giveaway_id))
    db.commit()
    embed = disnake.Embed(title="🎉 Новый победитель (Reroll)", description=f"<@{new_winner}>", color=0x676767)
    try:
        msg = await channel.fetch_message(row["message_id"])
        await msg.reply(embed=embed, mention_author=False)
    except Exception:
        if channel:
            await channel.send(embed=embed)
    await inter.response.send_message("✅ Reroll выполнен.", ephemeral=True)

    await log_to_channel(
        title="🔄 Reroll выполнен",
        description=f"Для розыгрыша **#{giveaway_id}** (`{row['prize']}`) выбран новый победитель: <@{new_winner}>",
        color=0xff9900
    )

@bot.slash_command(
    name="end_giveaway",
    description="Принудительно завершить розыгрыш",
    default_member_permissions=disnake.Permissions(administrator=True)
)
async def end_giveaway(inter: disnake.ApplicationCommandInteraction, giveaway_id: int):
    row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=? AND status='active'", (giveaway_id,)).fetchone()
    if not row:
        return await inter.response.send_message("❌ Активный розыгрыш не найден.", ephemeral=True)
    await inter.response.send_message("⏳ Завершаю...", ephemeral=True)
    await finish_giveaway(giveaway_id)
    await inter.edit_original_message(content="✅ Розыгрыш завершён.")

@bot.slash_command(
    name="list_giveaways",
    description="Список розыгрышей",
    default_member_permissions=disnake.Permissions(administrator=True)
)
async def list_giveaways(
    inter: disnake.ApplicationCommandInteraction,
    статус: str = commands.Param(default="все", choices=["все", "активные", "завершённые"])
):
    if статус == "активные":
        rows = cur.execute("SELECT * FROM giveaways WHERE guild_id=? AND status='active' ORDER BY giveaway_id DESC", (inter.guild.id,)).fetchall()
        title = "🟢 Активные розыгрыши"
    elif статус == "завершённые":
        rows = cur.execute("SELECT * FROM giveaways WHERE guild_id=? AND status='finished' ORDER BY giveaway_id DESC", (inter.guild.id,)).fetchall()
        title = "🔴 Завершённые розыгрыши"
    else:
        rows = cur.execute("SELECT * FROM giveaways WHERE guild_id=? ORDER BY giveaway_id DESC", (inter.guild.id,)).fetchall()
        title = "📋 Все розыгрыши"
    if not rows:
        return await inter.send("Розыгрышей не найдено.", ephemeral=True)
    lines = []
    for r in rows:
        status_icon = "🟢" if r["status"] == "active" else "🔴"
        participants_count = len(list_from_str(r["participants"]))
        winners = list_from_str(r["winners"])
        if r["status"] == "active":
            time_str = f"Конец: <t:{r['end_time']}:R>"
        else:
            time_str = f"Закончился: <t:{r['end_time']}:d>"
        winners_str = ""
        if r["status"] == "finished":
            if winners:
                winners_str = " | Победители: " + ", ".join(f"<@{w}>" for w in winners)
            else:
                winners_str = " | Победителей нет"
        lines.append(f"{status_icon} **ID {r['giveaway_id']}** — {r['prize']}\n┗ Участников: **{participants_count}** | {time_str}{winners_str}")
    embed = disnake.Embed(title=title, description="\n\n".join(lines), color=6776679)
    embed.set_footer(text=f"Всего: {len(rows)}")
    await inter.send(embed=embed, ephemeral=True)

# ================= ON_READY =================

@bot.event
async def on_ready():
    await bot.change_presence(status=disnake.Status.online, activity=disnake.Game("Призы и инвайты"))

    for guild in bot.guilds:
        await sync_invites(guild)

    # Восстанавливаем активные розыгрыши
    active_rows = cur.execute("SELECT giveaway_id, message_id FROM giveaways WHERE status='active'").fetchall()
    for r in active_rows:
        gid = r["giveaway_id"]
        msg_id = r["message_id"]
        view = GiveawayView(gid)
        bot.add_view(view, message_id=msg_id)
        asyncio.create_task(schedule_end(gid))

    await log_to_channel(
        "✅ Бот запущен",
        f"**{bot.user}** готов к работе.\nАктивных розыгрышей: {len(active_rows)}",
        color=0x00ff00
    )
    print(f"✅ Bot ready as {bot.user} | Активных розыгрышей: {len(active_rows)}")

# ================= RUN =================

bot.run(TOKEN)
