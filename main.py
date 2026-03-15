import os
import json
import sqlite3
import datetime
import socket
import aiohttp
import random
import asyncio
from datetime import time, timezone, timedelta
import discord
from discord import ui
from discord.ext import commands, tasks
from groq import AsyncGroq
from dotenv import load_dotenv

# --- 1. 環境與模型配置 ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")
DB_PATH = os.getenv("DB_PATH", "study_fortress.db")
tw_tz = timezone(timedelta(hours=8))

# ✨ 使用目前 Groq 支援的最強穩定模型
groq_client = AsyncGroq(api_key=GROQ_KEY)
MODEL_NAME = "llama-3.3-70b-versatile"

env_id = os.getenv("MIS_CHANNEL_ID")
MIS_CHANNEL_ID = int(env_id) if env_id and env_id.isdigit() else 0

MOTIVATIONAL_QUOTES = [
    "「預測未來最好的方法，就是去創造它。」— Peter Drucker",
    "「技術會更迭，但邏輯思考與管理智慧是永遠的護城河。」",
    "「今天的每一分努力，都是在為未來的系統做最穩健的 Commit。」"
]


# --- 2. 權限防禦 ---
def is_mis_channel():
    async def predicate(ctx):
        if ctx.author.guild_permissions.administrator: return True
        is_target = (ctx.channel.id == MIS_CHANNEL_ID)
        is_thread = (getattr(ctx.channel, 'parent_id', None) == MIS_CHANNEL_ID)
        return is_target or is_thread

    return commands.check(predicate)


# --- 3. 資料庫核心 (含自動遷移補強) ---
class StudyDB:
    def __init__(self, path):
        db_dir = os.path.dirname(path)
        if db_dir and not os.path.exists(db_dir): os.makedirs(db_dir)
        self.conn = sqlite3.connect(path)
        self.create_tables()

    def create_tables(self):
        with self.conn:
            # 使用者資料
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, xp INTEGER DEFAULT 0, last_answered DATE)")
            # 題目歷史
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS questions_history (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, question_text TEXT, created_at DATE)")

            # ✨ 核心加固：自動檢查並補上 subject 欄位 (解決 Migration 問題)
            try:
                self.conn.execute("ALTER TABLE questions_history ADD COLUMN subject TEXT")
                print("✅ 已自動補齊 subject 欄位")
            except sqlite3.OperationalError:
                pass  # 欄位已存在

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

    def add_question(self, subject, text):
        with self.conn:
            self.conn.execute("INSERT INTO questions_history (subject, question_text, created_at) VALUES (?, ?, ?)",
                              (subject, text, datetime.date.today().isoformat()))


# --- 4. UI 元件 (✨ 具備 XP 即時回饋顯示) ---
class AnswerModal(ui.Modal, title='📝 提交修行答案'):
    answer = ui.TextInput(label='你的回答', style=discord.TextStyle.paragraph, placeholder='戰友，寫下你的邏輯...',
                          min_length=5, max_length=500)

    def __init__(self, db, today_q):
        super().__init__()
        self.db = db
        self.today_q = today_q

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ 助教閱卷中...", ephemeral=True)
        instruction = "你是一位資管所考研戰友。請針對回答給予簡短建議。最後一行格式：SCORE_DATA: {\"score\": 1-10, \"is_related\": bool}"
        try:
            completion = await groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": f"題目：{self.today_q}\n回答：{self.answer.value}"}
                ],
                model=MODEL_NAME,
                temperature=0.4
            )
            ai_reply = completion.choices[0].message.content
            if "SCORE_DATA:" in ai_reply:
                main_text, _, json_part = ai_reply.partition("SCORE_DATA:")
                data = json.loads(json_part.strip().replace("```json", "").replace("```", ""))

                if data.get('is_related'):
                    user_info = self.db.get_user(interaction.user.id)
                    today_str = datetime.date.today().isoformat()
                    xp_gain = int(10 + data['score'] * 2)
                    embed = discord.Embed(title="🎯 結算報告", description=main_text.strip()[:1000], color=0xe67e22)

                    if not user_info or user_info[1] != today_str:
                        self.db.add_xp(interaction.user.id, xp_gain)
                        embed.add_field(name="XP 獲得", value=f"✨ 戰友太強了！這次修行獲得了 **{xp_gain}** 點經驗值！")
                    else:
                        embed.add_field(name="XP 狀態", value="💡 今日修行已達標，經驗值已領取過囉。")

                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send("⚠️ 內容好像跑題了，戰友再確認一下？", ephemeral=True)
            else:
                await interaction.followup.send(ai_reply[:1900], ephemeral=True)
        except Exception as e:
            print(f"Error: {e}")
            await interaction.followup.send("🚨 助教連線異常。", ephemeral=True)


class ChallengeView(ui.View):
    def __init__(self, db, today_q):
        super().__init__(timeout=None)
        self.db = db
        self.today_q = today_q

    @ui.button(label="📝 開始修行", style=discord.ButtonStyle.primary, custom_id="mis_v11_btn")
    async def submit_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(AnswerModal(self.db, self.today_q))


