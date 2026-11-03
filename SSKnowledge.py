import asyncio
import json
import os
import random
import sqlite3
import time
import hashlib
import math
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '0'))
# Only these two Discord accounts may manage quizzes.
HOST_IDS = {1501587787294703657, 1204163177793720331}
SS_QUIZ_ROLE_ID = 1567139397285978122
ROLE_MENTIONS = discord.AllowedMentions(everyone=False, users=False, roles=[discord.Object(id=SS_QUIZ_ROLE_ID)])
MAX_PLAYERS = 50
QUESTION_COUNT = 25
ANSWER_SECONDS = 15
MIN_PLAYERS = 1  # Solo testing enabled; set to 2 to require multiple players
LOBBY_SECONDS = 600
INTERMISSION_SECONDS = 7
TIE_READY_SECONDS = 120
QUIZ_ROLE_EMOJI = "🧠"
QUESTION_DIR = Path(__file__).with_name('questions')
QUIZ_NAMES = {'secret_service': 'Secret Service Trivia', 'autumn_thanksgiving': 'Autumn & Thanksgiving Trivia', 'general_knowledge': 'General Knowledge Trivia'}
DATA_DIR = Path(os.getenv('DATA_DIR', str(Path(__file__).parent)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / 'quiz_history.sqlite3'
SS_LOGO_URL = os.getenv('SS_LOGO_URL', '').strip()
ASSET_DIR = Path(__file__).with_name('assets')
if not ASSET_DIR.is_dir():
    ASSET_DIR = Path(__file__).parent  # Also supports PNGs uploaded to repository root

intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
games = {}


def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.execute('CREATE TABLE IF NOT EXISTS quiz_role_panels (message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS redundant_questions (guild_id INTEGER NOT NULL, question_id TEXT NOT NULL, PRIMARY KEY (guild_id, question_id))')
    conn.execute('CREATE TABLE IF NOT EXISTS used_questions (guild_id INTEGER NOT NULL, question_id TEXT NOT NULL, PRIMARY KEY (guild_id, question_id))')
    conn.execute('CREATE TABLE IF NOT EXISTS rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, played_at TEXT NOT NULL, player_count INTEGER NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS results (round_id INTEGER NOT NULL, user_id INTEGER NOT NULL, name TEXT NOT NULL, score INTEGER NOT NULL, placement INTEGER NOT NULL, PRIMARY KEY(round_id, user_id))')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(rounds)')}
    if 'quiz_key' not in columns:
        conn.execute("ALTER TABLE rounds ADD COLUMN quiz_key TEXT NOT NULL DEFAULT 'legacy'")
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
    if len(bank) < QUESTION_COUNT:
        raise ValueError(f'Expected {QUESTION_COUNT} valid questions, found {len(bank)}.')
    chosen = random.sample(bank, QUESTION_COUNT)
    return chosen


def save_round(game, final_order):
    with db_connect() as conn:
        cursor = conn.execute('INSERT INTO rounds (guild_id, played_at, player_count, quiz_key) VALUES (?, ?, ?, ?)',
                              (game.channel.guild.id, datetime.now(timezone.utc).isoformat(timespec='seconds'), len(game.players), game.quiz_key))
        conn.executemany('INSERT INTO results (round_id, user_id, name, score, placement) VALUES (?, ?, ?, ?, ?)',
                         [(cursor.lastrowid, uid, game.players[uid], game.scores[uid], i)
                          for i, uid in enumerate(final_order, 1)])


def past_round_embed(guild_id, round_number=None, quiz_key=None):
    with db_connect() as conn:
        if round_number is None:
            item = conn.execute('SELECT id, played_at, player_count, quiz_key FROM rounds WHERE guild_id=? AND quiz_key=? ORDER BY id DESC LIMIT 1', (guild_id, quiz_key)).fetchone()
        else:
            item = conn.execute('SELECT id, played_at, player_count, quiz_key FROM rounds WHERE guild_id=? AND id=? AND quiz_key=?', (guild_id, round_number, quiz_key)).fetchone()
        if not item:
            return themed_embed('📜 Previous Quiz Results', 'No completed rounds found yet.')
        rid, played_at, count, saved_quiz_key = item
        rows = conn.execute('SELECT name, score, placement FROM results WHERE round_id=? ORDER BY placement', (rid,)).fetchall()
    medals = ['🥇', '🥈', '🥉']
    lines = [f'{medals[place-1] if place <= 3 else str(place)+"."} **{name}** — **{score:,} pts**'
             for name, score, place in rows[:50]]
    result = themed_embed(f'{QUIZ_NAMES.get(saved_quiz_key, saved_quiz_key)} | Round #{rid}', '\n'.join(lines) or 'No scores', GOLD)
    result.add_field(name='Completed (UTC)', value=played_at.replace('T', ' ')[:16], inline=True)
    result.add_field(name='Players', value=str(count), inline=True)
    return result


def records_embed(guild_id, quiz_key):
    with db_connect() as conn:
        rows = conn.execute("""SELECT user_id, MAX(name), SUM(score), COUNT(*), MAX(score)
                               FROM results JOIN rounds ON rounds.id = results.round_id
                               WHERE rounds.guild_id=? AND rounds.quiz_key=? GROUP BY user_id ORDER BY SUM(score) DESC LIMIT 15""", (guild_id, quiz_key)).fetchall()
    lines = [f'**{i}. {name}** — **{total:,} pts** · {games_played} games · best {best:,}'
             for i, (_, name, total, games_played, best) in enumerate(rows, 1)]
    return themed_embed(f'{QUIZ_NAMES[quiz_key]} | Records', '\n'.join(lines) or 'No completed games yet.', GOLD)



def load_questions(quiz_key):
    path = QUESTION_DIR / f'{quiz_key}.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    valid = []
    for item in data:
        if (isinstance(item, dict) and isinstance(item.get('question'), str)
                and isinstance(item.get('options'), list) and len(item['options']) == 4
                and all(isinstance(x, str) and 0 < len(x) <= 80 for x in item['options'])
                and isinstance(item.get('answer'), int) and 0 <= item['answer'] < 4):
            valid.append(item)
    return valid


def load_tiebreaker_questions(quiz_key):
    path = QUESTION_DIR / f'{quiz_key}_tiebreakers.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    valid = []
    for item in data:
        if (isinstance(item, dict) and isinstance(item.get('question'), str)
                and isinstance(item.get('options'), list) and len(item['options']) == 4
                and all(isinstance(x, str) and 0 < len(x) <= 80 for x in item['options'])
                and isinstance(item.get('answer'), int) and 0 <= item['answer'] < 4):
            valid.append(item)
    if not valid:
        raise ValueError(f'No valid tiebreaker questions in {path.name}')
    return valid



def host_only(interaction):
    return interaction.guild is not None and interaction.user.id in HOST_IDS


# Discord embeds support one accent colour, not a custom background or font.
# Use the same restrained silver/navy styling throughout the quiz.
SILVER = discord.Color.from_rgb(158, 175, 195)
GOLD = discord.Color.from_rgb(194, 165, 105)


def themed_embed(title, description='', color=None):
    e = discord.Embed(title=title, description=description, color=color or SILVER)
    e.set_author(name='SECRET SERVICE  |  KNOWLEDGE SHOWDOWN')
    e.set_footer(text='Secret Service Knowledge Showdown  •  Official Quiz')
    return e


def embed(title, description='', color=SILVER):
    return themed_embed(title, description, color)


# Compact question embeds: no full-width banners or fixed question numbers.
ROUND_STYLE = {
    'secret_service': ('🛡️', discord.Color.from_rgb(35, 79, 130)),
    'autumn_thanksgiving': ('🍂', discord.Color.from_rgb(180, 109, 50)),
    'general_knowledge': ('🧠', discord.Color.from_rgb(133, 100, 201)),
}

def compact_question_embed(quiz_key, question, number, total, seconds, tie=False, deadline=None):
    icon, accent = ROUND_STYLE[quiz_key]
    title = (f'⚡ SUDDEN DEATH | QUESTION {number}' if tie
             else f'{icon} {QUIZ_NAMES[quiz_key]} | QUESTION {number:02d} / {total:02d}')
    e = discord.Embed(
        title=title,
        description=f"### {question['question']}\n\n"
                    + '\n'.join(f"**{chr(65+i)}.** {option}" for i, option in enumerate(question['options'])),
        color=accent,
    )
    e.set_author(name='SECRET SERVICE  |  KNOWLEDGE SHOWDOWN')
    if deadline is not None:
        e.add_field(name='Answer deadline', value=f'<t:{deadline}:R> · <t:{deadline}:T> (Discord time)', inline=False)
    e.set_footer(text=f'{seconds} seconds to answer  •  Select one button below  •  Answer locks on click')
    return e


async def send_branded(channel, e, kind='question', quiz_key='secret_service', **kwargs):
    # Retained as a compatibility wrapper; no banners or image attachments.
    e.color = ROUND_STYLE[quiz_key][1]
    return await channel.send(embed=e, **kwargs)


async def respond_branded(interaction, e, kind='leaderboard', quiz_key='secret_service', ephemeral=False):
    e.color = ROUND_STYLE[quiz_key][1]
    return await interaction.response.send_message(embed=e, ephemeral=ephemeral)


async def private_notice(interaction, message, success=False):
    colour = GOLD if success else SILVER
    await interaction.response.send_message(
        embed=themed_embed('Confirmation' if success else 'Quiz Notice', message, colour), ephemeral=True)


def status_embed(title, message, highlight=False):
    colour = GOLD if highlight else SILVER
    return themed_embed(title, message, colour)


def branded_lobby_embed(description, quiz_key='secret_service'):
    icon, colour = ROUND_STYLE[quiz_key]
    return themed_embed(f'{icon} {QUIZ_NAMES[quiz_key]} | Lobby', description, colour)


class LobbyView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(label='Join Quiz', style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        if game.phase != 'lobby':
            return await private_notice(interaction, 'The lobby has closed.', success=False)
        if interaction.user.id in game.players:
            return await private_notice(interaction, 'You have already joined!', success=False)
        if len(game.players) >= MAX_PLAYERS:
            return await private_notice(interaction, 'This quiz is full.', success=False)
        game.players[interaction.user.id] = interaction.user.display_name
        game.scores[interaction.user.id] = 0
        await private_notice(interaction, f'You joined! Players: {len(game.players)}/{MAX_PLAYERS}', success=True)
        await game.refresh_lobby()

    @discord.ui.button(label='Leave Quiz', style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        if game.phase != 'lobby' or interaction.user.id not in game.players:
            return await private_notice(interaction, 'You are not in the open lobby.', success=False)
        game.players.pop(interaction.user.id)
        game.scores.pop(interaction.user.id, None)
        await private_notice(interaction, 'You left the quiz.', success=True)
        await game.refresh_lobby()


    @discord.ui.button(label='Start Now (Host)', style=discord.ButtonStyle.secondary)
    async def start_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        if not host_only(interaction):
            return await private_notice(interaction, 'Only authorised quiz hosts can start early.', success=False)
        if game.phase != 'lobby' or game.lobby_start.is_set():
            return await private_notice(interaction, 'The lobby is already closed.', success=False)
        if len(game.players) < MIN_PLAYERS:
            return await private_notice(interaction, f'At least {MIN_PLAYERS} player(s) must join before starting.', success=False)
        game.lobby_start.set()
        await private_notice(interaction, '🚀 Starting the quiz now!', success=True)


class AnswerView(discord.ui.View):
    def __init__(self, game, question, eligible, seconds):
        super().__init__(timeout=None)
        self.game = game
        self.question = question
        self.eligible = set(eligible)
        self.answers = {}
        # First valid interaction per user: (monotonic arrival ns, interaction id, choice).
        # This measures bot receipt, not the physical click at the player's device.
        self.answer_receipts = {}
        self.started_ns = None
        self.deadline_ns = None
        self.open = True
        self.lock = asyncio.Lock()
        for index, option in enumerate(question['options']):
            button = discord.ui.Button(label=f'{chr(65 + index)}. {option}', style=discord.ButtonStyle.primary, row=index // 2)
            async def callback(interaction, choice=index):
                # Timestamp before acquiring the lock or performing any Discord API calls.
                received_ns = time.monotonic_ns()
                async with self.lock:
                    if self.game.paused:
                        notice = ('The quiz is paused. Please wait for the host to resume.', False)
                    elif (not self.open or self.game.phase == 'ended'
                            or self.started_ns is None or received_ns >= self.deadline_ns):
                        notice = ('This question is closed.', False)
                    elif interaction.user.id not in self.eligible:
                        notice = ('You are not eligible for this question.', False)
                    elif interaction.user.id in self.answers:
                        notice = ('Your answer is already locked.', False)
                    else:
                        self.answers[interaction.user.id] = choice
                        self.answer_receipts[interaction.user.id] = (received_ns, interaction.id, choice)
                        notice = None
                # Acknowledge valid answers privately, without posting to the game channel.
                # Keep Discord API calls outside the lock for fair receipt timestamps.
                if notice is None:
                    await private_notice(interaction, 'Your answer is locked in!', success=True)
                else:
                    await private_notice(interaction, notice[0], success=notice[1])
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
    def __init__(self, channel, host_id, questions, quiz_key):
        self.channel = channel
        self.quiz_key = quiz_key
        self.host_id = host_id
        self.questions = questions
        self.players = {}
        self.scores = {}
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
        self.current_question = None
        self.current_question_number = None
        self.pause_started_ns = None
        self.pause_reason = None
        self.question_sent_at = None
        self.question_lock = asyncio.Lock()
        self.replacement_question = None
        self.replaced_question_ids = set()
        self.used_replacement_ids = set()

    async def refresh_lobby(self):
        if self.lobby_message and self.phase == 'lobby':
            try:
                await self.lobby_message.edit(embed=branded_lobby_embed(
                    f'**Players:** {len(self.players)}/{MAX_PLAYERS}\n**Questions:** {QUESTION_COUNT}\n'
                    f'**Answer time:** {ANSWER_SECONDS}s\n**Lobby:** {LOBBY_SECONDS // 60} minutes (or host starts early)\n**Minimum players:** {MIN_PLAYERS}\n\n'
                    'Click **Join Quiz** to enter!', self.quiz_key), view=LobbyView(self))
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
                await self.channel.send(embed=status_embed('Round Cancelled', f'At least **{MIN_PLAYERS}** player(s) required.'))
                return
            self.phase = 'playing'
            if self.lobby_message:
                await self.lobby_message.edit(view=None)
            await self.channel.send(content=f'<@&{SS_QUIZ_ROLE_ID}>', allowed_mentions=ROLE_MENTIONS,
                                    embed=themed_embed('The Showdown Begins', f'**{QUIZ_NAMES[self.quiz_key]}**\n**{len(self.players)}** competitors are ready.\n\n**25 questions**  •  **15 seconds each**  •  **Speed scoring: 4 / 3 / 2 / 1 pts**', ROUND_STYLE[self.quiz_key][1]))
            for i, question in enumerate(self.questions, 1):
                if self.phase == 'ended':
                    return
                await self.pause_gate.wait()
                self.round_number = i
                while True:
                    await self.pause_gate.wait()
                    outcome = await self.ask(question, i, False, list(self.players))
                    if outcome != 'REPLACED':
                        break
                    question = self.replacement_question
                    self.replacement_question = None
                    self.questions[i - 1] = question
                    await self.channel.send(embed=status_embed('Replacement Question',
                        f'Question {i} has been replaced. The previous question is void and awards no points.', True))
                if self.phase == 'ended':
                    return
                if not await self.wait_or_end(INTERMISSION_SECONDS):
                    return
                if i % 5 == 0:
                    await send_branded(self.channel, self.leaderboard(), 'leaderboard', quiz_key=self.quiz_key)
            await self.pause_gate.wait()
            await self.finish()
        except asyncio.CancelledError:
            raise
        except Exception:
            import traceback
            traceback.print_exc()
            await self.channel.send(embed=status_embed('Round Interrupted', 'An unexpected error occurred. Please ask the host to check the bot logs.'))
        finally:
            self.phase = 'ended'
            if games.get(self.channel.guild.id) is self:
                games.pop(self.channel.guild.id, None)

    async def ask(self, question, number, tie, eligible):
        self.phase = 'tiebreaker' if tie else 'playing'
        seconds = ANSWER_SECONDS
        view = AnswerView(self, question, eligible, seconds)
        self.active_view = view
        # No question banner: a lightweight, round-coloured Discord embed.
        # The question number is generated dynamically for every question.
        question_embed = compact_question_embed(
            self.quiz_key, question, number, len(self.questions), seconds, tie=tie
        )
        # Deadline is established immediately before sending; allow up to one second
        # for delivery so the visible timestamp does not overpromise the answer window.
        visible_deadline = math.ceil(time.time() + seconds + 1)
        question_embed = compact_question_embed(self.quiz_key, question, number, len(self.questions), seconds, tie=tie, deadline=visible_deadline)
        # A pause can arrive while Discord is still delivering the question.
        # Do not start the answer clock until delivery and any pause have finished.
        await self.pause_gate.wait()
        message = await self.channel.send(embed=question_embed, view=view)
        self.current_question = question
        self.current_question_number = number
        self.question_sent_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.active_message = message
        # Begin the countdown after Discord confirms the question message was sent.
        await self.pause_gate.wait()
        view.started_ns = time.monotonic_ns()
        view.deadline_ns = view.started_ns + seconds * 1_000_000_000
        # If pause happened during the send, wait for resume before counting.
        # The resume command adjusts the deadline only if a timer already exists.
        # Freeze the *actual* answer deadline when paused, not just the interval
        # between questions. This also keeps existing accepted answers intact.
        while True:
            if self.phase == 'ended' or self.replacement_question is not None:
                break
            if self.paused:
                await asyncio.sleep(0.1)
                continue
            if time.monotonic_ns() >= view.deadline_ns:
                break
            await asyncio.sleep(min(0.15, max(0.01, (view.deadline_ns - time.monotonic_ns()) / 1e9)))
        # Resume may repost the question; close the currently visible message.
        await view.close(self.active_message or message)
        if self.phase == 'ended':
            return set()
        if self.replacement_question is not None:
            self.active_view = None
            self.active_message = None
            self.current_question = None
            return 'REPLACED'
        correct = {uid for uid, answer in view.answers.items() if answer == question['answer']}
        # Sort by receipt time, then Discord's unique interaction ID for stable ties.
        correct_in_order = sorted(correct, key=lambda uid: view.answer_receipts[uid][:2])
        if not tie:
            for place, uid in enumerate(correct_in_order):
                points = (4, 3, 2)[place] if place < 3 else 1
                self.scores[uid] += points

        letter = chr(65 + question['answer'])
        scoring_text = ('' if tie else
            '\n**Scoring:** Fastest correct = 4 pts, second = 3, third = 2, remaining = 1.')
        await self.channel.send(embed=themed_embed('Answer Revealed',
            f"**Correct answer:** {letter}. {question['options'][question['answer']]}\n"
            f"**Correct players:** {len(correct)}/{len(eligible)}"
            f"{scoring_text}", ROUND_STYLE[self.quiz_key][1]))
        self.active_view = None
        self.active_message = None
        self.current_question = None
        return correct

    def leaderboard(self):
        ranked = sorted(self.scores, key=lambda uid: (-self.scores[uid], self.players[uid].lower()))
        lines = [f'**{i}.** {self.players[uid]} — **{self.scores[uid]}** pts'
                 for i, uid in enumerate(ranked[:10], 1)]
        return themed_embed(f'{QUIZ_NAMES[self.quiz_key]} | Top 10', '\n'.join(lines) or 'No players yet.', ROUND_STYLE[self.quiz_key][1])

    async def finish(self):
        if self.phase == 'ended':
            return
        await self.channel.send(embed=status_embed('Round Complete', 'All questions are finished. Calculating final placements and resolving any podium ties…', True))
        ranked_scores = sorted(set(self.scores.values()), reverse=True)
        # Resolve ties affecting the top three places; unrelated ties retain shared ranks.
        final_order = []
        # Each category has its own dedicated tiebreaker bank.
        if self.quiz_key in ('general_knowledge', 'autumn_thanksgiving', 'secret_service'):
            tie_pool = load_tiebreaker_questions(self.quiz_key)
        else:
            tie_pool = load_questions(self.quiz_key).copy()
        with db_connect() as conn:
            used = {r[0] for r in conn.execute('SELECT question_id FROM used_questions WHERE guild_id=?', (self.channel.guild.id,))}
        tie_pool = [q for q in tie_pool if question_id(q) not in self.used_replacement_ids]
        if self.quiz_key in ('general_knowledge', 'autumn_thanksgiving', 'secret_service'):
            main_ids = {question_id(q) for q in self.questions}
            tie_pool = [q for q in tie_pool if question_id(q) not in main_ids]
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
        # Publish immediately; the separate approval commands have been removed.
        if self.phase == 'ended':
            return
        save_round(self, final_order)
        await send_branded(self.channel, embed(f'{QUIZ_NAMES[self.quiz_key]} | Final Standings', '\n'.join(lines), GOLD), 'results', quiz_key=self.quiz_key)

    async def resolve_tie(self, tied, score, pool):
        mentions = ' '.join(f'<@{uid}>' for uid in tied)
        await self.channel.send(
            content=mentions,
            embed=status_embed('Sudden Death | Tiebreaker',
                f'Competitors are tied at **{score:,} points**.\n\n**Preparation:** {TIE_READY_SECONDS // 60} minutes\n**Answer window:** {ANSWER_SECONDS} seconds', True),
            allowed_mentions=discord.AllowedMentions(users=True))
        if not await self.wait_or_end(TIE_READY_SECONDS):
            return tied
        survivors = list(tied)
        eliminated = []
        round_no = 0
        while len(survivors) > 1 and self.phase != 'ended':
            if not pool:
                await self.channel.send(embed=status_embed('Tiebreaker Ended', 'No unused tiebreaker questions remain. The host can resolve the remaining tie manually.'))
                break
            round_no += 1
            await self.pause_gate.wait()
            tie_question = pool.pop(0)
            mark_used(self.channel.guild.id, [tie_question])
            correct = await self.ask(tie_question, round_no, True, survivors)
            if not correct:
                await self.channel.send(embed=status_embed('Sudden Death | No Eliminations', 'No correct answers. All remaining competitors advance.'))
            elif len(correct) == len(survivors):
                await self.channel.send(embed=status_embed('Sudden Death | No Eliminations', 'All competitors answered correctly. Another question follows.'))
            else:
                eliminated_now = [uid for uid in survivors if uid not in correct]
                eliminated = eliminated_now + eliminated
                survivors = [uid for uid in survivors if uid in correct]
                await self.channel.send(embed=status_embed('Sudden Death | Still In', ', '.join(self.players[uid] for uid in survivors)))
            if len(survivors) > 1 and not await self.wait_or_end(INTERMISSION_SECONDS):
                break
        return survivors + eliminated


class QuizSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Secret Service Trivia', value='secret_service', emoji='🛡️', description='25 SS policy and agency questions'),
            discord.SelectOption(label='Autumn & Thanksgiving Trivia', value='autumn_thanksgiving', emoji='🍂', description='25 seasonal and Thanksgiving questions'),
            discord.SelectOption(label='General Knowledge Trivia', value='general_knowledge', emoji='🧠', description='25 mixed general knowledge questions'),
        ]
        super().__init__(placeholder='Choose a quiz to host...', options=options)

    async def callback(self, interaction: discord.Interaction):
        if not host_only(interaction):
            return await private_notice(interaction, 'Only hosts can start a quiz.')
        guild_id = interaction.guild.id
        if guild_id in games:
            return await private_notice(interaction, 'A quiz is already active in this server.')
        quiz_key = self.values[0]
        try:
            bank = load_questions(quiz_key)
            chosen = select_questions(guild_id, bank)
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return await private_notice(interaction, f'Question bank error: {exc}')
        game = Game(interaction.channel, interaction.user.id, chosen, quiz_key)
        games[guild_id] = game
        await private_notice(interaction, f'{QUIZ_NAMES[quiz_key]} lobby created!', success=True)
        game.lobby_message = await send_branded(interaction.channel, branded_lobby_embed(
            f'**Theme:** {QUIZ_NAMES[quiz_key]}\n**Players:** 0/{MAX_PLAYERS}\n**Questions:** {QUESTION_COUNT}\n'
            f'**Answer time:** {ANSWER_SECONDS}s\n**Lobby:** {LOBBY_SECONDS // 60} minutes (or host starts early)\n\n'
            'Click **Join Quiz** to enter!', quiz_key), 'lobby', quiz_key=quiz_key, view=LobbyView(game),
            content=f'<@&{SS_QUIZ_ROLE_ID}> — **{QUIZ_NAMES[quiz_key]}** lobby is open! Join now.',
            allowed_mentions=ROLE_MENTIONS)
        game.task = asyncio.create_task(game.run())


class QuizSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(QuizSelect())


class ThemeSelect(discord.ui.Select):
    def __init__(self, mode, round_number=None):
        self.mode = mode
        self.round_number = round_number
        super().__init__(placeholder='Select a quiz leaderboard...', options=[
            discord.SelectOption(label=name, value=key) for key, name in QUIZ_NAMES.items()
        ])

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        if self.mode == 'records':
            e = records_embed(interaction.guild.id, key)
        else:
            e = past_round_embed(interaction.guild.id, self.round_number, key)
        await respond_branded(interaction, e, 'leaderboard' if self.mode == 'records' else 'results', quiz_key=key, ephemeral=True)


class ThemeSelectView(discord.ui.View):
    def __init__(self, mode, round_number=None):
        super().__init__(timeout=180)
        self.add_item(ThemeSelect(mode, round_number))


class ReplacementSelect(discord.ui.Select):
    def __init__(self, game, candidates):
        self.game = game
        self.candidates = candidates
        options = [discord.SelectOption(label=f'{i + 1}. {q["question"][:80]}', value=str(i),
                                        description='Select to void the active question')
                   for i, q in enumerate(candidates)]
        super().__init__(placeholder='Choose a replacement from the tiebreaker bank', options=options)

    async def callback(self, interaction: discord.Interaction):
        game = self.game
        if not host_only(interaction):
            return await private_notice(interaction, 'Only authorised hosts can replace questions.')
        async with game.question_lock:
            if (games.get(interaction.guild.id) is not game or not game.paused
                    or game.phase != 'playing' or not game.active_view or not game.active_view.open
                    or game.replacement_question is not None):
                return await private_notice(interaction, 'No paused active question is available for replacement.')
            chosen = self.candidates[int(self.values[0])]
            chosen_id = question_id(chosen)
            if chosen_id in game.used_replacement_ids:
                return await private_notice(interaction, 'That replacement was already selected. Please reopen the menu.')
            old_id = question_id(game.current_question)
            game.replaced_question_ids.add(old_id)
            game.used_replacement_ids.add(chosen_id)
            with db_connect() as conn:
                conn.execute('INSERT OR IGNORE INTO redundant_questions VALUES (?, ?)',
                             (interaction.guild.id, old_id))
            game.replacement_question = chosen
            # The old question's answer receipts are discarded, never scored.
        await private_notice(interaction,
            'Replacement selected. The original question is void and will not score. Use /quiz resume to present the replacement.', True)


class ReplacementSelectView(discord.ui.View):
    def __init__(self, game, candidates):
        super().__init__(timeout=180)
        self.add_item(ReplacementSelect(game, candidates))


@bot.tree.command(name='quiz', description='SS quiz host commands')
@app_commands.describe(action='Choose a quiz action')
@app_commands.choices(action=[app_commands.Choice(name=x, value=x) for x in ('start', 'pause', 'resume', 'replace', 'leaderboard', 'end')])
async def quiz(interaction: discord.Interaction, action: app_commands.Choice[str]):
    if not interaction.guild:
        return await private_notice(interaction, 'Use this command in a server.', success=False)
    game = games.get(interaction.guild.id)
    command = action.value
    if command == 'leaderboard':
        if game:
            return await respond_branded(interaction, game.leaderboard(), 'leaderboard', quiz_key=game.quiz_key)
        return await interaction.response.send_message('Choose a quiz to view its leaderboard:', view=ThemeSelectView('records'), ephemeral=True)
    if not host_only(interaction):
        return await private_notice(interaction, 'Only the two authorised quiz hosts can manage quizzes.', success=False)
    if command == 'start':
        if game:
            return await private_notice(interaction, 'A quiz is already running in this server.', success=False)
        return await interaction.response.send_message('Select which quiz you want to host:', view=QuizSelectView(), ephemeral=True)
    if not game:
        return await private_notice(interaction, 'No quiz is running.', success=False)
    if command == 'end':
        game.phase = 'ended'
        game.pause_gate.set()
        if game.active_view and game.active_message:
            await game.active_view.close(game.active_message)
        if game.task:
            game.task.cancel()
        games.pop(interaction.guild.id, None)
        return await private_notice(interaction, 'Quiz ended by the host.', success=True)
    if command == 'replace':
        if not game.paused or game.phase != 'playing' or not game.active_view or not game.active_view.open:
            return await private_notice(interaction, 'Pause during an active main-round question before replacing it.')
        if game.replacement_question is not None:
            return await private_notice(interaction, 'A replacement has already been selected. Resume the quiz.')
        try:
            bank = load_tiebreaker_questions(game.quiz_key)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return await private_notice(interaction, f'Tiebreaker bank unavailable: {exc}')
        excluded = ({question_id(q) for q in game.questions} | game.used_replacement_ids |
                    game.replaced_question_ids)
        with db_connect() as conn:
            excluded.update(r[0] for r in conn.execute(
                'SELECT question_id FROM redundant_questions WHERE guild_id=?', (interaction.guild.id,)))
        candidates = [q for q in bank if question_id(q) not in excluded]
        if not candidates:
            return await private_notice(interaction, 'No unused replacement questions remain in the bank.')
        return await interaction.response.send_message(
            'Choose the replacement question. This permanently voids the current question and discards its answers.',
            view=ReplacementSelectView(game, candidates[:25]), ephemeral=True)
    if command == 'pause':
        if game.paused:
            return await private_notice(interaction, 'The quiz is already paused.', success=False)
        game.paused = True
        game.pause_started_ns = time.monotonic_ns()
        game.pause_gate.clear()
        await game.channel.send(embed=status_embed('⏸️ Quiz Paused',
            'The host has paused the quiz for review or a player disconnection. '
            'The question timer is frozen and new answers are temporarily disabled. '
            'Please wait for the host to resume.', True))
        return await private_notice(interaction, 'Quiz paused. The active question timer is frozen.', success=True)
    if command == 'resume':
        if not game.paused:
            return await private_notice(interaction, 'The quiz is not paused.', success=False)
        # A replacement is queued for the question loop; never repost the void question.
        replacing = game.replacement_question is not None
        active = game.active_view
        live_question = bool(not replacing and active and active.open and game.active_message
                             and game.current_question is not None)
        # Acknowledge the command immediately; Discord message operations can take
        # longer than the three-second interaction response window.
        await interaction.response.defer(ephemeral=True)
        try:
            if live_question:
                # Remove the old interactive buttons. Reusing the existing message
                # causes clients to display a stale question/countdown on resume.
                old_message = game.active_message
                await old_message.edit(view=None)
                await game.channel.send(embed=status_embed('▶️ Quiz Resuming',
                    'A fresh copy of the question is being posted. Wait for the new answer buttons.', True))
                new_embed = compact_question_embed(
                    game.quiz_key, game.current_question, game.current_question_number,
                    len(game.questions), ANSWER_SECONDS,
                    tie=game.phase == 'tiebreaker', deadline=None)
                new_message = await game.channel.send(embed=new_embed, view=active)
                # The timer is NOT restarted until Discord acknowledges delivery
                # of the new question. A short grace period gives clients time to render.
                game.active_message = new_message
                now_ns = time.monotonic_ns()
                active.started_ns = now_ns
                active.deadline_ns = now_ns + (ANSWER_SECONDS + 3) * 1_000_000_000
                # Show a countdown for the real deadline, not the earlier send time.
                deadline = math.ceil(time.time() + ANSWER_SECONDS + 3)
                try:
                    await new_message.edit(embed=compact_question_embed(
                        game.quiz_key, game.current_question, game.current_question_number,
                        len(game.questions), ANSWER_SECONDS + 3,
                        tie=game.phase == 'tiebreaker', deadline=deadline))
                except discord.HTTPException:
                    pass
            else:
                await game.channel.send(embed=status_embed('▶️ Quiz Resumed',
                    'The quiz is continuing.', True))
            game.pause_started_ns = None
            game.paused = False
            game.pause_gate.set()
            await interaction.followup.send(
                'Quiz resumed. ' + ('A fresh question message has been posted with an 18-second window.'
                                    if live_question else 'The quiz will continue.'), ephemeral=True)
        except discord.HTTPException as exc:
            # Keep the quiz safely paused instead of silently consuming time.
            await interaction.followup.send(
                f'Could not repost the question; quiz remains paused. Discord error: {exc}', ephemeral=True)
        return


# Operational safeguards and event-day controls.





# Event-day controls: host-only, no extra public messages except cancellation notices.



@bot.tree.command(name='quiz-role-panel', description='Post the Secret Service quiz reaction-role announcement')
async def quiz_role_panel(interaction: discord.Interaction):
    if not host_only(interaction):
        return await private_notice(interaction, 'Only authorised quiz hosts can post the role panel.')
    guild = interaction.guild
    role = guild.get_role(SS_QUIZ_ROLE_ID)
    if role is None:
        return await private_notice(interaction, 'The SS Quiz role was not found in this server.')
    me = guild.me
    if role.managed:
        return await private_notice(interaction, 'The SS Quiz role is integration-managed. Please use a regular server role.')
    if me is None or not me.guild_permissions.manage_roles or role >= me.top_role:
        return await private_notice(interaction, 'Give the bot Manage Roles and move its highest role above SS Quiz.')
    await interaction.response.defer(ephemeral=True)
    announcement = themed_embed(
        '🧠 SECRET SERVICE KNOWLEDGE SHOWDOWN',
        '**Think you have what it takes to become our next Quiz Champion?** 🏆\n\n'
        'Join Halfeti & Gumdrop for three exciting rounds of trivia, with prizes '
        'up for grabs in **each round**!\n\n'
        '📅 **Date:** Monday, 9th November 2026\n'
        '🕑 **Time:** 2:00 PM GMT\n'
        '🎤 **Hosts:** Halfeti & Gumdrop\n\n'
        '**🎯 THE THREE ROUNDS**\n'
        '1️⃣ Secret Service Trivia\n'
        '2️⃣ Autumn & Thanksgiving Trivia\n'
        '3️⃣ General Knowledge Trivia\n\n'
        '**🏆 PRIZES — AWARDED IN EACH ROUND**\n'
        '🥇 **1st Place:** 100c + HC Box\n'
        '🥈 **2nd Place:** 50c\n'
        '🥉 **3rd Place:** 25c\n\n'
        '**🔓 HOW TO PARTICIPATE**\n'
        f'React with {QUIZ_ROLE_EMOJI} below to receive the **@SS Quiz** role and unlock '
        'access to our dedicated quiz channels!\n\n'
        'You will also be notified when the quiz lobby opens and when the game begins.\n\n'
        '**React now to get access and prepare for the Showdown!** 🏆\n\n'
        f'*Remove your {QUIZ_ROLE_EMOJI} reaction to remove the role.*',
        GOLD)
    try:
        message = await interaction.channel.send(embed=announcement)
        with db_connect() as conn:
            conn.execute('INSERT INTO quiz_role_panels (message_id, guild_id, channel_id) VALUES (?, ?, ?)',
                         (message.id, guild.id, interaction.channel.id))
        await message.add_reaction(QUIZ_ROLE_EMOJI)
    except discord.HTTPException:
        await interaction.followup.send('Could not post the panel or add the reaction. Check Send Messages, Embed Links and Add Reactions permissions.', ephemeral=True)
        return
    await interaction.followup.send(f'Quiz role panel posted: {message.jump_url}', ephemeral=True)


def is_quiz_role_panel(message_id, guild_id):
    with db_connect() as conn:
        return conn.execute('SELECT 1 FROM quiz_role_panels WHERE message_id=? AND guild_id=?',
                            (message_id, guild_id)).fetchone() is not None


async def update_quiz_role(payload, add):
    if payload.guild_id is None or payload.user_id == bot.user.id or str(payload.emoji) != QUIZ_ROLE_EMOJI:
        return
    if not is_quiz_role_panel(payload.message_id, payload.guild_id):
        return
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    role = guild.get_role(SS_QUIZ_ROLE_ID)
    if role is None:
        return
    try:
        member = guild.get_member(payload.user_id)
        if member is None:
            member = await guild.fetch_member(payload.user_id)
        if member.bot:
            return
        if add:
            await member.add_roles(role, reason='Reacted to the SS Quiz role panel')
        else:
            await member.remove_roles(role, reason='Removed reaction from the SS Quiz role panel')
    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
        print(f'Quiz reaction-role update failed for {payload.user_id}: {exc}')


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await update_quiz_role(payload, True)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    await update_quiz_role(payload, False)


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')


async def setup_hook():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        # Also remove obsolete global commands from older deployments.
        await bot.tree.sync()
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
