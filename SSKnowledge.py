import asyncio
import json
import os
import random
import sqlite3
import hashlib
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '0'))
MAX_PLAYERS = 50
QUESTION_COUNT = 25
ANSWER_SECONDS = 15
MIN_PLAYERS = 1  # TEMPORARY: set back to 2 after testing
LOBBY_SECONDS = 600
INTERMISSION_SECONDS = 7
TIE_READY_SECONDS = 120
QUESTION_FILE = Path(__file__).with_name('questions.json')
DATA_DIR = Path(os.getenv('DATA_DIR', str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / 'quiz_history.sqlite3'
SS_LOGO_URL = os.getenv('SS_LOGO_URL', '').strip()
ASSET_DIR = Path(__file__).with_name('assets')

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
games = {}


def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('CREATE TABLE IF NOT EXISTS used_questions (guild_id INTEGER NOT NULL, question_id TEXT NOT NULL, PRIMARY KEY (guild_id, question_id))')
    conn.execute('CREATE TABLE IF NOT EXISTS rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, played_at TEXT NOT NULL, player_count INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS results (round_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL, score INTEGER NOT NULL, placement INTEGER NOT NULL, PRIMARY KEY(round_id, user_id))')
    conn.commit()
    return conn


def question_id(question):
    # Stable across process restarts and shuffled game order.
    payload = json.dumps([question['question'], question['options'], question['answer']], ensure_ascii=False)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def mark_used(guild_id, questions):
    with db_connect() as conn:
        conn.executemany('INSERT OR IGNORE INTO used_questions VALUES (?, ?)', [(guild_id, question_id(q)) for q in questions])


def select_questions(guild_id, bank):
    targets = {'easy': 2, 'medium': 9, 'hard': 14}
    with db_connect() as conn:
        used = {row[0] for row in conn.execute('SELECT question_id FROM used_questions WHERE guild_id=?', (guild_id,))}
    selected = []
    for level, count in targets.items():
        candidates = [q for q in bank if q.get('difficulty') == level]
        if len(candidates) < count:
            raise ValueError(f'Not enough {level} questions (need {count}).')
        random.shuffle(candidates)
        fresh = [q for q in candidates if question_id(q) not in used]
        old = [q for q in candidates if question_id(q) in used]
        selected.extend((fresh + old)[:count])
    random.shuffle(selected)
    mark_used(guild_id, selected)
    return selected


def save_round(game, final_order):
    with db_connect() as conn:
        cursor = conn.execute('INSERT INTO rounds (guild_id, played_at, player_count) VALUES (?, ?, ?)',
                              (game.channel.guild.id, datetime.now(timezone.utc).isoformat(timespec='seconds'), len(game.players)))
        conn.executemany('INSERT INTO results (round_id, user_id, name, score, placement) VALUES (?, ?, ?, ?, ?)',
                         [(cursor.lastrowid, uid, game.players[uid], game.scores[uid], i)
                          for i, uid in enumerate(final_order, 1)])


def past_round_embed(guild_id, round_number=None):
    with db_connect() as conn:
        if round_number is None:
            item = conn.execute('SELECT id, played_at, player_count FROM rounds WHERE guild_id=? ORDER BY id DESC LIMIT 1', (guild_id,)).fetchone()
        else:
            item = conn.execute('SELECT id, played_at, player_count FROM rounds WHERE guild_id=? AND id=?', (guild_id, round_number)).fetchone()
        if not item:
            return themed_embed('📜 Previous Quiz Results', 'No completed rounds found yet.')
        rid, played_at, count = item
        rows = conn.execute('SELECT name, score, placement FROM results WHERE round_id=? ORDER BY placement', (rid,)).fetchall()
    medals = ['🥇', '🥈', '🥉']
    lines = [f'{medals[place-1] if place <= 3 else str(place)+"."} **{name}** — **{score:,} pts**'
             for name, score, place in rows[:50]]
    result = themed_embed(f'🏆 Round #{rid} — Final Standings', '\n'.join(lines) or 'No scores', discord.Color.gold())
    result.add_field(name='Completed (UTC)', value=played_at.replace('T', ' ')[:16], inline=True)
    result.add_field(name='Players', value=str(count), inline=True)
    return result


def records_embed(guild_id):
    with db_connect() as conn:
        rows = conn.execute("""SELECT user_id, MAX(name), SUM(score), COUNT(*), MAX(score)
                               FROM results JOIN rounds ON rounds.id = results.round_id
                               WHERE rounds.guild_id=? GROUP BY user_id ORDER BY SUM(score) DESC LIMIT 15""", (guild_id,)).fetchall()
    lines = [f'**{i}. {name}** — **{total:,} pts** · {games_played} games · best {best:,}'
             for i, (_, name, total, games_played, best) in enumerate(rows, 1)]
    return themed_embed('👑 SS All-Time Leaderboard', '\n'.join(lines) or 'No completed games yet.', discord.Color.gold())



def load_questions():
    data = json.loads(QUESTION_FILE.read_text(encoding='utf-8'))
    valid = []
    for item in data:
        if (isinstance(item, dict) and isinstance(item.get('question'), str)
                and isinstance(item.get('options'), list) and len(item['options']) == 4
                and all(isinstance(x, str) and 0 < len(x) <= 80 for x in item['options'])
                and isinstance(item.get('answer'), int) and 0 <= item['answer'] < 4):
            valid.append(item)
    return valid


def host_only(interaction):
    return interaction.user.guild_permissions.manage_guild


def themed_embed(title, description='', color=discord.Color.from_rgb(190, 155, 70)):
    e = discord.Embed(title=title, description=description, color=color)
    e.set_author(name='SECRET SERVICE  •  KNOWLEDGE SHOWDOWN')
    e.set_footer(text='SS Knowledge Showdown  •  Think fast. Answer smart. Win big.')
    if SS_LOGO_URL.startswith(('https://', 'http://')):
        e.set_thumbnail(url=SS_LOGO_URL)
    return e


def embed(title, description='', color=discord.Color.from_rgb(151, 163, 178)):
    return themed_embed(title, description, color)


def brand_file(e, kind='question'):
    """Attach the actual SS logo artwork within a sleek header banner."""
    name = f'{kind}.png'
    path = ASSET_DIR / name
    if path.exists():
        e.set_image(url=f'attachment://{name}')
        return discord.File(path, filename=name)
    return None


async def send_branded(channel, e, kind='question', **kwargs):
    picture = brand_file(e, kind)
    if picture:
        kwargs['file'] = picture
    return await channel.send(embed=e, **kwargs)


async def respond_branded(interaction, e, kind='leaderboard'):
    picture = brand_file(e, kind)
    if picture:
        return await interaction.response.send_message(embed=e, file=picture)
    return await interaction.response.send_message(embed=e)




def branded_lobby_embed(description):
    e = themed_embed('SS KNOWLEDGE SHOWDOWN  |  LOBBY', description)
    e.set_image(url='attachment://lobby.png')
    return e


class LobbyView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(label='Join Quiz', style=discord.ButtonStyle.success, emoji='🎮')
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        if game.phase != 'lobby':
            return await interaction.response.send_message('The lobby has closed.', ephemeral=True)
        if interaction.user.id in game.players:
            return await interaction.response.send_message('You have already joined!', ephemeral=True)
        if len(game.players) >= MAX_PLAYERS:
            return await interaction.response.send_message('This quiz is full.', ephemeral=True)
        game.players[interaction.user.id] = interaction.user.display_name
        game.scores[interaction.user.id] = 0
        game.streaks[interaction.user.id] = 0
        await interaction.response.send_message(f'You joined! Players: {len(game.players)}/{MAX_PLAYERS}', ephemeral=True)
        await game.refresh_lobby()

    @discord.ui.button(label='Leave Quiz', style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        if game.phase != 'lobby' or interaction.user.id not in game.players:
            return await interaction.response.send_message('You are not in the open lobby.', ephemeral=True)
        game.players.pop(interaction.user.id)
        game.scores.pop(interaction.user.id, None)
        game.streaks.pop(interaction.user.id, None)
        await interaction.response.send_message('You left the quiz.', ephemeral=True)
        await game.refresh_lobby()


    @discord.ui.button(label='Start Now (Host)', style=discord.ButtonStyle.primary, emoji='🚀')
    async def start_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        if not host_only(interaction):
            return await interaction.response.send_message('Only hosts with Manage Server permission can start early.', ephemeral=True)
        if game.phase != 'lobby' or game.lobby_start.is_set():
            return await interaction.response.send_message('The lobby is already closed.', ephemeral=True)
        if len(game.players) < MIN_PLAYERS:
            return await interaction.response.send_message(f'At least {MIN_PLAYERS} player(s) must join before starting.', ephemeral=True)
        game.lobby_start.set()
        await interaction.response.send_message('🚀 Starting the quiz now!', ephemeral=True)


class AnswerView(discord.ui.View):
    def __init__(self, game, question, eligible, seconds):
        super().__init__(timeout=None)
        self.game = game
        self.question = question
        self.eligible = set(eligible)
        self.answers = {}
        self.open = True
        self.lock = asyncio.Lock()
        for index, option in enumerate(question['options']):
            button = discord.ui.Button(label=f'{chr(65 + index)}. {option}', style=discord.ButtonStyle.primary, row=index // 2)
            async def callback(interaction, choice=index):
                async with self.lock:
                    if not self.open or self.game.phase == 'ended':
                        return await interaction.response.send_message('This question is closed.', ephemeral=True)
                    if interaction.user.id not in self.eligible:
                        return await interaction.response.send_message('You are not eligible for this question.', ephemeral=True)
                    if interaction.user.id in self.answers:
                        return await interaction.response.send_message('Your answer is already locked.', ephemeral=True)
                    self.answers[interaction.user.id] = choice
                    await interaction.response.send_message('Answer locked in! 🔒', ephemeral=True)
            button.callback = callback
            self.add_item(button)

    async def close(self, message):
        async with self.lock:
            self.open = False
            for child in self.children:
                child.disabled = True
        try:
            await message.edit(view=self)
        except discord.HTTPException:
            pass


class Game:
    def __init__(self, channel, host_id, questions):
        self.channel = channel
        self.host_id = host_id
        self.questions = questions
        self.players = {}
        self.scores = {}
        self.streaks = {}
        self.phase = 'lobby'
        self.lobby_start = asyncio.Event()
        self.paused = False
        self.pause_gate = asyncio.Event()
        self.pause_gate.set()
        self.task = None
        self.lobby_message = None
        self.active_view = None
        self.active_message = None
        self.round_number = 0

    async def refresh_lobby(self):
        if self.lobby_message and self.phase == 'lobby':
            try:
                await self.lobby_message.edit(embed=branded_lobby_embed(
                    f'**Players:** {len(self.players)}/{MAX_PLAYERS}\n**Questions:** {QUESTION_COUNT}\n'
                    f'**Answer time:** {ANSWER_SECONDS}s\n**Lobby:** {LOBBY_SECONDS // 60} minutes (or host starts early)\n**Test mode:** {MIN_PLAYERS} player minimum\n\n'
                    'Click **Join Quiz** to enter!'), view=LobbyView(self))
            except discord.HTTPException:
                pass

    async def wait_or_end(self, seconds):
        # Pause takes effect between questions, not during a live answer timer.
        for _ in range(seconds):
            if self.phase == 'ended':
                return False
            await self.pause_gate.wait()
            await asyncio.sleep(1)
        return self.phase != 'ended'

    async def run(self):
        try:
            # Lobby closes after ten minutes, or immediately when an authorised host clicks Start Now.
            try:
                await asyncio.wait_for(self.lobby_start.wait(), timeout=LOBBY_SECONDS)
            except asyncio.TimeoutError:
                pass
            if self.phase == 'ended':
                return
            if len(self.players) < MIN_PLAYERS:
                await self.channel.send(f'Quiz cancelled: at least {MIN_PLAYERS} player(s) required.')
                return
            self.phase = 'playing'
            if self.lobby_message:
                await self.lobby_message.edit(view=None)
            await self.channel.send(f'🎉 **The quiz is starting!** {len(self.players)} players are competing.')
            for i, question in enumerate(self.questions, 1):
                if self.phase == 'ended':
                    return
                await self.pause_gate.wait()
                self.round_number = i
                await self.ask(question, i, False, list(self.players))
                if self.phase == 'ended':
                    return
                if i % 5 == 0:
                    await send_branded(self.channel, self.leaderboard(), 'leaderboard')
                if i < len(self.questions) and not await self.wait_or_end(INTERMISSION_SECONDS):
                    return
            await self.finish()
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback
            traceback.print_exc()
            await self.channel.send('The quiz stopped due to an unexpected error. Check the bot logs.')
        finally:
            self.phase = 'ended'
            if games.get(self.channel.guild.id) is self:
                games.pop(self.channel.guild.id, None)

    async def ask(self, question, number, tie, eligible):
        self.phase = 'tiebreaker' if tie else 'playing'
        seconds = 10 if tie else ANSWER_SECONDS
        view = AnswerView(self, question, eligible, seconds)
        self.active_view = view
        title = f'⚔️ Tiebreaker {number}' if tie else f'❓ Question {number}/{len(self.questions)}'
        options = '\n'.join(f'**{chr(65+i)}.** {opt}' for i, opt in enumerate(question['options']))
        message = await send_branded(self.channel, embed(title, f"**{question['question']}**\n\n{options}\n\n⏳ {seconds} seconds to answer"), 'tiebreaker' if tie else 'question', view=view)
        self.active_message = message
        await asyncio.sleep(seconds)
        await view.close(message)
        if self.phase == 'ended':
            return set()
        correct = {uid for uid, answer in view.answers.items() if answer == question['answer']}
        if not tie:
            for uid in self.players:
                if uid in correct:
                    self.streaks[uid] += 1
                    self.scores[uid] += 100
                    if self.streaks[uid] % 3 == 0:
                        self.scores[uid] += 100
                else:
                    self.streaks[uid] = 0
        letter = chr(65 + question['answer'])
        await self.channel.send(embed=embed('✅ Answer revealed',
            f"**{letter}. {question['options'][question['answer']]}**\n"
            f"Correct players: **{len(correct)}/{len(eligible)}**", discord.Color.green()))
        self.active_view = None
        self.active_message = None
        return correct

    def leaderboard(self):
        ranked = sorted(self.scores, key=lambda uid: (-self.scores[uid], self.players[uid].lower()))
        lines = [f'**{i}.** {self.players[uid]} — **{self.scores[uid]}** pts'
                 for i, uid in enumerate(ranked[:10], 1)]
        return embed('🏆 SS Quiz Leaderboard', '\n'.join(lines) or 'No players yet.')

    async def finish(self):
        if self.phase == 'ended':
            return
        await self.channel.send('🏁 **All questions completed!** Checking the final standings...')
        ranked_scores = sorted(set(self.scores.values()), reverse=True)
        # Resolve ties affecting the top three places; unrelated ties retain shared ranks.
        final_order = []
        tie_pool = [q for q in load_questions() if q not in self.questions]
        with db_connect() as conn:
            used = {r[0] for r in conn.execute('SELECT question_id FROM used_questions WHERE guild_id=?', (self.channel.guild.id,))}
        random.shuffle(tie_pool)
        tie_pool.sort(key=lambda q: question_id(q) in used)
        for score in ranked_scores:
            tied = [uid for uid, value in self.scores.items() if value == score]
            if len(final_order) >= 3:
                final_order.extend(sorted(tied, key=lambda uid: self.players[uid].lower()))
                continue
            if len(tied) > 1:
                tied = await self.resolve_tie(tied, score, tie_pool)
            final_order.extend(tied)
            if self.phase == 'ended':
                return
        medals = ['🥇', '🥈', '🥉']
        lines = [f'{medals[i] if i < 3 else f"{i+1}."} <@{uid}> — **{self.scores[uid]} pts**'
                 for i, uid in enumerate(final_order)]
        save_round(self, final_order)
        await send_branded(self.channel, embed('🏆 SS KNOWLEDGE SHOWDOWN — FINAL RESULTS', '\n'.join(lines), discord.Color.gold()), 'results')
        await self.channel.send('📚 Previous results: `/quiz-history` · All-time scores: `/quiz-records`')

    async def resolve_tie(self, tied, score, pool):
        mentions = ' '.join(f'<@{uid}>' for uid in tied)
        await self.channel.send(f'⚔️ **SUDDEN DEATH TIEBREAKER!**\n{mentions}\n'
                                f'You are tied on **{score} points**. Prepare yourselves! '
                                f'Starting in {TIE_READY_SECONDS} seconds.',
                                allowed_mentions=discord.AllowedMentions(users=True))
        if not await self.wait_or_end(TIE_READY_SECONDS):
            return tied
        survivors = list(tied)
        eliminated = []
        round_no = 0
        while len(survivors) > 1 and self.phase != 'ended':
            if not pool:
                await self.channel.send('⚠️ Tiebreaker question pool exhausted. Remaining players share the position; host may resolve prizes manually.')
                break
            round_no += 1
            await self.pause_gate.wait()
            tie_question = pool.pop(0)
            mark_used(self.channel.guild.id, [tie_question])
            correct = await self.ask(tie_question, round_no, True, survivors)
            if not correct:
                await self.channel.send('Nobody answered correctly — all players remain!')
            elif len(correct) == len(survivors):
                await self.channel.send('Everyone answered correctly — another round!')
            else:
                eliminated_now = [uid for uid in survivors if uid not in correct]
                eliminated = eliminated_now + eliminated
                survivors = [uid for uid in survivors if uid in correct]
                await self.channel.send('**Still in:** ' + ', '.join(self.players[uid] for uid in survivors))
            if len(survivors) > 1 and not await self.wait_or_end(INTERMISSION_SECONDS):
                break
        return survivors + eliminated


@bot.tree.command(name='quiz', description='SS quiz host commands')
@app_commands.describe(action='Choose a quiz action')
@app_commands.choices(action=[app_commands.Choice(name=x, value=x) for x in ('start', 'pause', 'resume', 'skip', 'leaderboard', 'end')])
async def quiz(interaction: discord.Interaction, action: app_commands.Choice[str]):
    if not interaction.guild:
        return await interaction.response.send_message('Use this command in a server.', ephemeral=True)
    game = games.get(interaction.guild.id)
    command = action.value
    if command == 'leaderboard':
        return await respond_branded(interaction, game.leaderboard() if game else past_round_embed(interaction.guild.id))
    if not host_only(interaction):
        return await interaction.response.send_message('Only members with Manage Server permission can host quizzes.', ephemeral=True)
    if command == 'start':
        if game:
            return await interaction.response.send_message('A quiz is already running in this server.', ephemeral=True)
        bank = load_questions()
        if len(bank) < QUESTION_COUNT + 10:
            return await interaction.response.send_message(f'Add at least {QUESTION_COUNT + 10} valid questions to questions.json (25 main + 10 tiebreakers).', ephemeral=True)
        groups = {level: [q for q in bank if q.get('difficulty') == level] for level in ('easy', 'medium', 'hard')}
        targets = {'easy': 2, 'medium': 9, 'hard': 14}
        if any(len(groups[level]) < count for level, count in targets.items()):
            return await interaction.response.send_message('Question bank needs at least 2 easy, 9 medium and 14 hard questions.', ephemeral=True)
        chosen = select_questions(interaction.guild.id, bank)
        game = Game(interaction.channel, interaction.user.id, chosen)
        games[interaction.guild.id] = game
        await interaction.response.send_message('Quiz lobby created!', ephemeral=True)
        game.lobby_message = await send_branded(interaction.channel, branded_lobby_embed(
            f'**Players:** 0/{MAX_PLAYERS}\n**Questions:** {QUESTION_COUNT}\n'
            f'**Answer time:** {ANSWER_SECONDS}s\n**Lobby:** {LOBBY_SECONDS // 60} minutes (or host starts early)\n\nClick **Join Quiz** to enter!'), 'lobby', view=LobbyView(game))
        game.task = asyncio.create_task(game.run())
        return
    if not game:
        return await interaction.response.send_message('No quiz is running.', ephemeral=True)
    if command == 'end':
        game.phase = 'ended'
        game.pause_gate.set()
        if game.active_view and game.active_message:
            await game.active_view.close(game.active_message)
        if game.task:
            game.task.cancel()
        games.pop(interaction.guild.id, None)
        return await interaction.response.send_message('🛑 Quiz ended by the host.')
    if command == 'pause':
        game.paused = True
        game.pause_gate.clear()
        return await interaction.response.send_message('⏸️ Paused between questions. An active answer timer will finish normally.')
    if command == 'resume':
        game.paused = False
        game.pause_gate.set()
        return await interaction.response.send_message('▶️ Quiz resumed.')
    if command == 'skip':
        return await interaction.response.send_message('Skipping is not enabled in this version, to avoid unfairly shortening an active question.', ephemeral=True)


@bot.tree.command(name='quiz-history', description='View a completed quiz round, or the latest results')
@app_commands.describe(round_number='Optional round ID shown on the results')
async def quiz_history(interaction: discord.Interaction, round_number: int | None = None):
    if not interaction.guild:
        return await interaction.response.send_message('Use this command in a server.', ephemeral=True)
    await respond_branded(interaction, past_round_embed(interaction.guild.id, round_number), 'results')


@bot.tree.command(name='quiz-records', description='All-time quiz leaderboard across completed games')
async def quiz_records(interaction: discord.Interaction):
    if not interaction.guild:
        return await interaction.response.send_message('Use this command in a server.', ephemeral=True)
    await respond_branded(interaction, records_embed(interaction.guild.id), 'leaderboard')


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')


async def setup_hook():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()


bot.setup_hook = setup_hook


async def main():
    if not TOKEN:
        raise RuntimeError('Set DISCORD_TOKEN in your environment.')
    db_connect().close()
    async with bot:
        await bot.start(TOKEN)


if __name__ == '__main__':
    asyncio.run(main())
