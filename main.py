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

# --- 1. 基礎配置 ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DB_PATH = os.getenv("DB_PATH", "study_fortress.db")
MIS_CHANNEL_ID = int(os.getenv("MIS_CHANNEL_ID", 0))

tw_tz = timezone(timedelta(hours=8))
client = genai.Client(api_key=GEMINI_KEY)
MODEL_NAME = "gemini-2.5-flash"

# 激勵金句庫
MOTIVATIONAL_QUOTES = [
    "「預測未來最好的方法，就是去創造它。」— Peter Drucker",
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
                          placeholder='寫下你的想法，導師幫你看看...', min_length=5, max_length=500)

    def __init__(self, db, today_q):
        super().__init__()
        self.db = db
        self.today_q = today_q

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ 導師閱卷中，先喝口水吧...", ephemeral=True)
        instruction = "你是一位暖心且專業的資管所名師。請針對回答給予具備同理心的建議與專業詳解。最後一行格式：SCORE_DATA: {\"score\": 1-10, \"is_related\": bool}"
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
                    status = "✨ 經驗已增加！" if not user_info or user_info[
                        1] != datetime.date.today().isoformat() else "💡 今日修行已達標"
                    if "✨" in status: self.db.add_xp(interaction.user.id, int(10 + data['score'] * 2))
                    embed = discord.Embed(title="🎯 閱卷結算", description=main_text.strip()[:1000], color=0xe67e22)
                    embed.add_field(name="狀態", value=status)
                    await interaction.edit_original_response(content=None, embed=embed)
                else:
                    await interaction.edit_original_response(content="⚠️ 內容好像有點跑題了，再想看看？")
            else:
                await interaction.edit_original_response(content=ai_reply[:1900])
        except Exception as e:
            print(f"批改失敗: {e}");
            await interaction.edit_original_response(content="🚨 導師腦袋打結了，請等我喝杯咖啡。")


class ChallengeView(ui.View):
    def __init__(self, db, today_q):
        super().__init__(timeout=None)
        self.db = db;
        self.today_q = today_q

    @ui.button(label="📝 開始修行", style=discord.ButtonStyle.primary, custom_id="mis_v4_btn")
    async def submit_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AnswerModal(self.db, self.today_q))


# --- 4. 考官 Cog ---
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
        target = random.choice(["MIS", "資料庫", "網路資安", "數位轉型"])
        quote = random.choice(MOTIVATIONAL_QUOTES)
        try:
            res = client.models.generate_content(model=MODEL_NAME,
                                                 contents=f"產出一題關於 {target} 的資管考研觀念題，50字內。")
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


# --- 5. ✨ 穩定版心靈導師 Cog (解決拒答與罷工) ---
class TutorCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.history_cache = {}

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return
        if isinstance(message.channel, discord.DMChannel):
            async with message.channel.typing():
                user_id = message.author.id
                if user_id not in self.history_cache: self.history_cache[user_id] = []

                instruction = """你是一位具備『資管教授深度』與『戰友溫感』的考研導師。

                【行為準則：核心權重分配】
                1. 暖心開場 (10%)：先用一句話稱呼戰友並給予情緒共感（例如：辛苦了、這題很有深度）。
                2. 硬核解惑 (70%)：這是最重要的部分！當學生問及專業知識（MIS、DB、OS、網路、資安），你必須給出『教科書等級』的精確定義、結構化要點或圖表式說明，回覆長度控制在繁體中文 600 字以內。
                3. 蘇格拉底引導 (20%)：在給出完整解答後，再提出一個啟發性的問題，引導學生思考進階應用。

                【禁忌】
                - 絕對不可只給鼓勵而不給答案。
                - 絕對不可用模糊的詞彙帶過技術細節。
                - 專業知識必須準確（例如：提到正規化，必須說明 1NF, 2NF, 3NF 的差異）。"""

                # 建立純文字歷史紀錄 (Stateless 模式最穩定)
                api_contents = []
                for entry in self.history_cache[user_id]:
                    api_contents.append({"role": entry["role"], "parts": [{"text": entry["content"]}]})
                api_contents.append({"role": "user", "parts": [{"text": message.content}]})

                try:
                    response = client.models.generate_content(
                        model=MODEL_NAME, contents=api_contents,
                        config={'system_instruction': instruction}
                    )
                    ai_text = response.text
                    # 更新快取
                    self.history_cache[user_id].append({"role": "user", "content": message.content})
                    self.history_cache[user_id].append({"role": "model", "content": ai_text})
                    if len(self.history_cache[user_id]) > 8: self.history_cache[user_id] = self.history_cache[user_id][
                                                                                           -8:]
                    await message.reply(ai_text)
                except Exception as e:
                    print(f"🚨 導師異常: {e}")
                    await message.reply(
                        "抱歉戰友，導師剛才思緒斷線了。☕\n可以再跟我說一次嗎？或是輸入 `!reset` 讓我清醒一下。")

    @commands.command(name="reset")
    async def reset_tutor(self, ctx):
        self.history_cache[ctx.author.id] = []
        await ctx.send("🧹 **導師記憶已重置！** 讓我們重新聊聊吧。")


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
            name = user.display_name if user else f"戰友({uid})"
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
        print(f"🚀 {self.user.name} 穩健心靈導師版已啟動！")


if __name__ == "__main__":
    bot = MyBot()
    bot.run(TOKEN)