# --- 5. 考官與排行榜模組 ---
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
        target = random.choice(["MIS 觀念", "資料庫", "資訊安全", "數位轉型與策略"])
        try:
            res = await groq_client.chat.completions.create(
                messages=[{"role": "user", "content": f"產出一題關於 {target} 的資管考研觀念題，50字內。"}],
                model=MODEL_NAME
            )
            q_text = res.choices[0].message.content.strip()
            self.db.add_question(target, q_text)
            embed = discord.Embed(title=f"⚡ 每日挑戰 | {target}", description=f"**{q_text}**", color=0xe67e22)
            await channel.create_thread(name=f"【戰友修行】{datetime.date.today()}", embed=embed,
                                        view=ChallengeView(self.db, q_text))
        except Exception as e:
            print(f"產題失敗: {e}")

    @commands.command(name="test_push")
    @commands.has_permissions(administrator=True)
    async def test_push(self, ctx):
        await ctx.send("🚀 正在手動觸發 MIS AI 出題測試...", delete_after=5)
        await self.push_question()

    @commands.command(name="top")
    @is_mis_channel()
    async def top(self, ctx):
        users = self.db.get_top_users(10)
        desc = "".join([
            f"{'🥇' if i == 1 else '🥈' if i == 2 else '🥉' if i == 3 else f'{i}.'} **{self.bot.get_user(uid).display_name if self.bot.get_user(uid) else uid}** — `{xp} XP` (Lv.{(xp // 100) + 1})\n"
            for i, (uid, xp) in enumerate(users, 1)])
        await ctx.send(embed=discord.Embed(title="🏆 戰友修行榜", description=desc or "目前無人上榜", color=0xf1c40f))

    @commands.command(name="rank")
    @is_mis_channel()
    async def rank(self, ctx):
        info = self.db.get_user(ctx.author.id)
        if not info: return await ctx.send("🔍 戰友，目前還沒有修行紀錄喔。")
        xp, date = info
        embed = discord.Embed(title="📊 個人成就卡", color=0x2ecc71)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.add_field(name="等級", value=f"**Lv.{(xp // 100) + 1}**", inline=True)
        embed.add_field(name="累積經驗", value=f"**{xp} XP**", inline=True)
        embed.set_footer(text=f"最後修行：{date}")
        await ctx.send(embed=embed)


# --- 6. 戰友 TutorCog (平實戰友版) ---
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

                # ✨ 使用你精心調整的最終版 Prompt (不油膩、有同理心、有免責聲明)
                instruction = """你是一位正在準備資管所考研的『戰友助教』。

                【人格特質】
                - 語氣：自然、像大學同學。因為你是考生的得力戰友，可以保持溫柔，需顧及考生的心態。
                - 表情符號：每則回覆最多限制使用 1 到 2 個 Emoji。
                - 善用條列：當對方再進行選擇，可善用條列給予對方建議。

                【話題控制】
                - 關於『生活/食物』：站在對方的角度，適時的誇獎或給予建議。
                - 話題導回：連續兩輪沒聊學科時，結尾簡短提醒進度。

                【語言守則】
                - 必須使用台灣繁體中文（行程、執行緒、記憶體、死結）。
                - 計算題強制使用『代碼塊』垂直拆解，嚴禁 LaTeX。

                【社交邏輯：像個正常人】
                1. 生活與學科的比例：
                   - 當戰友聊生活/晚餐/累了，請『順著話聊』，並且保持溫柔，站在對方的角度思考，並適時給予鼓勵。
                   - 不要主動提到讀書，除非戰友先提，或是對話已經閒聊超過三輪。
                2. 拒絕說教
                3. 表情管制：
                   - 全文最多 1 個 Emoji，甚至不用也可以，維持工程師的簡潔感。
                4. 有同理心：
                   - 試圖站在對方的角度看問題，像是解題困難，壓力大，或是吃飯的選擇。
                5. 關於餐食選擇：
                   - 可以以健康的角度給予建議，如果可以的話請給考生條列幾種組合作選擇，讓考生可以保持健康的飲食。
                   - 若考生想吃放縱餐，不要直接阻擋，適時給予情緒價值，並鼓勵考生繼續努力。
                6. 關於情緒：
                   - 考生在準備的過程可能伴隨巨大壓力，若考生有了情緒請適時地安慰或給予鼓勵。
                   - 你的角色是給予考生繼續努力的動力，可以試圖用座右銘激勵他！
                7. 關於離題：
                   - 閒聊一下是很重要的，但如果話題是關於其他專業領域，請不要過度回答。
                   - 若考生想放鬆，請不要阻攔，最重要的是讓考生隨時保持動力！
                8. 免責聲明：
                   - 若探討學術問題，請記得在最後提醒考生內容不一定正確，因為你是AI。"""

                msgs = [{"role": "system", "content": instruction}]
                for e in self.history_cache[user_id]: msgs.append({"role": e["role"], "content": e["content"]})
                msgs.append({"role": "user", "content": message.content})

                try:
                    completion = await groq_client.chat.completions.create(
                        messages=msgs,
                        model=MODEL_NAME,
                        temperature=0.5
                    )
                    ai_text = completion.choices[0].message.content
                    self.history_cache[user_id].append({"role": "user", "content": message.content})
                    self.history_cache[user_id].append({"role": "assistant", "content": ai_text})
                    if len(self.history_cache[user_id]) > 6: self.history_cache[user_id] = self.history_cache[user_id][
                                                                                           -6:]
                    await message.reply(ai_text)
                except Exception:
                    await message.reply("連線有點噴了，戰友晚點再試。")

    @commands.command(name="reset")
    async def reset_tutor(self, ctx):
        self.history_cache[ctx.author.id] = []
        await ctx.send("🧹 記憶已清空。")


# --- 7. 啟動入口 ---
class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())
        self.db = StudyDB(DB_PATH)

    async def setup_hook(self):
        # ✨ IPv4 加固
        connector = aiohttp.TCPConnector(family=socket.AF_INET)
        self.http.connector = connector
        # 註冊組件 (Ranking 已整合進 Examiner)
        await self.add_cog(MIS_Examiner(self, self.db))
        await self.add_cog(TutorCog(self))
        self.add_view(ChallengeView(self.db, ""))

    async def on_ready(self):
        print(f"🚀 {self.user.name} MIS 全能回饋修正版已啟動。")


if __name__ == "__main__":
    bot = MyBot()
    bot.run(TOKEN)