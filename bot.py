import os
import json
import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database import Database
from server import start_web_server

# 環境変数と設定の読み込み
DATABASE_URL = os.environ.get("DATABASE_URL")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
JST = ZoneInfo("Asia/Tokyo")

with open('config.json', 'r', encoding='utf-8') as f:
    CONFIG = json.load(f)

# Bot初期化
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True

class AttendanceBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.db = Database(DATABASE_URL)
        self.scheduler = AsyncIOScheduler(timezone=JST)

    async def setup_hook(self):
        await self.db.connect()
        await self.tree.sync()
        self.scheduler.add_job(midnight_batch_process, CronTrigger(hour=0, minute=0, timezone=JST))
        self.scheduler.start()

bot = AttendanceBot()

# --- VCイベント検知 ---
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    now = datetime.now(JST)

    if before.channel is None and after.channel is not None:
        await bot.db.set_vc_join(member.id, member.guild.id, now)

    elif before.channel is not None and after.channel is None:
        records = await bot.db.get_all_current_vc()
        user_record = next((r for r in records if r['user_id'] == member.id), None)
        
        if user_record:
            join_time = user_record['join_time'].astimezone(JST)
            if join_time.date() == now.date():
                duration = int((now - join_time).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, now.date(), duration)
            else:
                end_of_join_day = datetime.combine(join_time.date() + timedelta(days=1), datetime.min.time(), tzinfo=JST)
                duration_day1 = int((end_of_join_day - join_time).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, join_time.date(), duration_day1)
                
                start_of_leave_day = datetime.combine(now.date(), datetime.min.time(), tzinfo=JST)
                duration_day2 = int((now - start_of_leave_day).total_seconds() // 60)
                await bot.db.add_daily_time(member.id, member.guild.id, now.date(), duration_day2)

            await bot.db.remove_vc_join(member.id)

# --- 0時バッチ処理 ---
async def midnight_batch_process():
    print(f"\n--- [0時バッチ処理開始] {datetime.now(JST)} ---")
    now = datetime.now(JST)
    yesterday = (now - timedelta(days=1)).date()

    current_vc_users = await bot.db.get_all_current_vc()
    for record in current_vc_users:
        user_id = record['user_id']
        guild_id = record['guild_id']
        join_time = record['join_time'].astimezone(JST)

        duration = int((now - join_time).total_seconds() // 60)
        await bot.db.add_daily_time(user_id, guild_id, yesterday, duration)
        await bot.db.set_vc_join(user_id, guild_id, now)

    for guild in bot.guilds:
        threshold = await bot.db.get_threshold(guild.id)
        for member in guild.members:
            if member.bot:
                continue
            await update_member_role(member, guild, yesterday, threshold)
    print("--- [0時バッチ処理終了] ---\n")


# --- 出席率計算・ロール更新ロジック ---
def get_total_valid_days(start_date: date, end_date: date) -> int:
    valid_days = 0
    current = start_date
    weekdays_exclude = CONFIG['exclude_days']['weekdays']
    holidays_exclude =[datetime.strptime(d, "%Y-%m-%d").date() for d in CONFIG['exclude_days']['holidays']]

    while current <= end_date:
        if current.weekday() not in weekdays_exclude and current not in holidays_exclude:
            valid_days += 1
        current += timedelta(days=1)
    return valid_days

async def calculate_attendance(member: discord.Member, guild: discord.Guild, target_date: date, threshold: int):
    records = await bot.db.get_user_attendance(member.id, guild.id)
    bot_start = datetime.strptime(CONFIG['bot_start_date'], "%Y-%m-%d").date()
    member_join = member.joined_at.astimezone(JST).date() if member.joined_at else bot_start

    if records:
        oldest_record_date = min(r['record_date'] for r in records)
        member_start = min(member_join, oldest_record_date)
    else:
        member_start = member_join

    start_date = max(bot_start, member_start)

    if target_date < start_date:
        return 0, 0, 0

    total_valid_days = get_total_valid_days(start_date, target_date)
    if total_valid_days == 0:
        return 0, 0, 0

    attended_days = 0
    weekdays_exclude = CONFIG['exclude_days']['weekdays']
    holidays_exclude = [datetime.strptime(d, "%Y-%m-%d").date() for d in CONFIG['exclude_days']['holidays']]

    for r in records:
        r_date = r['record_date']
        if r_date.weekday() in weekdays_exclude or r_date in holidays_exclude:
            continue
        if r_date < start_date or r_date > target_date:
            continue

        if r['is_override']:
            if r['override_status'] == 'attended':
                attended_days += 1
        else:
            if r['total_minutes'] >= threshold:
                attended_days += 1

    rate = (attended_days / total_valid_days) * 100
    return min(rate, 100.0), attended_days, total_valid_days

async def update_member_role(member: discord.Member, guild: discord.Guild, target_date: date, threshold: int):
    rate, attended, total = await calculate_attendance(member, guild, target_date, threshold)

    # ★ ロック状態を確認
    is_locked = await bot.db.get_user_lock_status(member.id, guild.id)

    if is_locked:
        log_msg = "🔒 ロールは固定(ロック)されているため、自動更新をスキップしました。"
        return rate, attended, total, log_msg

    old_percent = 0
    old_role_name = "なし"
    for r in member.roles:
        for cfg in CONFIG['roles']:
            if r.name == cfg['name']:
                if cfg['min_percent'] > old_percent:
                    old_percent = cfg['min_percent']
                    old_role_name = cfg['name']

    target_role_name = None
    new_percent = 0
    new_role_name = "なし"
    for role_cfg in sorted(CONFIG['roles'], key=lambda x: x['min_percent'], reverse=True):
        if rate >= role_cfg['min_percent']:
            target_role_name = role_cfg['name']
            new_percent = role_cfg['min_percent']
            new_role_name = role_cfg['name']
            break

    all_role_names = [r['name'] for r in CONFIG['roles']]
    roles_to_remove = []
    role_to_add = None
    server_has_target_role = False

    for r in guild.roles:
        if r.name in all_role_names:
            if r.name == target_role_name:
                role_to_add = r
                server_has_target_role = True
            else:
                roles_to_remove.append(r)

    log_messages = []
    if target_role_name and not server_has_target_role:
        log_messages.append(f"⚠️ エラー: '{target_role_name}' という名前のロールがサーバーに存在しません。")

    actual_roles_to_remove = [r for r in roles_to_remove if r in member.roles]
    needs_update = False

    if actual_roles_to_remove:
        needs_update = True
    if role_to_add and role_to_add not in member.roles:
        needs_update = True

    if not needs_update:
        if target_role_name and server_has_target_role:
            log_messages.append(f"ℹ️ 維持 ({new_role_name})")
        elif not target_role_name:
            log_messages.append("ℹ️ 基準に未達")
        
        final_log = "\n".join(log_messages)
        return rate, attended, total, final_log

    try:
        if actual_roles_to_remove:
            await member.remove_roles(*actual_roles_to_remove, reason="出席率システム: 古いロールの剥奪")
        if role_to_add and role_to_add not in member.roles:
            await member.add_roles(role_to_add, reason="出席率システム: 新しいロールの付与")

        if new_percent > old_percent:
            log_messages.append(f"🎉 昇格！ ({old_role_name} ➔ {new_role_name})")
        elif new_percent < old_percent:
            log_messages.append(f"📉 降格... ({old_role_name} ➔ {new_role_name})")
        else:
            log_messages.append(f"🔄 更新 ({old_role_name} ➔ {new_role_name})")

    except discord.Forbidden:
        log_messages.append("❌ 権限エラー: Botのロールが対象ロールより下か、管理権限がありません。")
    except Exception as e:
        log_messages.append(f"❌ 予期せぬエラー: {str(e)}")

    final_log = "\n".join(log_messages)
    return rate, attended, total, final_log


# --- スラッシュコマンド ---
@bot.tree.command(name="attendance", description="現在の出席率を確認し、ロールを更新します")
async def attendance(interaction: discord.Interaction, target_user: discord.Member = None):
    await interaction.response.defer()

    user = target_user or interaction.user
    today = datetime.now(JST).date()
    threshold = await bot.db.get_threshold(interaction.guild.id)
    
    rate, attended, total, log_msg = await update_member_role(user, interaction.guild, today, threshold)
    
    embed = discord.Embed(title=f"📊 {user.display_name} の出席率", color=discord.Color.blue())
    embed.add_field(name="出席率", value=f"**{rate:.1f}%**", inline=False)
    embed.add_field(name="出席日数 / 総日数", value=f"{attended}日 / {total}日", inline=False)
    embed.add_field(name="⚙️ ロール更新ステータス", value=f"```\n{log_msg}\n```", inline=False)
    embed.set_footer(text=f"規定時間: 1日{threshold}分以上")
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="absent_days", description="指定したユーザーの「欠席した日(規定時間未達)」を列挙します")
async def absent_days(interaction: discord.Interaction, target_user: discord.Member = None):
    await interaction.response.defer()
    
    user = target_user or interaction.user
    guild = interaction.guild
    threshold = await bot.db.get_threshold(guild.id)
    
    yesterday = datetime.now(JST).date() - timedelta(days=1)
    records = await bot.db.get_user_attendance(user.id, guild.id)
    bot_start = datetime.strptime(CONFIG['bot_start_date'], "%Y-%m-%d").date()
    member_join = user.joined_at.astimezone(JST).date() if user.joined_at else bot_start

    if records:
        oldest_record_date = min(r['record_date'] for r in records)
        member_start = min(member_join, oldest_record_date)
    else:
        member_start = member_join

    start_date = max(bot_start, member_start)

    if yesterday < start_date:
        await interaction.followup.send("集計できる過去の期間がありません（今日参加したばかりなど）。")
        return

    weekdays_exclude = CONFIG['exclude_days']['weekdays']
    holidays_exclude = [datetime.strptime(d, "%Y-%m-%d").date() for d in CONFIG['exclude_days']['holidays']]

    record_dict = {r['record_date']: r for r in records}
    
    absent_list = []
    current = start_date
    while current <= yesterday:
        if current.weekday() in weekdays_exclude or current in holidays_exclude:
            current += timedelta(days=1)
            continue
        
        is_attended = False
        if current in record_dict:
            r = record_dict[current]
            if r['is_override']:
                if r['override_status'] == 'attended':
                    is_attended = True
            else:
                if r['total_minutes'] >= threshold:
                    is_attended = True
        
        if not is_attended:
            mins = record_dict[current]['total_minutes'] if current in record_dict else 0
            absent_list.append(f"`{current.strftime('%Y-%m-%d')}` (滞在: {mins}分)")
            
        current += timedelta(days=1)
        
    embed = discord.Embed(title=f"📅 {user.display_name} の欠席日一覧", color=discord.Color.red())
    embed.description = f"対象期間: `{start_date.strftime('%Y-%m-%d')}` ～ `{yesterday.strftime('%Y-%m-%d')}`"
    
    if not absent_list:
        embed.add_field(name="欠席日", value="欠席日はありません！皆勤です🎉")
    else:
        chunk = ""
        field_count = 1
        for item in absent_list:
            if len(chunk) + len(item) + 1 > 1000:
                embed.add_field(name=f"欠席日 ({field_count})", value=chunk, inline=False)
                chunk = item + "\n"
                field_count += 1
            else:
                chunk += item + "\n"
        if chunk:
            embed.add_field(name=f"欠席日 ({field_count})", value=chunk, inline=False)

    embed.set_footer(text=f"合計欠席日数: {len(absent_list)}日 / 規定時間: {threshold}分")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="total_time", description="指定したユーザーの総VC滞在時間(累計)を表示します")
