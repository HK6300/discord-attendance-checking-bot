import asyncpg
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
        await self._init_tables()

    async def _init_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    threshold_minutes INT DEFAULT 30
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS current_vc (
                    user_id BIGINT PRIMARY KEY,
                    guild_id BIGINT,
                    join_time TIMESTAMP WITH TIME ZONE
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS daily_attendance (
                    user_id BIGINT,
                    guild_id BIGINT,
                    record_date DATE,
                    total_minutes INT DEFAULT 0,
                    is_override BOOLEAN DEFAULT FALSE,
                    override_status VARCHAR(20),
                    PRIMARY KEY (user_id, guild_id, record_date)
                )
            ''')
            # ★新規追加: ユーザーごとの設定（ロール固定フラグなど）を保存するテーブル
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id BIGINT,
                    guild_id BIGINT,
                    is_role_locked BOOLEAN DEFAULT FALSE,
                    PRIMARY KEY (user_id, guild_id)
                )
            ''')

    async def get_threshold(self, guild_id: int) -> int:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval('SELECT threshold_minutes FROM guild_settings WHERE guild_id = $1', guild_id)
            return val if val else 30

    async def set_threshold(self, guild_id: int, minutes: int):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO guild_settings (guild_id, threshold_minutes)
                VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET threshold_minutes = $2
            ''', guild_id, minutes)

    async def set_vc_join(self, user_id: int, guild_id: int, join_time: datetime):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO current_vc (user_id, guild_id, join_time)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id) DO UPDATE SET join_time = $3
            ''', user_id, guild_id, join_time)

    async def get_all_current_vc(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch('SELECT user_id, guild_id, join_time FROM current_vc')

    async def remove_vc_join(self, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM current_vc WHERE user_id = $1', user_id)

    async def add_daily_time(self, user_id: int, guild_id: int, date: datetime.date, minutes: int):
        if minutes <= 0:
            return
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO daily_attendance (user_id, guild_id, record_date, total_minutes)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, guild_id, record_date) 
                DO UPDATE SET total_minutes = daily_attendance.total_minutes + $4
            ''', user_id, guild_id, date, minutes)

    async def set_override(self, user_id: int, guild_id: int, date: datetime.date, status: str):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO daily_attendance (user_id, guild_id, record_date, total_minutes, is_override, override_status)
                VALUES ($1, $2, $3, 0, TRUE, $4)
                ON CONFLICT (user_id, guild_id, record_date) 
                DO UPDATE SET is_override = TRUE, override_status = $4
            ''', user_id, guild_id, date, status)

    async def get_user_attendance(self, user_id: int, guild_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetch('''
                SELECT record_date, total_minutes, is_override, override_status 
                FROM daily_attendance 
                WHERE user_id = $1 AND guild_id = $2
            ''', user_id, guild_id)

    # ★新規追加: ユーザーのロールロック状態を取得
    async def get_user_lock_status(self, user_id: int, guild_id: int) -> bool:
        async with self.pool.acquire() as conn:
            val = await conn.fetchval('''
                SELECT is_role_locked FROM user_settings 
                WHERE user_id = $1 AND guild_id = $2
            ''', user_id, guild_id)
            return bool(val)

    # ★新規追加: ユーザーのロールロック状態を保存
    async def set_user_lock_status(self, user_id: int, guild_id: int, is_locked: bool):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO user_settings (user_id, guild_id, is_role_locked)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, guild_id) 
                DO UPDATE SET is_role_locked = $3
            ''', user_id, guild_id, is_locked)