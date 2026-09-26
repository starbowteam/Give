# -*- coding: utf-8 -*-
import os
import time
import disnake
from disnake.ext import commands
from disnake import ui
import asyncio
import sqlite3
import random
from datetime import datetime, timezone, timedelta
import re
import ast
import io

# ============================================================
# КОНФИГ
# ============================================================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("❌ Ошибка: переменная окружения BOT_TOKEN не установлена.")
    exit(1)

LOG_CHANNEL_ID          = 1530453871581855744
GIVEAWAY_CHANNEL_ID     = 1462390938851741923
STAFF_PANEL_CHANNEL_ID  = 1551276116679860314
REPORT_DM_USER_ID       = 796293832751972352

GIVEAWAY_FULL_ROLES = [
    1530823425764098058,
    1530822331188903966,
    1127428607606796294,
    1471844291595731016,
]

# ============================================================
# ШАБЛОНЫ РОЗЫГРЫШЕЙ
# ============================================================
GIVEAWAY_TEMPLATES = {
    "150": {
        "prize": "150 DC",
        "winners": 1,
        "description": "🎉 Стандартный розыгрыш 150 Diamond Coins на 1 победителя!",
        "label": "150 DC · 1 победитель",
    },
    "400": {
        "prize": "400 DC",
        "winners": 2,
        "description": "🎉 Розыгрыш 400 Diamond Coins на 2 победителей!",
        "label": "400 DC · 2 победителя",
    },
    "600": {
        "prize": "600 DC",
        "winners": 3,
        "description": "🎉 Розыгрыш 600 Diamond Coins на 3 победителей!",
        "label": "600 DC · 3 победителя",
    },
    "1000": {
        "prize": "1000 DC",
        "winners": 5,
        "description": "👑 Большой розыгрыш 1000 Diamond Coins на 5 победителей!",
        "label": "1000 DC · 5 победителей",
    },
}

intents = disnake.Intents.default()
intents.members = True
intents.guilds = True
intents.invites = True
intents.message_content = True

bot = commands.InteractionBot(intents=intents)
MSK = timezone(timedelta(hours=3))

