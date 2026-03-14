import os
import json
import sqlite3
import datetime
from datetime import time, timezone, timedelta
import random
import discord
from discord import ui
from discord.ext import commands, tasks
from google import genai
from dotenv import load_dotenv

# --- 1. 環境初始化 ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", "study_fortress.db")
MIS_CHANNEL_ID = int(os.getenv("MIS_CHANNEL_ID", 0))

tw_tz = timezone(timedelta(hours=8))
client = genai.Client(api_key=GEMINI_KEY)
MODEL_NAME = "gemini-2.5-flash"  # ✨ 統一使用 2.5 flash


# --- 2. 資料庫核心 ---
class StudyDB:
    def __init__(self, path):
        db_dir = os.path.dirname(path)
        if db_dir and not os.path.exists(db_dir): os.makedirs(db_dir)
        self.conn = sqlite3.connect(path)
        self.create_tables()

    def create_tables(self):
        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, xp INTEGER DEFAULT 0, last_answered DATE)")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS questions_history (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, question_text TEXT, created_at DATE)")

    def get_user(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute("SELECT xp, last_answered FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone()

    def get_top_users(self, limit=10):
        cursor = self.conn.cursor()
        cursor.execute("SELECT user_id, xp FROM users ORDER BY xp DESC LIMIT ?", (limit,))
        return cursor.fetchall()

    def add_xp(self, user_id, xp_gain):
        today = datetime.date.today().isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO users (user_id, xp, last_answered) VALUES (?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET xp = xp + ?, last_answered = ?",
                (user_id, xp_gain, today, xp_gain, today))


# --- 3. UI 元件 (Modal & View) ---
class AnswerModal(ui.Modal, title='📝 提交資管觀念挑戰'):
    answer = ui.TextInput(label='你的回答', style=discord.TextStyle.paragraph,
                          placeholder='針對 MIS/DB/網路 觀念進行回答...', min_length=5, max_length=500)

    def __init__(self, db, today_q):
        super().__init__()
        self.db = db
        self.today_q = today_q

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ 導師正在批閱中...", ephemeral=True)
        instruction = """你是一位台灣資管所考研名師。針對回答給予專業建議與觀念補強。300字內。
        最後一行格式：SCORE_DATA: {"score": 1-10, "is_related": bool}"""
        try:
            response = client.models.generate_content(
                model=MODEL_NAME,
                contents=f"題目：{self.today_q}\n回答：{self.answer.value}",
                config={'system_instruction': instruction}
            )
            ai_reply = response.text
            if "SCORE_DATA:" in ai_reply:
                main_text, _, json_part = ai_reply.partition("SCORE_DATA:")
                data = json.loads(json_part.strip().replace("```json", "").replace("```", ""))
                if data.get('is_related'):
                    user_info = self.db.get_user(interaction.user.id)
                    status = "✨ 獲得經驗！" if not user_info or user_info[
                        1] != datetime.date.today().isoformat() else "💡 今日已領取"
                    if "✨" in status: self.db.add_xp(interaction.user.id, int(10 + data['score'] * 2))
                    embed = discord.Embed(title="🎯 修行結算", description=main_text.strip()[:1000], color=0xe67e22)
                    embed.add_field(name="狀態", value=status)
                    await interaction.edit_original_response(content=None, embed=embed)
                else:
                    await interaction.edit_original_response(content="⚠️ 內容不相關。")
            else:
                await interaction.edit_original_response(content=ai_reply[:1900])
        except Exception as e:
            print(f"批改失敗: {e}");
            await interaction.edit_original_response(content="🚨 系統忙碌")


class ChallengeView(ui.View):
    def __init__(self, db, today_q):
        super().__init__(timeout=None)
        self.db = db
        self.today_q = today_q

    @ui.button(label="📝 我要挑戰", style=discord.ButtonStyle.primary, custom_id="mis_challenge_btn_v3")
    async def submit_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AnswerModal(self.db, self.today_q))


# --- 4. 考官模組 (Examiner) ---
class MIS_Examiner(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot
        self.db = db
        self.daily_task.start()

    @tasks.loop(time=time(hour=8, minute=30, tzinfo=tw_tz))
    async def daily_task(self):
        await self.push_question()

    async def push_question(self):
        channel = self.bot.get_channel(MIS_CHANNEL_ID)
        if not channel: return
        subjects = ["MIS管理資訊系統", "資料庫系統", "資料通訊與網路", "資訊安全管理"]
        target = random.choice(subjects)
        prompt = f"產出一題關於 {target} 的資管考研觀念題，50字內。"
        try:
            res = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            q_text = res.text.strip()
            embed = discord.Embed(title="📊 資管每日觀念挑戰", description=f"**{q_text}**", color=0xe67e22)
            await channel.create_thread(name=f"【資管挑戰】{datetime.date.today()} | {target}", embed=embed,
                                        view=ChallengeView(self.db, q_text))
        except Exception as e:
            print(f"🚨 MIS 產題失敗: {e}")

    @commands.command(name="mis_test")
    @commands.has_permissions(administrator=True)
    async def mis_test(self, ctx):
        await self.push_question()


# --- 5. ✨ 自動家教模組 (Tutor Mode - 僅私訊觸發) ---
class TutorCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.history = {}

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return

        # ✨ 無需切換：如果是私訊，自動啟動家教
        if isinstance(message.channel, discord.DMChannel):
            async with message.channel.typing():
                user_id = message.author.id
                if user_id not in self.history: self.history[user_id] = []

                self.history[user_id].append({"role": "user", "parts": [message.content]})

                # 家教的人格設定
                instruction = """你是一位專業的資管所考研專屬家教（由 Gemini 2.5 Flash 驅動）。
                1. 語氣親切專業，擅長蘇格拉底引導法。
                2. 學生問問題時，先引導其思考，再給予層次分明的解答。
                3. 擅長將複雜技術觀念轉化為資管維度的商業應用場景。"""

                try:
                    chat = client.chats.create(
                        model=MODEL_NAME,
                        config={'system_instruction': instruction},
                        history=self.history[user_id][:-1]
                    )
                    response = chat.send_message(message.content)
                    self.history[user_id].append({"role": "model", "parts": [response.text]})

                    # 記憶體控制
                    if len(self.history[user_id]) > 10: self.history[user_id] = self.history[user_id][-10:]

                    await message.reply(response.text)
                except Exception as e:
                    print(f"家教對話錯誤: {e}")
                    await message.reply("🚨 導師目前正在休息，請稍後再詢問。")


# --- 6. 排行榜系統 ---
class RankingCog(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot;
        self.db = db

    @commands.command(name="top")
    async def top(self, ctx):
        users = self.db.get_top_users(10)
        desc = ""
        for i, (uid, xp) in enumerate(users, 1):
            user = self.bot.get_user(uid)
            name = user.display_name if user else f"戰友({uid})"
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            desc += f"{medal} **{name}** — `{xp} XP` (Lv.{(xp // 100) + 1})\n"
        await ctx.send(
            embed=discord.Embed(title="🏆 考研要塞：資管首席榜", description=desc or "尚無數據", color=0xf1c40f))

    @commands.command(name="rank")
    async def rank(self, ctx):
        info = self.db.get_user(ctx.author.id)
        if not info: return await ctx.send("🔍 尚未有修行紀錄。")
        xp, date = info
        embed = discord.Embed(title="📊 個人修行成就", color=0x2ecc71)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.add_field(name="等級", value=f"**Lv.{(xp // 100) + 1}**", inline=True)
        embed.add_field(name="累積經驗", value=f"**{xp} XP**", inline=True)
        embed.set_footer(text=f"最後修行：{date}")
        await ctx.send(embed=embed)


# --- 7. 啟動入口 ---
class MyBot(commands.Bot):
    def __init__(self):
        # 確保 intents 包含訊息內容與私訊
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.db = StudyDB(DB_PATH)

    async def setup_hook(self):
        await self.add_cog(MIS_Examiner(self, self.db))
        await self.add_cog(RankingCog(self, self.db))
        await self.add_cog(TutorCog(self))
        self.add_view(ChallengeView(self.db, ""))

    async def on_ready(self):
        print(f"🚀 {self.user.name} 穩健版已啟動！")
        print(f"📡 使用模型：{MODEL_NAME}")


if __name__ == "__main__":
    bot = MyBot()
    bot.run(TOKEN)