async def total_time(interaction: discord.Interaction, target_user: discord.Member = None):
    await interaction.response.defer()
    
    user = target_user or interaction.user
    guild = interaction.guild
    
    records = await bot.db.get_user_attendance(user.id, guild.id)
    total_minutes = sum(r['total_minutes'] for r in records)
    
    current_vc_users = await bot.db.get_all_current_vc()
    current_record = next((r for r in current_vc_users if r['user_id'] == user.id), None)
    
    if current_record:
        join_time = current_record['join_time'].astimezone(JST)
        now = datetime.now(JST)
        duration = int((now - join_time).total_seconds() // 60)
        total_minutes += duration
        
    hours = total_minutes // 60
    minutes = total_minutes % 60
    
    embed = discord.Embed(title=f"⏱️ {user.display_name} の総VC滞在時間", color=discord.Color.green())
    embed.description = f"**{hours} 時間 {minutes} 分**\n(累計: {total_minutes} 分)"
    
    if current_record:
        embed.set_footer(text="※現在VC滞在中のため、リアルタイムの時間を加算して表示しています")
         
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="ranking", description="メンバーの総VC滞在時間ランキングを表示します")
async def ranking(interaction: discord.Interaction):
    await interaction.response.defer()
    guild = interaction.guild

    async with bot.db.pool.acquire() as conn:
        records = await conn.fetch('''
            SELECT user_id, SUM(total_minutes) as total
            FROM daily_attendance 
            WHERE guild_id = $1 
            GROUP BY user_id
        ''', guild.id)
        
    total_times = {r['user_id']: r['total'] for r in records}

    current_vc_users = await bot.db.get_all_current_vc()
    now = datetime.now(JST)
    for r in current_vc_users:
        if r['guild_id'] == guild.id:
            uid = r['user_id']
            join_time = r['join_time'].astimezone(JST)
            duration = int((now - join_time).total_seconds() // 60)
            total_times[uid] = total_times.get(uid, 0) + duration

    ranking_data = []
    for member in guild.members:
        if member.bot:
            continue
        t = total_times.get(member.id, 0)
        if t > 0:
            ranking_data.append((member, t))

    ranking_data.sort(key=lambda x: x[1], reverse=True)

    if not ranking_data:
        await interaction.followup.send("まだVCの滞在記録がありません。")
        return

    embed = discord.Embed(title=f"🏆 {guild.name} VC滞在時間ランキング", color=discord.Color.gold())
    
    description = ""
    for i, (member, t) in enumerate(ranking_data[:15], 1):
        hours = t // 60
        mins = t % 60
        
        if i == 1: medal = "🥇"
        elif i == 2: medal = "🥈"
        elif i == 3: medal = "🥉"
        else: medal = f"`{i}.`"
        
        description += f"{medal} **{member.display_name}** : {hours}時間{mins}分\n"
        
    embed.description = description
    embed.set_footer(text=f"全 {len(ranking_data)} 名中 上位15名を表示 / リアルタイム反映済")
    
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="set_threshold", description="【管理者用】出席判定の規定時間(分)を変更します")
@app_commands.default_permissions(manage_roles=True)
async def set_threshold(interaction: discord.Interaction, minutes: int):
    if minutes <= 0:
        await interaction.response.send_message("1以上の数値を指定してください。", ephemeral=True)
        return
    await bot.db.set_threshold(interaction.guild.id, minutes)
    await interaction.response.send_message(f"✅ このサーバーの出席規定時間を **{minutes}分** に変更しました。")