# ============================================================
# БД
# ============================================================
db = sqlite3.connect("giveaways.db", check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")

cur = db.cursor()
cur.executescript("""
CREATE TABLE IF NOT EXISTS giveaways (
    giveaway_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER,
    channel_id         INTEGER,
    message_id         INTEGER,
    host_id            INTEGER,
    prize              TEXT,
    description        TEXT,
    winners_count      INTEGER,
    end_time           INTEGER,
    created_at         INTEGER,
    required_invites   INTEGER DEFAULT 0,
    participants       TEXT,
    winners            TEXT,
    status             TEXT,
    valid_participants TEXT,
    final_embed_id     INTEGER
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
""")
db.commit()

# ============================================================
# LOGGING
# ============================================================
async def log_discord(title: str, description: str, color: int = 0x00ff00, fields: list = None):
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if not channel:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        if not channel:
            return
        embed = disnake.Embed(title=title, description=description, color=color, timestamp=datetime.now(timezone.utc))
        if fields:
            for name, value, inline in fields:
                embed.add_field(name=name, value=value, inline=inline)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[ERROR] Не удалось отправить лог: {e}")

# ============================================================
# UTILS
# ============================================================
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

def is_giveaway_staff(member: disnake.Member) -> bool:
    return any(r.id in GIVEAWAY_FULL_ROLES for r in member.roles)

# ============================================================
# ИНВАЙТЫ
# ============================================================
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

@bot.event
async def on_member_join(member: disnake.Member):
    guild = member.guild
    snapshot_before = {
        row["invite_code"]: row for row in
        cur.execute("SELECT * FROM invites_snapshot WHERE guild_id=?", (guild.id,)).fetchall()
    }
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
        cur.execute(
            "REPLACE INTO invites_snapshot (invite_code, guild_id, uses, inviter_id) VALUES (?, ?, ?, ?)",
            (inv.code, guild.id, inv.uses, inv.inviter.id if inv.inviter else None)
        )
    if not used_invite or not used_invite.inviter:
        db.commit()
        return
    inviter_id = used_invite.inviter.id
    is_bot = 1 if member.bot else 0
    joined_at = now_ts()
    cur.execute(
        "INSERT INTO invites (guild_id, inviter_id, member_id, joined_at, is_bot) VALUES (?, ?, ?, ?, ?)",
        (guild.id, inviter_id, member.id, joined_at, is_bot)
    )
    db.commit()
    await log_discord(
        title="📨 Использован инвайт",
        description=f"> **Пользователь:** {member.mention}\n> **Пригласил:** <@{inviter_id}>\n> **Код:** `{used_invite.code}`",
        color=0x00aaff
    )

@bot.event
async def on_member_remove(member: disnake.Member):
    guild = member.guild
    cur.execute(
        "UPDATE invites SET left_at=? WHERE guild_id=? AND member_id=? AND left_at IS NULL",
        (now_ts(), guild.id, member.id)
    )
    row = cur.execute(
        "SELECT joined_at FROM invites WHERE guild_id=? AND member_id=? ORDER BY joined_at DESC LIMIT 1",
        (guild.id, member.id)
    ).fetchone()
    if row and (now_ts() - row["joined_at"]) < 600:
        cur.execute(
            "UPDATE invites SET is_fake=1 WHERE guild_id=? AND member_id=? AND is_fake=0",
            (guild.id, member.id)
        )
        await log_discord(
            title="⚠️ Фейковый вход",
            description=f"> **Пользователь:** {member.mention}\n> Ушёл менее чем через 10 минут.",
            color=0xff6600
        )
    db.commit()

@bot.event
async def on_invite_create(invite: disnake.Invite):
    cur.execute(
        "REPLACE INTO invites_snapshot VALUES (?, ?, ?, ?)",
        (invite.code, invite.guild.id, invite.uses, invite.inviter.id if invite.inviter else None)
    )
    db.commit()

@bot.event
async def on_invite_delete(invite: disnake.Invite):
    cur.execute("DELETE FROM invites_snapshot WHERE invite_code=?", (invite.code,))
    db.commit()

# ============================================================
# СТАТИСТИКА ИНВАЙТОВ
# ============================================================
async def get_invite_stats(guild: disnake.Guild, user: disnake.Member, giveaway_id: int = None):
    if giveaway_id is not None:
        g_row = cur.execute(
            "SELECT created_at, end_time FROM giveaways WHERE giveaway_id=? AND guild_id=?",
            (giveaway_id, guild.id)
        ).fetchone()
        if not g_row:
            return None
        now = now_ts()
        end_time = min(g_row["end_time"], now)
        rows = cur.execute(
            "SELECT is_bot, left_at, is_fake, member_id FROM invites "
            "WHERE guild_id=? AND inviter_id=? AND joined_at BETWEEN ? AND ?",
            (guild.id, user.id, g_row["created_at"], end_time)
        ).fetchall()
        total = len(rows)
        remaining = sum(1 for r in rows if r["left_at"] is None and r["member_id"] != 0)
        left = sum(1 for r in rows if r["left_at"] is not None)
        bots = sum(1 for r in rows if r["is_bot"] == 1)
        return {"total": total, "remaining": remaining, "left": left, "bots": bots}
    else:
        try:
            invites = await guild.invites()
            total_uses = 0
            for inv in invites:
                if inv.inviter and inv.inviter.id == user.id:
                    total_uses += inv.uses
            return {"total": total_uses, "remaining": total_uses, "left": 0, "bots": 0}
        except Exception:
            return None

# ============================================================
# EMBED BUILDERS (> формат)
# ============================================================
IMG_BANNER_GW = "https://cdn.discordapp.com/attachments/1527006158282555412/1529678932742508674/image.png?ex=6a62d005&is=6a617e85&hm=b524115aa4edc9de6ec5aad871f9a32c7ffe37c6d45b5da1718b2803d986bd52&"
IMG_STRIPE_GW = "https://cdn.discordapp.com/attachments/1527006158282555412/1530795801268453447/pisk.png?ex=6a66e02f&is=6a658eaf&hm=79e41273327d2e1048ba42df62868cbb88f5b06b112ca864de2c7a02326523e9&"
IMG_BANNER_FIN = "https://cdn.discordapp.com/attachments/1462418981825810535/1529721309880258660/image.png?ex=6a62f77d&is=6a61a5fd&hm=195de5a268f76548d304db161adc041513e051f0938fcb62588ccd6801e374b2&"

def build_giveaway_embeds(prize, description, winners_count, participants_count, end_dt, required_invites=0):
    end_ts = int(end_dt.timestamp())
    embed_banner = disnake.Embed(color=6776679)
    embed_banner.set_image(url=IMG_BANNER_GW)
    desc_text = (
        f"> {description}\n\n"
        f"> **Приз:** {prize}\n"
        f"> **Время окончания:** <t:{end_ts}:R>\n"
        f"> <t:{end_ts}:F> (МСК GMT+3)\n"
        f"> **Участвуют:** {participants_count}\n"
        f"> **Победителей:** {winners_count}"
    )
    if required_invites:
        desc_text += f"\n> **Требуется инвайтов:** {required_invites}"
    embed_main = disnake.Embed(title="🎉 Розыгрыш", description=desc_text, color=6776679)
    embed_main.set_image(url=IMG_STRIPE_GW)
    return [embed_banner, embed_main]

def build_finished_giveaway_embed(prize, description, participants_count, winners_mentions, end_dt):
    end_ts = int(end_dt.timestamp())
    embed_banner = disnake.Embed(color=6776679)
    embed_banner.set_image(url=IMG_BANNER_FIN)
    desc_text = (
        f"> {description}\n\n"
        f"> **Приз:** {prize}\n"
        f"> **Участвовали:** {participants_count}\n"
        f"> **Победители:** {winners_mentions}\n"
        f"> **Закончено:** <t:{end_ts}:F>"
    )
    embed_main = disnake.Embed(title="🎉 Розыгрыш завершён!", description=desc_text, color=6776679)
    embed_main.set_image(url=IMG_STRIPE_GW)
    return [embed_banner, embed_main]

# ============================================================
# МОДАЛКА СОЗДАНИЯ
# ============================================================
class GiveawayModal(ui.Modal):
    def __init__(self):
        components = [
            ui.TextInput(label="Приз", custom_id="prize", max_length=256),
            ui.TextInput(label="Описание", custom_id="description",
                         style=disnake.TextInputStyle.paragraph, max_length=1024),
            ui.TextInput(label="Победителей (число)", custom_id="winners", max_length=3),
            ui.TextInput(label="Длительность (10m, 2h, 1d)", custom_id="duration",
                         max_length=10, placeholder="Например: 30m, 2h, 7d"),
            ui.TextInput(label="Мин. инвайтов (0 или пусто = без ограничений)",
                         custom_id="invites", required=False, max_length=10, placeholder="0"),
        ]
        super().__init__(title="Создание розыгрыша", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_giveaway_staff(inter.author):
            return await inter.response.send_message("⛔ У вас нет прав на создание розыгрышей.", ephemeral=True)

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

        channel = bot.get_channel(GIVEAWAY_CHANNEL_ID)
        if not channel:
            return await inter.edit_original_message(content="❌ Канал для розыгрышей не найден.")

        msg = await channel.send(embeds=embeds)
        cur.execute(
            """INSERT INTO giveaways
               (guild_id, channel_id, message_id, host_id, prize, description, winners_count,
                end_time, created_at, required_invites, participants, winners, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (inter.guild.id, channel.id, msg.id, inter.author.id, prize, description, winners_count,
             end_time, created_at, required_invites, "[]", "[]", "active")
        )
        db.commit()
        gid = cur.lastrowid
        view = GiveawayView(gid)
        bot.add_view(view, message_id=msg.id)
        await msg.edit(view=view)
        asyncio.create_task(schedule_end(gid))
        await inter.edit_original_message(content=f"✅ Розыгрыш создан! **ID: `{gid}`**")
        await log_discord(
            title="🎉 Создан розыгрыш",
            description=(
                f"> **ID:** `{gid}`\n"
                f"> **Приз:** {prize}\n"
                f"> **Канал:** {channel.mention}\n"
                f"> **Создал:** {inter.author.mention}\n"
                f"> **Завершится:** <t:{end_time}:F>"
            ),
            color=0x00ff00
        )

# ============================================================
# VIEW РОЗЫГРЫША
# ============================================================
class GiveawayView(disnake.ui.View):
    def __init__(self, gid: int):
        super().__init__(timeout=None)
        self.gid = gid
        self.add_item(JoinButton(gid))

class JoinButton(disnake.ui.Button):
    def __init__(self, gid: int):
        super().__init__(
            label="ㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤ🎉 Участвоватьㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤㅤ",
            style=disnake.ButtonStyle.secondary,
            custom_id=f"giveaway_join_{gid}",
            row=0
        )
        self.gid = gid

    async def callback(self, inter: disnake.MessageInteraction):
        try:
            row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (self.gid,)).fetchone()
            if not row or row["status"] != "active":
                await inter.response.send_message("❌ Розыгрыш не найден или уже завершён.", ephemeral=True)
                return

            participants = list_from_str(row["participants"])
            user_id = inter.user.id

            if user_id in participants:
                await inter.response.send_message("🎊 Вы уже участвуете в розыгрыше!", ephemeral=True)
                return

            participants.append(user_id)
            cur.execute("UPDATE giveaways SET participants=? WHERE giveaway_id=?",
                        (str_from_list(participants), self.gid))
            db.commit()

            end_dt = datetime.fromtimestamp(row["end_time"], timezone.utc)
            embeds = build_giveaway_embeds(
                row["prize"],
                row["description"],
                row["winners_count"],
                len(participants),
                end_dt,
                row["required_invites"]
            )
            await inter.message.edit(embeds=embeds)

            await inter.response.send_message("✅ Ты успешно присоединился к розыгрышу!", ephemeral=True)

            embed_dm = disnake.Embed(
                title="✅ Ты участвуешь в розыгрыше!",
                description=(
                    f"> **Приз:** {row['prize']}\n"
                    f"> **ID розыгрыша:** `{self.gid}`\n"
                    f"> **Участников:** {len(participants)}"
                ),
                color=0x00ff00
            )
            embed_dm.set_footer(text="Удачи! 🍀")
            try:
                await inter.user.send(embed=embed_dm)
            except Exception:
                pass
        except Exception as e:
            print(f"[ERROR] Ошибка в кнопке: {e}")
            await inter.response.send_message("❌ Произошла ошибка. Попробуйте позже.", ephemeral=True)

# ============================================================
# ВАЛИДНЫЕ УЧАСТНИКИ
# ============================================================
async def get_valid_participants(gid: int, guild: disnake.Guild):
    row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (gid,)).fetchone()
    if not row:
        return []

    participants = list_from_str(row["participants"])
    required_invites = row["required_invites"]

    valid = []
    for uid in participants:
        member = guild.get_member(uid)
        if not member:
            continue
        if member.bot:
            continue
        if required_invites > 0:
            stats = await get_invite_stats(guild, member, giveaway_id=gid)
            if stats is None:
                continue
            if stats["total"] < required_invites:
                continue
        valid.append(uid)

    return list(set(valid))

# ============================================================
# ПЛАНИРОВЩИК
# ============================================================
async def schedule_end(gid: int):
    row = cur.execute("SELECT end_time FROM giveaways WHERE giveaway_id=?", (gid,)).fetchone()
    if not row:
        return
    delay = row["end_time"] - now_ts()
    if delay > 0:
        await asyncio.sleep(delay)
    await finish_giveaway(gid)

# ============================================================
# ЗАВЕРШЕНИЕ
# ============================================================
async def finish_giveaway(gid: int):
    try:
        row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (gid,)).fetchone()
        if not row or row["status"] != "active":
            return

        guild = bot.get_guild(row["guild_id"])
        if not guild:
            await log_discord("❌ Ошибка завершения", f"Гильдия {row['guild_id']} не найдена", color=0xff0000)
            return

        channel = guild.get_channel(row["channel_id"]) or bot.get_channel(row["channel_id"])
        if not channel:
            await log_discord("❌ Ошибка завершения", f"Канал {row['channel_id']} не найден", color=0xff0000)
            return

        valid_participants = await get_valid_participants(gid, guild)
        participants = list_from_str(row["participants"])
        winners_count = row["winners_count"]

        pool = valid_participants.copy()
        random.shuffle(pool)
        winners = pool[:winners_count] if pool else []

        cur.execute(
            "UPDATE giveaways SET winners=?, status='finished', valid_participants=? WHERE giveaway_id=?",
            (str_from_list(winners), str_from_list(valid_participants), gid)
        )
        db.commit()

        winners_mentions = " ".join(f"<@{u}>" for u in winners) if winners else "Нет победителей 😔"

        try:
            msg = await channel.fetch_message(row["message_id"])
            await msg.delete()
        except Exception as e:
            print(f"[WARN] Не удалось удалить сообщение: {e}")

        end_dt = datetime.fromtimestamp(row["end_time"], timezone.utc)
        finished_embeds = build_finished_giveaway_embed(
            row["prize"], row["description"], len(participants), winners_mentions, end_dt
        )
        embed_msg = await channel.send(embeds=finished_embeds)

        cur.execute("UPDATE giveaways SET final_embed_id=? WHERE giveaway_id=?", (embed_msg.id, gid))
        db.commit()

        # ============================================================
        # ОТЧЁТ В ЛС — txt с УЧАСТНИКАМИ + красивый эмбед
        # ============================================================
        await send_giveaway_report(
            gid=gid,
            prize=row["prize"],
            description=row["description"] or "—",
            participants=participants,
            winners=winners,
            host_id=row["host_id"],
        )

        # ============================================================
        # ЛС УЧАСТНИКАМ
        # ============================================================
        prize_name = row["prize"]
        winner_ids = set(winners)
        for uid in participants:
            member = guild.get_member(uid)
            if not member:
                continue
            if uid in winner_ids:
                embed_dm = disnake.Embed(
                    title="🎉 ТЫ ВЫИГРАЛ!",
                    description=f"> **Приз:** {prize_name}\n> **ID розыгрыша:** `{gid}`",
                    color=0xffaa00
                )
                embed_dm.add_field(
                    name="Что делать?",
                    value="> В течение 24 часов отпишись в личные сообщения <@796293832751972352>, иначе приз будет разыгран другому участнику.",
                    inline=False
                )
                embed_dm.set_footer(text="Поздравляем! 🏆")
            else:
                embed_dm = disnake.Embed(
                    title="🎉 Розыгрыш завершён!",
                    description=f"> **Приз:** {prize_name}\n> **ID розыгрыша:** `{gid}`",
                    color=0x808080
                )
                embed_dm.add_field(
                    name="Результат",
                    value="> Ты не занял призовое место. Постарайся в следующий раз! 🍀",
                    inline=False
                )
                embed_dm.set_footer(text="Спасибо за участие!")
            try:
                await member.send(embed=embed_dm)
            except Exception:
                pass

        await log_discord(
            title="🏁 Розыгрыш завершён",
            description=f"> **ID:** `{gid}`\n> **Приз:** {row['prize']}\n> **Победители:** {winners_mentions}",
            color=0xffaa00
        )
    except Exception as e:
        print(f"[ERROR] finish_giveaway: {e}")
        await log_discord("❌ Ошибка завершения розыгрыша", f"ID: {gid}\nОшибка: {e}", color=0xff0000)

# ============================================================
# ОТЧЁТ (txt + эмбед) В ЛС
# ============================================================
async def send_giveaway_report(gid: int, prize: str, description: str,
                                participants: list, winners: list, host_id: int):
    try:
        user = bot.get_user(REPORT_DM_USER_ID)
        if not user:
            user = await bot.fetch_user(REPORT_DM_USER_ID)
        if not user:
            return

        # Собираем txt: заголовок + участники
        lines = []
        lines.append(f"=== ОТЧЁТ ПО РОЗЫГРЫШУ #{gid} ===")
        lines.append(f"Приз: {prize}")
        lines.append(f"Организатор ID: {host_id}")
        lines.append(f"Всего участников: {len(participants)}")
        lines.append(f"Победителей: {len(winners)}")
        lines.append("")
        lines.append("=== ПОБЕДИТЕЛИ ===")
        if winners:
            for i, uid in enumerate(winners, 1):
                lines.append(f"{i}. {uid}")
        else:
            lines.append("нет")
        lines.append("")
        lines.append("=== ВСЕ УЧАСТНИКИ ===")
        if participants:
            for i, uid in enumerate(participants, 1):
                lines.append(f"{i}. {uid}")
        else:
            lines.append("нет")

        content = "\n".join(lines)
        file_data = io.BytesIO(content.encode("utf-8"))
        file = disnake.File(file_data, filename=f"report_giveaway_{gid}.txt")

        # Красивый эмбед
        winners_mentions = " ".join(f"<@{u}>" for u in winners) if winners else "—"
        e1 = disnake.Embed(color=6776679)
        e1.set_image(url=IMG_STRIPE_GW)
        e2 = disnake.Embed(
            title=f"📊 Отчёт по розыгрышу #{gid}",
            description=(
                f"> **Приз:** {prize}\n"
                f"> **Организатор:** <@{host_id}>\n"
                f"> **Участников:** `{len(participants)}`\n"
                f"> **Победителей:** `{len(winners)}`\n\n"
                f"> **Победители:**\n> {winners_mentions}\n\n"
                f"> 📎 Файл со всеми ID — в вложении."
            ),
            color=6776679
        )
        e2.set_image(url=IMG_STRIPE_GW)

        await user.send(embeds=[e1, e2], file=file)
        print(f"[REPORT] Отчёт по розыгрышу #{gid} отправлен {REPORT_DM_USER_ID}")
    except Exception as e:
        print(f"[ERROR] send_giveaway_report: {e}")

# ============================================================
# РЕРОЛЛ
# ============================================================
async def reroll_giveaway(inter, gid: int):
    row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=?", (gid,)).fetchone()
    if not row:
        return await inter.edit_original_message(content="❌ Розыгрыш не найден.")
    if row["status"] == "active":
        return await inter.edit_original_message(content="❌ Розыгрыш ещё не завершён.")

    guild = bot.get_guild(row["guild_id"])
    if not guild:
        return await inter.edit_original_message(content="❌ Сервер не найден.")
    channel = guild.get_channel(row["channel_id"]) or bot.get_channel(row["channel_id"])
    if not channel:
        return await inter.edit_original_message(content="❌ Канал не найден.")

    valid_participants = await get_valid_participants(gid, guild)
    if not valid_participants:
        return await inter.edit_original_message(content="❌ Нет валидных участников для перевыбора.")

    old_winners = list_from_str(row["winners"])
    pool = [u for u in valid_participants if u not in old_winners]

    if not pool:
        return await inter.edit_original_message(content="❌ Нет участников для перевыбора (все валидные уже выиграли).")

    winners_count = row["winners_count"]
    random.shuffle(pool)
    new_winners = pool[:winners_count] if pool else []

    cur.execute("UPDATE giveaways SET winners=? WHERE giveaway_id=?", (str_from_list(new_winners), gid))
    db.commit()

    try:
        if row["final_embed_id"]:
            msg = await channel.fetch_message(row["final_embed_id"])
            await msg.delete()
    except Exception:
        pass

    winners_mentions = " ".join(f"<@{u}>" for u in new_winners) if new_winners else "Нет победителей 😔"
    prize = row["prize"]
    description = row["description"] or ""

    end_dt = datetime.fromtimestamp(row["end_time"], timezone.utc)
    finished_embeds = build_finished_giveaway_embed(
        prize, description, len(list_from_str(row["participants"])), winners_mentions, end_dt
    )
    embed_msg = await channel.send(embeds=finished_embeds)

    cur.execute("UPDATE giveaways SET final_embed_id=? WHERE giveaway_id=?", (embed_msg.id, gid))
    db.commit()

    # Отчёт по рероллу тоже в ЛС
    await send_giveaway_report(
        gid=gid,
        prize=prize,
        description=description,
        participants=list_from_str(row["participants"]),
        winners=new_winners,
        host_id=row["host_id"],
    )

    for uid in new_winners:
        member = guild.get_member(uid)
        if member:
            embed_dm = disnake.Embed(
                title="🎉 Перевыбор победителя!",
                description=f"> **Приз:** {prize}\n> **ID розыгрыша:** `{gid}`",
                color=0xff9900
            )
            embed_dm.add_field(
                name="Что делать?",
                value="> В течение 24 часов отпишись в личные сообщения <@796293832751972352>, иначе приз будет разыгран другому участнику.",
                inline=False
            )
            embed_dm.set_footer(text="Поздравляем! 🏆")
            try:
                await member.send(embed=embed_dm)
            except Exception:
                pass

    await inter.edit_original_message(content=f"✅ Перевыбор выполнен для розыгрыша #{gid}")
    await log_discord(
        title="🔄 Перевыбор победителя",
        description=f"> **Розыгрыш #**`{gid}` (приз: {row['prize']})\n> **Новые победители:** {winners_mentions}",
        color=0xff9900
    )

# ============================================================
# МОДАЛКИ ЗАВЕРШЕНИЯ / РЕРОЛЛА
# ============================================================
class EndGiveawayModal(ui.Modal):
    def __init__(self):
        components = [
            ui.TextInput(label="ID розыгрыша", custom_id="giveaway_id", max_length=10, placeholder="Введите числовой ID")
        ]
        super().__init__(title="Принудительное завершение", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_giveaway_staff(inter.author):
            return await inter.response.send_message("⛔ У вас нет прав.", ephemeral=True)
        try:
            gid = int(inter.text_values["giveaway_id"].strip())
        except ValueError:
            return await inter.response.send_message("❌ ID должен быть числом.", ephemeral=True)

        row = cur.execute("SELECT * FROM giveaways WHERE giveaway_id=? AND status='active'", (gid,)).fetchone()
        if not row:
            return await inter.response.send_message("❌ Активный розыгрыш с таким ID не найден.", ephemeral=True)

        await inter.response.send_message("⏳ Завершаю...", ephemeral=True)
        await finish_giveaway(gid)
        await inter.edit_original_message(content=f"✅ Розыгрыш #{gid} завершён.")

class RerollModal(ui.Modal):
    def __init__(self):
        components = [
            ui.TextInput(label="ID розыгрыша", custom_id="giveaway_id", max_length=10, placeholder="Введите числовой ID")
        ]
        super().__init__(title="Перевыбор победителя", components=components)

    async def callback(self, inter: disnake.ModalInteraction):
        if not is_giveaway_staff(inter.author):
            return await inter.response.send_message("⛔ У вас нет прав.", ephemeral=True)
        try:
            gid = int(inter.text_values["giveaway_id"].strip())
        except ValueError:
            return await inter.response.send_message("❌ ID должен быть числом.", ephemeral=True)

        await inter.response.defer(ephemeral=True)
        await reroll_giveaway(inter, gid)

# ============================================================
# ПАНЕЛЬ: СЕЛЕКТ
# ============================================================
class GiveawaySelect(disnake.ui.StringSelect):
    def __init__(self):
        options = [
            disnake.SelectOption(
                label="・Начать розыгрыш",
                description="Создать розыгрыш вручную",
                emoji="<:__:1538399607699021895>",
                value="create"
            ),
            disnake.SelectOption(
                label="・Шаблонные розыгрыши",
                description="Готовые шаблоны DC-розыгрышей",
                emoji="<:cakleb:1553236134316875846>",
                value="templates"
            ),
            disnake.SelectOption(
                label="・Лист розыгрышей",
                description="Все существующие розыгрыши",
                emoji="<:banne1:1538551829246513312>",
                value="list"
            ),
            disnake.SelectOption(
                label="・Завершить принудительно",
                description="Принудительно завершить розыгрыш",
                emoji="<:clear:1538561439491686410>",
                value="end"
            ),
            disnake.SelectOption(
                label="・Перевыбрать победителя",
                description="Перевыбрать победителя",
                emoji="<:restart:1553231421190049813>",
                value="reroll"
            ),
        ]
        super().__init__(
            placeholder="Выберите нужный пункт",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="giveaway_select"
        )

    async def callback(self, inter: disnake.MessageInteraction):
        if not is_giveaway_staff(inter.author):
            return await inter.response.send_message("⛔ У вас нет прав.", ephemeral=True)

        await log_discord(
            title="🎮 Выбор в панели розыгрышей",
            description=f"> **Пользователь:** {inter.author.mention}\n> **Выбрано:** `{inter.data.values[0]}`",
            color=0x00aaff
        )

        value = inter.data.values[0]
        if value == "create":
            await inter.response.send_modal(GiveawayModal())
        elif value == "templates":
            await show_templates_panel(inter)
        elif value == "list":
            await inter.response.defer(ephemeral=True)
            await list_giveaways(inter)
        elif value == "end":
            await inter.response.send_modal(EndGiveawayModal())
        elif value == "reroll":
            await inter.response.send_modal(RerollModal())

class GiveawayPanelView(disnake.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(GiveawaySelect())

# ============================================================
# ПАНЕЛЬ: ШАБЛОНЫ
# ============================================================
async def show_templates_panel(inter: disnake.MessageInteraction):
    e1 = disnake.Embed(color=6776679)
    e1.set_image(url="https://cdn.discordapp.com/attachments/1527006158282555412/1539649602435682344/image.png?ex=6ab7dd6e&is=6ab68bee&hm=b98fbd2809973d1c63315659a842d8b87e3cab4d276bfc9c6079b27c87130f4d&")
    e2 = disnake.Embed(
        title="Шаблонные розыгрыши",
        description=(
            "> Выберите тип ниже, розыгрыш начнёт своё существование "
            "после того, как вы нажмёте нужную кнопку.\n\n"
            "> ⏱ **Длительность:** 1 день\n"
            "> 🚫 **Инвайты:** не требуются\n"
            "> 📊 Нажмите кнопку, чтобы запустить."
        ),
        color=6776679
    )
    e2.set_image(url=IMG_STRIPE_GW)

    view = ui.View(timeout=120)
    view.add_item(TemplateStartButton("150"))
    view.add_item(TemplateStartButton("400"))
    view.add_item(TemplateStartButton("600"))
    view.add_item(TemplateStartButton("1000"))
    await inter.response.send_message(embeds=[e1, e2], view=view, ephemeral=True)


class TemplateStartButton(ui.Button):
    def __init__(self, key: str):
        tpl = GIVEAWAY_TEMPLATES[key]
        super().__init__(
            label=tpl["label"],
            style=disnake.ButtonStyle.secondary,
            custom_id=f"gwtpl_start_{key}",
        )
        self.key = key

    async def callback(self, inter: disnake.MessageInteraction):
        if not is_giveaway_staff(inter.author):
            return await inter.response.send_message("⛔ У вас нет прав.", ephemeral=True)

        tpl = GIVEAWAY_TEMPLATES[self.key]
        prize = tpl["prize"]
        winners_count = tpl["winners"]
        description = tpl["description"]

        duration_td = timedelta(days=1)
        end_dt = datetime.now(MSK) + duration_td
        end_dt_utc = end_dt.astimezone(timezone.utc)
        end_time = int(end_dt_utc.timestamp())
        created_at = now_ts()

        await inter.response.defer(ephemeral=True)

        channel = bot.get_channel(GIVEAWAY_CHANNEL_ID)
        if not channel:
            return await inter.edit_original_message(content="❌ Канал для розыгрышей не найден.")

        embeds = build_giveaway_embeds(prize, description, winners_count, 0, end_dt_utc, 0)
        msg = await channel.send(embeds=embeds)

        cur.execute(
            """INSERT INTO giveaways
               (guild_id, channel_id, message_id, host_id, prize, description, winners_count,
                end_time, created_at, required_invites, participants, winners, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (inter.guild.id, channel.id, msg.id, inter.author.id, prize, description, winners_count,
             end_time, created_at, 0, "[]", "[]", "active")
        )
        db.commit()
        gid = cur.lastrowid
        view = GiveawayView(gid)
        bot.add_view(view, message_id=msg.id)
        await msg.edit(view=view)
        asyncio.create_task(schedule_end(gid))

        await inter.edit_original_message(
            content=f"✅ Шаблонный розыгрыш **{prize}** создан! ID: `{gid}`\n> Канал: {channel.mention}"
        )
        await log_discord(
            title="🎉 Создан шаблонный розыгрыш",
            description=(
                f"> **ID:** `{gid}`\n"
                f"> **Шаблон:** {prize} · {winners_count} поб.\n"
                f"> **Канал:** {channel.mention}\n"
                f"> **Создал:** {inter.author.mention}\n"
                f"> **Завершится:** <t:{end_time}:F>"
            ),
            color=0x00ff00
        )

# ============================================================
# СПИСОК РОЗЫГРЫШЕЙ
# ============================================================
async def list_giveaways(inter: disnake.MessageInteraction):
    rows = cur.execute(
        "SELECT * FROM giveaways WHERE guild_id=? ORDER BY giveaway_id DESC LIMIT 20",
        (inter.guild.id,)
    ).fetchall()
    if not rows:
        return await inter.edit_original_message(content="Розыгрышей не найдено.")

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
        if r["status"] == "finished" and winners:
            winners_str = " | Победители: " + ", ".join(f"<@{w}>" for w in winners)
        lines.append(
            f"{status_icon} **ID {r['giveaway_id']}** — {r['prize']}\n"
            f"> Участников: **{participants_count}** | {time_str}{winners_str}"
        )

    embed = disnake.Embed(
        title="📋 Список розыгрышей (последние 20)",
        description="\n\n".join(lines),
        color=6776679
    )
    embed.set_footer(text=f"Всего показано: {len(rows)}")
    await inter.edit_original_message(embed=embed)

# ============================================================
# СЛЭШ-КОМАНДА /invites
# ============================================================
@bot.slash_command(name="invites", description="Статистика инвайтов пользователя")
async def invites(inter: disnake.ApplicationCommandInteraction, user: disnake.Member = None, giveaway_id: int = None):
    user = user or inter.author
    stats = await get_invite_stats(inter.guild, user, giveaway_id)
    if stats is None:
        return await inter.send("❌ Не удалось получить статистику.", ephemeral=True)

    if giveaway_id is not None:
        g_row = cur.execute(
            "SELECT created_at, end_time FROM giveaways WHERE giveaway_id=? AND guild_id=?",
            (giveaway_id, inter.guild.id)
        ).fetchone()
        if not g_row:
            return await inter.send("❌ Розыгрыш не найден.", ephemeral=True)
        title = f"📨 Инвайты в розыгрыше #{giveaway_id} — {user.display_name}"
        footer = f"Период: <t:{g_row['created_at']}:d> – <t:{g_row['end_time']}:d>"
        embed = disnake.Embed(title=title, color=6776679, description=(
            f"> **Приглашено:** {stats['total']}\n"
            f"> **На сервере:** {stats['remaining']}\n"
            f"> **Ушло:** {stats['left']}\n"
            f"> **Ботов:** {stats['bots']}"
        ))
    else:
        title = f"📨 Инвайты — {user.display_name}"
        footer = "Глобальная статистика (Discord API)"
        embed = disnake.Embed(title=title, color=6776679, description=(
            f"> **Всего использований инвайтов:** {stats['total']}"
        ))

    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=footer)
    await inter.send(embed=embed, ephemeral=True)

# ============================================================
# ОТПРАВКА ПАНЕЛИ В СТАФФ-КАНАЛ
# ============================================================
async def send_giveaway_panel():
    try:
        ch = bot.get_channel(STAFF_PANEL_CHANNEL_ID)
        if not ch:
            ch = await bot.fetch_channel(STAFF_PANEL_CHANNEL_ID)
        if not ch:
            print("[PANEL] Канал панели не найден")
            return

        # Удаляем старые панели розыгрышей
        try:
            async for msg in ch.history(limit=50):
                if msg.author == bot.user and msg.embeds:
                    for e in msg.embeds:
                        if e.title and "розыгрыш" in e.title.lower():
                            try:
                                await msg.delete()
                            except Exception:
                                pass
                            break
        except Exception:
            pass

        e1 = disnake.Embed(color=6776679)
        e1.set_image(url="https://cdn.discordapp.com/attachments/1527006158282555412/1539326899224846467/image.png?ex=6a85e964&is=6a8497e4&hm=eb6d2a0fad3e844d7eac44288af9c13c8c1ed301eff219fa57f43a5e2204a766&")

        e2 = disnake.Embed(
            title="Управление розыгрышами",
            description=(
                "> Панель для управления всеми розыгрышами сервера.\n\n"
                "> **Начать розыгрыш** — создать вручную с настройками.\n"
                "> **Шаблонные розыгрыши** — готовые DC-розыгрыши в один клик.\n"
                "> **Лист розыгрышей** — все текущие и завершённые.\n"
                "> **Завершить принудительно** — закрыть досрочно.\n"
                "> **Перевыбрать победителя** — реролл после завершения."
            ),
            color=6776679
        )
        e2.set_image(url=IMG_STRIPE_GW)

        await ch.send(embeds=[e1, e2], view=GiveawayPanelView())
        print("[PANEL] Панель розыгрышей отправлена")
    except Exception as e:
        print(f"[PANEL ERROR] {e}")

# ============================================================
# ON_READY
# ============================================================
@bot.event
async def on_ready():
    await bot.change_presence(status=disnake.Status.online, activity=disnake.Game("Розыгрыши"))

    for guild in bot.guilds:
        missing_roles = []
        for role_id in GIVEAWAY_FULL_ROLES:
            if not guild.get_role(role_id):
                missing_roles.append(str(role_id))
        if missing_roles:
            await log_discord(
                "⚠️ Отсутствуют роли",
                f"> На сервере **{guild.name}** отсутствуют: {', '.join(missing_roles)}",
                color=0xff6600
            )
        await sync_invites(guild)

    # Активные розыгрыши → view + schedule
    active_rows = cur.execute("SELECT giveaway_id, message_id FROM giveaways WHERE status='active'").fetchall()
    for r in active_rows:
        gid = r["giveaway_id"]
        msg_id = r["message_id"]
        view = GiveawayView(gid)
        bot.add_view(view, message_id=msg_id)
        asyncio.create_task(schedule_end(gid))

    # Панель в стафф-канал
    await send_giveaway_panel()

    await log_discord(
        "✅ Бот запущен",
        f"> **{bot.user}** готов к работе.\n> Активных розыгрышей: {len(active_rows)}",
        color=0x00ff00
    )
    print(f"✅ Bot ready as {bot.user} | Активных: {len(active_rows)}")

bot.run(TOKEN)
