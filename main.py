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

# --- 1. 環境與模型配置 ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", "study_fortress.db")
MIS_CHANNEL_ID = int(os.getenv("MIS_CHANNEL_ID", 0))

tw_tz = timezone(timedelta(hours=8))
client = genai.Client(api_key=GEMINI_KEY)
# ✨ 統一使用 2.5 Flash
MODEL_NAME = "gemini-2.5-flash"

# 📚 考研激勵金句庫
MOTIVATIONAL_QUOTES = [
    "「預測未來最好的方法，就是去創造它。」— Peter Drucker",
    "「管理就是把事情做得正確；領導就是做正確的事情。」",
    "「考研不是為了擊敗別人，而是為了遇見更好的自己。」",
    "「技術會更迭，但邏輯思考與管理智慧是永遠的護城河。」",
    "「今天的每一分努力，都是在為未來的系統做最穩健的 Commit。」"
]


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


# --- 3. UI 元件 (論壇批改) ---
class AnswerModal(ui.Modal, title='📝 提交資管觀念挑戰'):
    answer = ui.TextInput(label='你的回答', style=discord.TextStyle.paragraph,
                          placeholder='孩子，寫下你的想法，導師幫你看看...', min_length=5, max_length=500)

    def __init__(self, db, today_q):
        super().__init__()
        self.db = db
        self.today_q = today_q

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ 導師正在細心閱卷中，先喝口水吧...", ephemeral=True)
        instruction = """你是一位暖心且專業的台灣資管所考研名師。針對學生的回答給予具備同理心的建議。
        除了糾正觀念，請給予適度的鼓勵。300字內。
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
                    status = "✨ 勤奮修行！經驗已增加。" if not user_info or user_info[
                        1] != datetime.date.today().isoformat() else "💡 今日修行已達標"
                    if "✨" in status: self.db.add_xp(interaction.user.id, int(10 + data['score'] * 2))
                    embed = discord.Embed(title="🎯 閱卷結算", description=main_text.strip()[:1000], color=0xe67e22)
                    embed.add_field(name="狀態", value=status)
                    await interaction.edit_original_response(content=None, embed=embed)
                else:
                    await interaction.edit_original_response(content="⚠️ 這題好像離題了，再想看看？")
            else:
                await interaction.edit_original_response(content=ai_reply[:1900])
        except Exception as e:
            print(f"批改出錯: {e}");
            await interaction.edit_original_response(content="🚨 導師腦袋稍微打結了，請等我喝杯咖啡再試。")


class ChallengeView(ui.View):
    def __init__(self, db, today_q):
        super().__init__(timeout=None)
        self.db = db
        self.today_q = today_q

    @ui.button(label="📝 開始修行", style=discord.ButtonStyle.primary, custom_id="mis_challenge_v4")
    async def submit_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AnswerModal(self.db, self.today_q))


# --- 4. 考官模組 (論壇推送) ---
class MIS_Examiner(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot;
        self.db = db
        self.daily_task.start()

    @tasks.loop(time=time(hour=8, minute=30, tzinfo=tw_tz))
    async def daily_task(self):
        await self.push_question()

    async def push_question(self):
        channel = self.bot.get_channel(MIS_CHANNEL_ID)
        if not channel: return
        subjects = ["MIS管理資訊系統", "資料庫", "網路與資安", "數位轉型個案"]
        target = random.choice(subjects)
        quote = random.choice(MOTIVATIONAL_QUOTES)

        prompt = f"產出一題關於 {target} 的資管考研觀念題，50字內。語氣要像鼓勵學生的資研導師。"
        try:
            res = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            q_text = res.text.strip()
            embed = discord.Embed(title=f"📊 資管每日修行 | {target}", description=f"**{q_text}**", color=0xe67e22)
            embed.set_footer(text=f"💡 今日金句：{quote}")
            await channel.create_thread(name=f"【資管修行】{datetime.date.today()}", embed=embed,
                                        view=ChallengeView(self.db, q_text))
        except Exception as e:
            print(f"🚨 產題失敗: {e}")

    @commands.command(name="mis_test")
    @commands.has_permissions(administrator=True)
    async def mis_test(self, ctx):
        await self.push_question()


# --- 5. ✨ 心靈導師 Cog (修正後的穩定對話版) ---
class TutorCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # 儲存對話 Session 歷史
        self.history_cache = {}

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return

        if isinstance(message.channel, discord.DMChannel):
            async with message.channel.typing():
                user_id = message.author.id

                # 初始化歷史紀錄
                if user_id not in self.history_cache:
                    self.history_cache[user_id] = []

                instruction = """你是一位暖心且專業的資管所考研導師（由 Gemini 2.5 Flash 驅動）。
                - 情感支持：稱呼學生為『孩子』或『戰友』。如果離題聊生活或表現出壓力，請給予溫暖的共感。
                - 溫和導引：在情緒支持後，溫和地將話題帶回 MIS、資料庫、網路或資安觀念。
                - 蘇格拉底教學：多提問、多舉例，引導學生思考，不要直接丟答案。"""

                try:
                    # ✨ 建立 Session 並同步之前的歷史
                    chat = client.chats.create(
                        model=MODEL_NAME,
                        config={'system_instruction': instruction},
                        history=self.history_cache[user_id]
                    )

                    # ✨ 發送新訊息
                    response = chat.send_message(message.content)

                    # ✨ 同步 chat.history 到快取 (這是維持連續對話的關鍵)
                    self.history_cache[user_id] = chat.history

                    # 記憶體管理：只保留最近 12 則
                    if len(self.history_cache[user_id]) > 12:
                        self.history_cache[user_id] = self.history_cache[user_id][-12:]

                    await message.reply(response.text)

                except Exception as e:
                    print(f"🚨 家教對話異常: {e}")
                    error_hints = [
                        "導師剛才喝咖啡不小心灑到筆記了，能請你再說一次剛才的問題嗎？☕",
                        "抱歉孩子，剛才訊號有點斷斷續續，我沒聽清楚，我們可以重新聊聊嗎？",
                        "導師現在腦袋稍微打結了，等我深呼吸一下，你再問我一次好嗎？"
                    ]
                    await message.reply(random.choice(error_hints))


# --- 6. 排名系統 ---
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
            name = user.display_name if user else f"隱世高手({uid})"
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            desc += f"{medal} **{name}** — `{xp} XP` (Lv.{(xp // 100) + 1})\n"
        await ctx.send(
            embed=discord.Embed(title="🏆 資管考研要塞：首席榜", description=desc or "尚無數據", color=0xf1c40f))


# --- 7. 啟動入口 ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.db = StudyDB(DB_PATH)

    async def setup_hook(self):
        await self.add_cog(MIS_Examiner(self, self.db))
        await self.add_cog(RankingCog(self, self.db))
        await self.add_cog(TutorCog(self))
        self.add_view(ChallengeView(self.db, ""))

    async def on_ready(self):
        print(f"🚀 {self.user.name} 心靈導師版已上線 | 模式：穩定家教對話")


if __name__ == "__main__":
    bot = MyBot()
    bot.run(TOKEN)