@bot.tree.command(name="override_attendance", description="【管理者用】過去の出席状況を強制上書きします")
@app_commands.default_permissions(manage_roles=True)
@app_commands.choices(status=[
    app_commands.Choice(name="出席 (attended)", value="attended"),
    app_commands.Choice(name="欠席 (absent)", value="absent")
])
async def override_attendance(interaction: discord.Interaction, target_user: discord.Member, target_date: str, status: app_commands.Choice[str]):
    try:
        date_obj = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        await interaction.response.send_message("❌ 日付の形式が正しくありません。`YYYY-MM-DD` で入力してください。", ephemeral=True)
        return

    records = await bot.db.get_user_attendance(target_user.id, interaction.guild.id)
    bot_start = datetime.strptime(CONFIG['bot_start_date'], "%Y-%m-%d").date()
    member_join = target_user.joined_at.astimezone(JST).date() if target_user.joined_at else bot_start

    if records:
        oldest_record_date = min(r['record_date'] for r in records)
        member_start = min(member_join, oldest_record_date)
    else:
        member_start = member_join

    start_date = max(bot_start, member_start)

    if date_obj < start_date:
        await interaction.response.send_message("❌ 参加前、またはBot導入前の日付のため変更できません。", ephemeral=True)
        return

    await bot.db.set_override(target_user.id, interaction.guild.id, date_obj, status.value)
    threshold = await bot.db.get_threshold(interaction.guild.id)
    _, _, _, log_msg = await update_member_role(target_user, interaction.guild, datetime.now(JST).date(), threshold)

    await interaction.response.send_message(f"✅ {target_user.display_name} の `{target_date}` の記録を **{status.name}** に上書きしました。\n```\n{log_msg}\n```")


