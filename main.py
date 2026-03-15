import os
import json
import sqlite3
import datetime
import socket  # ✨ 處理協定族
import aiohttp  # ✨ 處理加固連線
from datetime import time, timezone, timedelta
import random
import asyncio
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
MODEL_NAME = "gemini-2.5-flash"

MOTIVATIONAL_QUOTES = [
    "「預測未來最好的方法，就是去創造它。」— Peter Drucker",
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


# --- 3. UI 元件 (非同步批改模式) ---
class AnswerModal(ui.Modal, title='📝 提交資管觀念挑戰'):
    answer = ui.TextInput(label='你的回答', style=discord.TextStyle.paragraph,
                          placeholder='寫下你的想法，導師幫你看看...', min_length=5, max_length=500)

    def __init__(self, db, today_q):
        super().__init__()
        self.db = db;
        self.today_q = today_q

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ 導師閱卷中，先喝口水吧...", ephemeral=True)
        instruction = "你是一位暖心且專業的資管所名師。請針對回答給予具備同理心的建議與專業詳解。最後一行格式：SCORE_DATA: {\"score\": 1-10, \"is_related\": bool}"
        try:
            # ✨ 非同步 API 呼叫
            response = await client.aio.models.generate_content(
                model=MODEL_NAME, contents=f"題目：{self.today_q}\n回答：{self.answer.value}",
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
            await interaction.edit_original_response(content="🚨 導師腦袋打結了。")


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
            res = await client.aio.models.generate_content(
                model=MODEL_NAME, contents=f"產出一題關於 {target} 的資管考研觀念題，50字內。"
            )
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


# --- 5. ✨ 心靈導師 Cog (含格式規範) ---
class TutorCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot;
        self.history_cache = {}

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot: return
        if isinstance(message.channel, discord.DMChannel):
            async with message.channel.typing():
                user_id = message.author.id
                if user_id not in self.history_cache: self.history_cache[user_id] = []

                instruction = """你是一位具備『資管教授深度』與『戰友溫感』的考研導師。
                【行為準則】
                1. 暖心開場 (10%)、硬核解惑 (70%)、蘇格拉底引導 (20%)。
                2. 正面解惑：必須給出『精確且結構化』的解答。
                【呈現規範】
                - 回覆長度控制在 600 字內。
                - 嚴格禁止長串水平公式。涉及計算，強制使用「垂直拆解」並放進「代碼塊 (Code Block)」中對齊。
                - 數學表示式請用純文字或 markdown 呈現。"""

                api_contents = [{"role": e["role"], "parts": [{"text": e["content"]}]} for e in
                                self.history_cache[user_id]]
                api_contents.append({"role": "user", "parts": [{"text": message.content}]})

                try:
                    response = await client.aio.models.generate_content(model=MODEL_NAME, contents=api_contents,
                                                                        config={'system_instruction': instruction})
                    ai_text = response.text
                    self.history_cache[user_id].append({"role": "user", "content": message.content})
                    self.history_cache[user_id].append({"role": "model", "content": ai_text})
                    if len(self.history_cache[user_id]) > 8: self.history_cache[user_id] = self.history_cache[user_id][
                                                                                           -8:]
                    await message.reply(ai_text)
                except Exception as e:
                    if "429" in str(e):
                        await message.reply("戰友，導師目前「修行額度」用完了。☕\n請稍等一分鐘或明天再試。")
                    else:
                        print(f"🚨 家教異常: {e}")
                        await message.reply("導師思緒斷線了，請稍後再試。")

    @commands.command(name="reset")
    async def reset_tutor(self, ctx):
        self.history_cache[ctx.author.id] = []
        await ctx.send("🧹 **導師記憶已重置！**")


# --- 6. 排行榜系統 ---
class RankingCog(commands.Cog):
    def __init__(self, bot, db):
        self.bot = bot;
        self.db = db

    @commands.command(name="top")
    async def top(self, ctx):
        users = self.db.get_top_users(10)
        desc = "".join([
                           f"{'🥇' if i == 1 else '🥈' if i == 2 else '🥉' if i == 3 else f'{i}.'} **{self.bot.get_user(uid).display_name if self.bot.get_user(uid) else uid}** — `{xp} XP` (Lv.{(xp // 100) + 1})\n"
                           for i, (uid, xp) in enumerate(users, 1)])
        await ctx.send(
            embed=discord.Embed(title="🏆 資管考研要塞：首席榜", description=desc or "目前無數據", color=0xf1c40f))

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


# --- 7. 啟動入口 (✨ 修正初始化時機) ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.db = StudyDB(DB_PATH)

    async def setup_hook(self):
        # ✨ 在這裡初始化連線器，解決 RuntimeError
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        self.http.connector = connector

        # 註冊所有模組
        await self.add_cog(MIS_Examiner(self, self.db))
        await self.add_cog(RankingCog(self, self.db))
        await self.add_cog(TutorCog(self))
        self.add_view(ChallengeView(self.db, ""))

    async def on_ready(self):
        print(f"🚀 {self.user.name} 最終穩定加固版已啟動！")


if __name__ == "__main__":
    bot = MyBot()
    bot.run(TOKEN)