# ★新規追加: ユーザーのロールをロック(固定)するコマンド
@bot.tree.command(name="lock_role", description="【管理者用】指定ユーザーのロール自動更新を固定(ロック)・解除します")
@app_commands.default_permissions(manage_roles=True)
@app_commands.choices(action=[
    app_commands.Choice(name="🔒 固定する (自動更新ストップ)", value=1),
    app_commands.Choice(name="🔓 解除する (自動更新スタート)", value=0)
])
async def lock_role(interaction: discord.Interaction, target_user: discord.Member, action: app_commands.Choice[int]):
    is_locked = bool(action.value)
    await bot.db.set_user_lock_status(target_user.id, interaction.guild.id, is_locked)
    
    if is_locked:
        status_text = "🔒 固定 (自動更新ストップ)"
        msg = f"✅ {target_user.display_name} のロールを現在の状態に **{status_text}** しました。\n以降、出席率が変動してもロールは自動で更新されません。"
    else:
        status_text = "🔓 解除 (自動更新スタート)"
        msg = f"✅ {target_user.display_name} のロール固定を **{status_text}** しました。\n次回の集計時から、出席率に応じたロールが自動付与されます。"

    await interaction.response.send_message(msg)


if __name__ == "__main__":
    start_web_server()
    bot.run(DISCORD_TOKEN)