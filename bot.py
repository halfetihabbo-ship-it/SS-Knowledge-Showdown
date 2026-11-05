"""The Forbidden Forest — standalone discord.py 2.5+ event bot.

All authoritative decisions are server-side. Run ONE bot instance against ONE SQLite
file on persistent storage. See README for setup and limitations.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import os
import random
import secrets
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
CHANNEL_ID = int(os.getenv("EVENT_CHANNEL_ID", "0"))
ROLE_ID = int(os.getenv("ROLE_ID", "1567982021106602065"))
HOST_IDS = {int(x.strip()) for x in os.getenv("HOST_IDS", "1501587787294703657").split(",") if x.strip()}
DB_PATH = os.getenv("DATABASE_PATH", "./forest.sqlite3")
START = dt.datetime(2026, 11, 8, 0, 0, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 11, 11, 0, 0, tzinfo=dt.timezone.utc)
BONUS_HOURS = 24
MAIN_ROUNDS = 40
BONUS_ROUNDS = 5
UTC = dt.timezone.utc

intents = discord.Intents.default()
intents.members = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)
forest = app_commands.Group(name="forest", description="The Forbidden Forest event")
lock = asyncio.Lock()
db: sqlite3.Connection

TYPES = [
    ("Chest", "🧰", "A weathered chest sits beneath fallen leaves."),
    ("Ruins", "🏛️", "Ancient ruins conceal an unclaimed relic."),
    ("Merchant", "🧙", "A wandering merchant offers a mysterious bargain."),
    ("Shrine", "🔮", "A forgotten shrine hums with strange energy."),
    ("Ravine", "🌉", "A narrow crossing leads to glittering treasure."),
    ("Grove", "🍄", "A glowing grove holds an unusual discovery."),
    ("Storm", "⛈️", "A sudden storm uncovers an old cache."),
    ("Cavern", "🕯️", "A dark cavern promises valuable finds."),
]
ARTIFACTS = [("Bronze Acorn", 25), ("Silver Maple Leaf", 50),
             ("Enchanted Mushroom", 100), ("Golden Compass", 200),
             ("Crown of the Forest", 400)]


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def stamp(t: dt.datetime) -> str:
    return t.isoformat()


def parse(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def init_db() -> None:
    global db
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS players (
      user_id INTEGER PRIMARY KEY, seed TEXT NOT NULL, created TEXT NOT NULL,
      stage TEXT NOT NULL DEFAULT 'main', step INTEGER NOT NULL DEFAULT 0,
      health INTEGER NOT NULL DEFAULT 100, energy INTEGER NOT NULL DEFAULT 100,
      supplies INTEGER NOT NULL DEFAULT 8, points INTEGER NOT NULL DEFAULT 0,
      items INTEGER NOT NULL DEFAULT 0, artifact_bag TEXT NOT NULL DEFAULT '{}', objectives INTEGER NOT NULL DEFAULT 0,
      spent INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
      main_score INTEGER, main_items INTEGER, main_health INTEGER, main_energy INTEGER,
      bonus_score INTEGER, bonus_objectives INTEGER, bonus_health INTEGER, bonus_spent INTEGER,
      bonus_status TEXT NOT NULL DEFAULT 'none', last_action TEXT
    );
    CREATE TABLE IF NOT EXISTS logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, user_id INTEGER,
      event TEXT NOT NULL, detail TEXT NOT NULL
    );
    """)
    if not get_meta("draw_secret"):
        set_meta("draw_secret", secrets.token_hex(32))
    if not get_meta("draw_commit"):
        set_meta("draw_commit", hashlib.sha256(get_meta("draw_secret").encode()).hexdigest())


def get_meta(k: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
    return row[0] if row else None


def set_meta(k: str, v: str) -> None:
    db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))


def log(uid: int | None, event: str, detail: str) -> None:
    db.execute("INSERT INTO logs(at,user_id,event,detail) VALUES(?,?,?,?)", (stamp(now()), uid, event, detail[:1800]))


def player(uid: int):
    return db.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone()


def rng_for(seed: str, stage: str, step: int) -> random.Random:
    h = hashlib.sha256(f"{seed}|{stage}|{step}".encode()).digest()
    return random.Random(int.from_bytes(h, "big"))


def encounter(p):
    stage = p["stage"]
    step = p["step"]
    r = rng_for(p["seed"], stage, step)
    # Each main adventure contains 5 occurrences of each of 8 encounter types,
    # with independently shuffled order; bonus has 5 varied encounters.
    if stage == "main":
        order = list(range(8)) * 5
        random.Random(int.from_bytes(hashlib.sha256((p["seed"] + "order").encode()).digest(), "big")).shuffle(order)
        typ = order[step]
    else:
        typ = r.randrange(8)
    name, icon, description = TYPES[typ]
    tier = min(4, (step // 9) if stage == "main" else 3)
    # Each encounter offers the same fixed reward and risk menu. Variation in
    # events/order and predetermined risk rolls makes adventures individual.
    base = [25, 50, 75, 100, 125][tier]
    flavor = r.choice(["A distant owl watches you.", "Footprints vanish into the mist.",
                       "The trees whisper around you.", "A cold wind carries falling leaves."])
    return name, icon, description + " " + flavor, base


def choices():
    return [("Careful", "Guaranteed smaller reward; costs 8 energy.", 0),
            ("Explore", "Moderate reward; 75% success; costs 15 energy.", 1),
            ("Gamble", "Large reward; 45% success; costs 22 energy.", 2)]


def finish_main(uid: int, escaped: bool, defeated: bool = False) -> None:
    p = player(uid)
    if not p or p["status"] != "active" or p["stage"] != "main":
        return
    # Defeat loses half unbanked points; voluntarily escaping secures all points.
    score = p["points"] // 2 if defeated else p["points"]
    status = "defeated" if defeated else ("escaped" if escaped else "completed")
    db.execute("""UPDATE players SET status=?, main_score=?, main_items=?, main_health=?,
       main_energy=?, last_action=? WHERE user_id=?""",
       (status, score, p["items"], p["health"], p["energy"], stamp(now()), uid))
    log(uid, "MAIN_FINISHED", f"status={status} score={score} encounters={p['step']}")


def finish_bonus(uid: int, forfeited: bool = False) -> None:
    p = player(uid)
    if not p or p["bonus_status"] != "active":
        return
    score = -1 if forfeited else p["points"]
    db.execute("""UPDATE players SET bonus_status=?, bonus_score=?, bonus_objectives=?,
       bonus_health=?, bonus_spent=?, last_action=? WHERE user_id=?""",
       ("forfeited" if forfeited else "finished", score, p["objectives"], p["health"], p["spent"], stamp(now()), uid))
    log(uid, "BONUS_FINISHED", f"score={score} forfeited={forfeited}")


def main_key(p):
    return (p["main_score"] if p["main_score"] is not None else -1,
            p["main_items"] or 0, p["main_health"] or 0, p["main_energy"] or 0)


def bonus_key(p):
    return (p["bonus_score"] if p["bonus_score"] is not None else -1,
            p["bonus_objectives"] or 0, p["bonus_health"] or 0,
            -(p["bonus_spent"] or 0))


def competitors():
    return db.execute("SELECT * FROM players WHERE main_score IS NOT NULL").fetchall()


def prize_ties(rows):
    groups = {}
    for p in rows:
        groups.setdefault(main_key(p), []).append(p)
    ordered = sorted(groups, reverse=True)
    eligible = set()
    rank = 1
    for key in ordered:
        group = groups[key]
        if len(group) > 1 and rank <= 3:
            eligible.update(p["user_id"] for p in group)
        rank += len(group)
    return eligible


def final_order():
    rows = competitors()
    secret = get_meta("draw_secret")
    def draw(uid):
        return int(hashlib.sha256(f"{secret}|{uid}".encode()).hexdigest(), 16)
    return sorted(rows, key=lambda p: (main_key(p), bonus_key(p), draw(p["user_id"])), reverse=True)


def game_phase():
    if now() < START:
        return "upcoming"
    if now() < END:
        return "open"
    if get_meta("finalized"):
        return "final"
    if get_meta("bonus_deadline"):
        return "bonus"
    return "closing"


def embed(title: str, body: str, color=0x9B592E):
    return discord.Embed(title=title, description=body, color=color, timestamp=now())


def state_embed(p):
    if p["stage"] == "main" and p["status"] != "active":
        return embed("🍁 Adventure finished", f"**Result:** {p['status']}\n**Secured artefact points:** {p['main_score']}\n**Encounters:** {p['step']}/40\nYour score is hidden from other players until the event closes.")
    if p["stage"] == "bonus" and p["bonus_status"] != "active":
        return embed("🏆 Bonus expedition finished", "Your tiebreaker results have been recorded. Wait for the final announcement.")
    name, icon, description, base = encounter(p)
    maximum = BONUS_ROUNDS if p["stage"] == "bonus" else MAIN_ROUNDS
    title = f"{icon} {name} — Encounter {p['step'] + 1}/{maximum}"
    body = (f"{description}\n\n**Health:** {p['health']} | **Energy:** {p['energy']} | **Supplies:** {p['supplies']}\n"
            f"**Unbanked artefact points:** {p['points']} | **Artefacts:** {p['items']}\n\n"
            f"**Careful:** +{base} pts guaranteed; −8 energy\n"
            f"**Explore:** +{base * 2} pts on success; 75% chance; −15 energy\n"
            f"**Gamble:** +{base * 4} pts on success; 45% chance; −22 energy\n\n"
            "Failed attempts lose 15 or 30 health respectively. Energy replenishes by 12 after each encounter (up to 100). "
            "If energy is too low, you automatically rest before acting (costs 1 supply if available).")
    return embed(title, body)


class AdventureView(discord.ui.View):
    def __init__(self, p):
        super().__init__(timeout=900)
        self.uid = p["user_id"]
        for label, _, number in choices():
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary if number == 0 else discord.ButtonStyle.secondary,
                                       custom_id=f"forest_action_{number}")
            button.callback = self.callback_for(number)
            self.add_item(button)
        if p["stage"] == "main":
            button = discord.ui.Button(label="Escape & Bank", style=discord.ButtonStyle.danger, custom_id="forest_escape")
            button.callback = self.escape_callback
            self.add_item(button)

    def callback_for(self, choice: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.uid:
                return await interaction.response.send_message("This isn't your adventure.", ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            async with lock:
                p = player(self.uid)
                if not p or (p["stage"] == "main" and p["status"] != "active") or (p["stage"] == "bonus" and p["bonus_status"] != "active"):
                    return await interaction.followup.send("This adventure is already finished.", ephemeral=True)
                if (p["stage"] == "main" and now() >= END) or (p["stage"] == "bonus" and now() >= parse(get_meta("bonus_deadline"))):
                    return await interaction.followup.send("The deadline has passed.", ephemeral=True)
                # Reject old or duplicated UI actions: an action is valid only for
                # the encounter rendered in this particular view.
                if p["step"] != self.step or p["stage"] != self.stage:
                    return await interaction.followup.send("That encounter is outdated. Use `/forest resume`.", ephemeral=True)
                db.execute("BEGIN IMMEDIATE")
                try:
                    p = player(self.uid)
                    _, _, _, base = encounter(p)
                    energy = p["energy"]
                    supplies = p["supplies"]
                    cost = [8, 15, 22][choice]
                    rested = False
                    if energy < cost:
                        energy = min(100, energy + 35)
                        if supplies > 0:
                            supplies -= 1
                        rested = True
                    energy = max(0, energy - cost)
                    r = rng_for(p["seed"], p["stage"], p["step"])
                    roll = r.random()
                    success = choice == 0 or roll < [1.0, .75, .45][choice]
                    gain = base * [1, 2, 4][choice] if success else 0
                    damage = 0 if success else [0, 15, 30][choice]
                    health = max(0, p["health"] - damage)
                    # Resource choices are meaningful; all players have the same
                    # expected opportunity distribution and identical action menu.
                    energy = min(100, energy + 12)
                    new_step = p["step"] + 1
                    bag = json.loads(p["artifact_bag"])
                    artifact_name = ARTIFACTS[min(4, (p["step"] // 9) if p["stage"] == "main" else 3)][0]
                    if success:
                        bag[artifact_name] = bag.get(artifact_name, 0) + 1
                    db.execute("""UPDATE players SET step=?, health=?, energy=?, supplies=?, points=points+?,
                      items=items+?, artifact_bag=?, objectives=objectives+?, spent=spent+?, last_action=? WHERE user_id=?""",
                      (new_step, health, energy, supplies, gain, int(success), json.dumps(bag), int(success and choice == 2),
                       cost, stamp(now()), self.uid))
                    log(self.uid, "ENCOUNTER", f"stage={p['stage']} step={new_step} choice={choice} success={success} gain={gain} damage={damage} rest={rested}")
                    if p["stage"] == "main" and (health == 0 or new_step >= MAIN_ROUNDS):
                        finish_main(self.uid, escaped=health > 0, defeated=health == 0)
                    if p["stage"] == "bonus" and (health == 0 or new_step >= BONUS_ROUNDS):
                        finish_bonus(self.uid)
                    db.execute("COMMIT")
                except Exception:
                    db.execute("ROLLBACK")
                    raise
                p = player(self.uid)
            outcome = f"{'Success' if success else 'Failed'}: +{gain} points, −{damage} health."
            if p["stage"] == "main" and p["status"] != "active":
                await interaction.followup.send(outcome, embed=state_embed(p), ephemeral=True)
            elif p["stage"] == "bonus" and p["bonus_status"] != "active":
                await interaction.followup.send(outcome, embed=state_embed(p), ephemeral=True)
            else:
                await interaction.followup.send(outcome, embed=state_embed(p), view=AdventureView(p), ephemeral=True)
        return callback

    async def escape_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.uid:
            return await interaction.response.send_message("This isn't your adventure.", ephemeral=True)
        async with lock:
            p = player(self.uid)
            if not p or p["stage"] != "main" or p["status"] != "active" or now() >= END:
                return await interaction.response.send_message("You cannot escape now.", ephemeral=True)
            if p["step"] != self.step:
                return await interaction.response.send_message("Old encounter. Use `/forest resume`.", ephemeral=True)
            finish_main(self.uid, escaped=True)
            p = player(self.uid)
        await interaction.response.send_message(embed=state_embed(p), ephemeral=True)

    @property
    def step(self):
        return self._step

    @property
    def stage(self):
        return self._stage

    def bind(self, p):
        self._step = p["step"]
        self._stage = p["stage"]
        return self


class StartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Begin / Resume Adventure", emoji="🌲", style=discord.ButtonStyle.success, custom_id="forest_start_v1")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_adventure(interaction)


async def open_adventure(interaction: discord.Interaction):
    if not interaction.guild or interaction.guild.id != GUILD_ID:
        return await interaction.response.send_message("This game is only available in the event server.", ephemeral=True)
    async with lock:
        phase = game_phase()
        p = player(interaction.user.id)
        if phase == "open":
            if not p:
                # Role required to participate. Role panel manages signup.
                member = interaction.user
                if not isinstance(member, discord.Member) or ROLE_ID not in [r.id for r in member.roles]:
                    return await interaction.response.send_message("React 🍁 on the signup panel to receive the SS Quiz role first.", ephemeral=True)
                db.execute("INSERT INTO players(user_id,seed,created) VALUES(?,?,?)",
                           (interaction.user.id, secrets.token_hex(32), stamp(now())))
                log(interaction.user.id, "START", "Main adventure created")
                p = player(interaction.user.id)
        elif phase == "bonus":
            if not p or p["bonus_status"] == "none":
                return await interaction.response.send_message("The main event has closed. Only eligible tied players can enter the bonus expedition.", ephemeral=True)
        else:
            if not p:
                return await interaction.response.send_message("The event isn't accepting new adventures.", ephemeral=True)
        if not p:
            return await interaction.response.send_message("No adventure found.", ephemeral=True)
        if phase != "open" and p["stage"] == "main":
            return await interaction.response.send_message("The main event is closed. Use `/forest leaderboard` when results are announced.", ephemeral=True)
        active = (p["stage"] == "main" and p["status"] == "active") or (p["stage"] == "bonus" and p["bonus_status"] == "active")
        e = state_embed(p)
        v = AdventureView(p).bind(p) if active else None
    await interaction.response.send_message(embed=e, view=v, ephemeral=True)


async def host_only(interaction: discord.Interaction) -> bool:
    if interaction.user.id not in HOST_IDS or not interaction.guild or interaction.guild.id != GUILD_ID:
        await interaction.response.send_message("Host access only.", ephemeral=True)
        return False
    return True


@forest.command(name="help", description="Rules, commands, scoring, prizes and win conditions")
async def help_command(interaction: discord.Interaction):
    e = embed("🌲 The Forbidden Forest — Help", "**8 Nov 2026 00:00 GMT → 11 Nov 2026 00:00 GMT** (72 hours)\n"
              "Explore up to **40 encounters**, collect artefact points and escape with your collection. "
              "One official adventure per Discord account. You can leave and resume before the deadline.")
    e.add_field(name="🎮 Player commands", value=("`/forest help` — This guide\n`/forest play` — Begin or resume\n"
                "`/forest resume` — Continue\n`/forest status` — View progress\n"
                "`/forest inventory` — View artefact totals\n`/forest escape` — Bank and finish early\n"
                "`/forest leaderboard` — Final results after event"), inline=False)
    e.add_field(name="🏆 Prizes", value="🥇 125c + HC Box\n🥈 75c + HC Box\n🥉 50c + HC Box", inline=False)
    e.add_field(name="🎯 Rules and scoring", value=("Choose **Careful** (guaranteed 1× reward), **Explore** (75% for 2×), or **Gamble** (45% for 4×). "
                "Choices cost energy; failures cost health. Energy regenerates after each encounter. "
                "Escape early to secure all points. Health reaching zero ends the run and secures only **50%** of unbanked points. "
                "At encounter 40, surviving players automatically bank their score. Unfinished runs at the deadline are auto-escaped if alive. "
                "Only the highest **secured artefact points** determine the winner."), inline=False)
    e.add_field(name="⚖️ Ties", value=("Compare main artefact points, then artefact count, health and energy. "
                "Prize-position ties remaining after those checks enter **one automatic five-encounter bonus expedition**, "
                "open for 24 hours. Compare bonus score, bonus objectives, health and least energy spent. "
                "If still tied, a precommitted random draw determines order. Missing the bonus deadline forfeits it."), inline=False)
    e.add_field(name="🔒 Fair play", value=("No resets, replay farming, multi-account entries, collaboration or exploiting bugs. "
                "Different encounter orders and risk rolls are generated privately; all players receive the same encounter-type counts and action rules. "
                "Host logs record every decision. Report bugs rather than exploiting them."), inline=False)
    e.add_field(name="🛠️ Host-only commands", value=("`/forest panel` — Publish 🍁 signup panel with @everyone\n"
                "`/forest dashboard` — Participation totals\n`/forest logs` — Recent audit entries\n"
                "`/forest player` — Inspect one player's run\n`/forest audit` — Integrity checks"), inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@forest.command(name="play", description="Start your one official adventure")
async def play(interaction: discord.Interaction):
    await open_adventure(interaction)


@forest.command(name="resume", description="Resume your saved adventure")
async def resume(interaction: discord.Interaction):
    await open_adventure(interaction)


@forest.command(name="status", description="View your current progress privately")
async def status(interaction: discord.Interaction):
    p = player(interaction.user.id)
    if not p:
        return await interaction.response.send_message("No adventure yet. Use `/forest play` after the event opens.", ephemeral=True)
    await interaction.response.send_message(f"Stage: {p['stage']} | Encounters: {p['step']}/{BONUS_ROUNDS if p['stage']=='bonus' else MAIN_ROUNDS} | "
                                            f"Health: {p['health']} | Energy: {p['energy']} | Supplies: {p['supplies']} | "
                                            f"Status: {p['bonus_status'] if p['stage']=='bonus' else p['status']}", ephemeral=True)


@forest.command(name="inventory", description="View your artefact points and collection")
async def inventory(interaction: discord.Interaction):
    p = player(interaction.user.id)
    if not p:
        return await interaction.response.send_message("No adventure yet.", ephemeral=True)
    await interaction.response.send_message(f"Artefacts found: **{p['items']}** | Unbanked points: **{p['points']}** | "
                                            f"Secured main score: **{p['main_score'] if p['main_score'] is not None else 'Not banked'}**\n" + "\n".join(f"{name}: {count}" for name, count in json.loads(p["artifact_bag"]).items()), ephemeral=True)


@forest.command(name="escape", description="Secure artefacts and finish your main adventure early")
async def escape(interaction: discord.Interaction):
    async with lock:
        p = player(interaction.user.id)
        if not p or p["stage"] != "main" or p["status"] != "active" or game_phase() != "open":
            return await interaction.response.send_message("No active main adventure to escape.", ephemeral=True)
        finish_main(interaction.user.id, escaped=True)
        p = player(interaction.user.id)
    await interaction.response.send_message(embed=state_embed(p), ephemeral=True)


@forest.command(name="leaderboard", description="View winners once the competition is final")
async def leaderboard(interaction: discord.Interaction):
    if not get_meta("finalized"):
        return await interaction.response.send_message("Scores are hidden until the event and any tiebreaker are finished.", ephemeral=True)
    rows = final_order()[:10]
    body = "\n".join(f"**{i}.** <@{p['user_id']}> — {p['main_score']} secured points" for i, p in enumerate(rows, 1)) or "No completed adventures."
    await interaction.response.send_message(embed=embed("🏆 Forbidden Forest — Final Standings", body))


@forest.command(name="panel", description="Host: post the signup panel and add 🍁 reaction")
async def panel(interaction: discord.Interaction):
    if not await host_only(interaction):
        return
    if not interaction.channel or not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message("Use this in a text channel.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    e = embed("🍁 THE FORBIDDEN FOREST — SIGN UP", "**@everyone** Venture into the unknown!\n\n"
              "🌲 **40 randomised encounters** per adventurer\n"
              "🗓️ **8 Nov 00:00 GMT – 11 Nov 00:00 GMT**\n"
              "🏆 **1st:** 125c + HC Box | **2nd:** 75c + HC Box | **3rd:** 50c + HC Box\n\n"
              "**React with 🍁 to receive the SS Quiz role** and gain access to the adventure. "
              "Remove your reaction to remove the role. Use `/forest help` for rules.")
    path = Path(__file__).with_name("panel.png")
    file = discord.File(path, filename="panel.png") if path.exists() else None
    if file:
        e.set_image(url="attachment://panel.png")
    # Real @everyone mention must be in message content, not just embed.
    msg = await interaction.channel.send(content="@everyone 🍁 **The Forbidden Forest registration is open!**",
                                         embed=e, file=file, allowed_mentions=discord.AllowedMentions(everyone=True))
    await msg.add_reaction("🍁")
    set_meta("panel_channel", str(msg.channel.id))
    set_meta("panel_message", str(msg.id))
    log(interaction.user.id, "PANEL", f"message={msg.id} channel={msg.channel.id}")
    await interaction.followup.send(f"Signup panel posted: {msg.jump_url}", ephemeral=True)


@forest.command(name="dashboard", description="Host: view event progress and participation")
async def dashboard(interaction: discord.Interaction):
    if not await host_only(interaction):
        return
    total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM players WHERE main_score IS NOT NULL").fetchone()[0]
    eligible = db.execute("SELECT COUNT(*) FROM players WHERE bonus_status!='none'").fetchone()[0]
    await interaction.response.send_message(embed=embed("🛠️ Forest Dashboard", f"Phase: **{game_phase()}**\nRegistered adventures: **{total}**\nCompleted: **{done}**\nTiebreaker players: **{eligible}**\nBonus deadline: {get_meta('bonus_deadline') or 'None'}"), ephemeral=True)


@forest.command(name="logs", description="Host: inspect recent server-side action logs")
@app_commands.describe(limit="Number of recent entries (1–20)")
async def logs(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 20] = 10):
    if not await host_only(interaction):
        return
    rows = db.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    text = "\n".join(f"`{r['id']}` <@{r['user_id']}> **{r['event']}** {r['detail'][:120]}" for r in rows) or "No activity."
    await interaction.response.send_message(embed=embed("📜 Forest Logs", text[:3900]), ephemeral=True)


@forest.command(name="player", description="Host: inspect a player's state and score")
async def inspect_player(interaction: discord.Interaction, member: discord.Member):
    if not await host_only(interaction):
        return
    p = player(member.id)
    if not p:
        return await interaction.response.send_message("No adventure for that player.", ephemeral=True)
    body = (f"User: {member.mention}\nStage: {p['stage']} | Step: {p['step']} | Status: {p['status']}\n"
            f"Main score: {p['main_score']} | Items: {p['main_items']} | HP: {p['main_health']} | Energy: {p['main_energy']}\n"
            f"Bonus: {p['bonus_status']} | Bonus score: {p['bonus_score']}\nLast action: {p['last_action']}")
    await interaction.response.send_message(embed=embed("🔍 Player Inspection", body), ephemeral=True)


@forest.command(name="audit", description="Host: review duplicate actions and database consistency")
async def audit(interaction: discord.Interaction):
    if not await host_only(interaction):
        return
    bad = db.execute("SELECT COUNT(*) FROM players WHERE step<0 OR step>40 OR health<0 OR energy<0 OR points<0 OR items<0").fetchone()[0]
    rows = db.execute("SELECT COUNT(*), COUNT(DISTINCT user_id) FROM players").fetchone()
    await interaction.response.send_message(embed=embed("🛡️ Integrity Audit", f"Stored adventures: **{rows[0]}**\nUnique Discord IDs: **{rows[1]}**\nInvalid state rows: **{bad}**\nRandom-draw commitment: `{get_meta('draw_commit')}`\n\nThis detects basic integrity issues, not alt accounts or external assistance."), ephemeral=True)


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.guild_id != GUILD_ID or str(payload.message_id) != get_meta("panel_message") or str(payload.emoji) != "🍁":
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild or payload.user_id == bot.user.id:
        return
    member = payload.member or guild.get_member(payload.user_id)
    if not member:
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return
    role = guild.get_role(ROLE_ID)
    if role and not member.bot:
        try:
            await member.add_roles(role, reason="Forbidden Forest 🍁 signup")
            log(member.id, "ROLE_ADD", "Leaf reaction")
        except discord.Forbidden:
            log(member.id, "ROLE_ERROR", "Cannot assign role; check hierarchy")


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.guild_id != GUILD_ID or str(payload.message_id) != get_meta("panel_message") or str(payload.emoji) != "🍁":
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    try:
        member = await guild.fetch_member(payload.user_id)
        role = guild.get_role(ROLE_ID)
        if role and not member.bot:
            await member.remove_roles(role, reason="Forbidden Forest 🍁 signup removed")
            log(member.id, "ROLE_REMOVE", "Leaf reaction removed")
    except discord.HTTPException:
        pass


async def event_channel():
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except discord.HTTPException:
            return None
    return channel


async def reconcile():
    # Guarded by lock; scheduled transitions survive bot restarts.
    async with lock:
        t = now()
        if START <= t and not get_meta("opened"):
            channel = await event_channel()
            if channel:
                await channel.send(content=f"<@&{ROLE_ID}> 🌲 **THE FORBIDDEN FOREST IS OPEN!**",
                                   embed=embed("🌲 Your adventure begins", "You have until **11 Nov 2026, 00:00 GMT** to complete up to 40 encounters. Click below or use `/forest play`."),
                                   view=StartView(), allowed_mentions=discord.AllowedMentions(roles=True))
                set_meta("opened", stamp(t))
                log(None, "EVENT_OPEN", "Automatic opening announcement")
        if t >= END and not get_meta("closed"):
            # Automatically bank incomplete living runs, and settle defeated runs.
            for p in db.execute("SELECT * FROM players WHERE status='active' AND stage='main'").fetchall():
                finish_main(p["user_id"], escaped=p["health"] > 0, defeated=p["health"] <= 0)
            set_meta("closed", stamp(t))
            log(None, "EVENT_CLOSED", "Automatic main event close")
            eligible = prize_ties(competitors())
            if eligible:
                deadline = END + dt.timedelta(hours=BONUS_HOURS)
                # If downtime spans the whole tiebreaker window, no extra play
                # is allowed: participants forfeit, then finalise.
                set_meta("bonus_deadline", stamp(deadline))
                for uid in eligible:
                    db.execute("""UPDATE players SET stage='bonus',step=0,health=100,energy=100,supplies=8,
                      points=0,items=0,artifact_bag='{}',objectives=0,spent=0,bonus_status='active' WHERE user_id=?""", (uid,))
                channel = await event_channel()
                if channel:
                    mentions = " ".join(f"<@{uid}>" for uid in sorted(eligible))
                    await channel.send(content=mentions[:1800], embed=embed("🏆 Automatic tiebreaker", "A prize-position tie remains. Eligible players have **five bonus encounters** and until **12 Nov 2026, 00:00 GMT** to finish. Use `/forest resume`."), allowed_mentions=discord.AllowedMentions(users=True))
                log(None, "BONUS_OPEN", f"eligible={sorted(eligible)} deadline={stamp(deadline)}")
        deadline = get_meta("bonus_deadline")
        if t >= END and not get_meta("finalized") and (not deadline or t >= parse(deadline)):
            for p in db.execute("SELECT * FROM players WHERE bonus_status='active'").fetchall():
                finish_bonus(p["user_id"], forfeited=True)
            set_meta("finalized", stamp(t))
            rows = final_order()
            prizes = ["125c + HC Box", "75c + HC Box", "50c + HC Box"]
            body = "\n".join(f"**{i+1}.** <@{p['user_id']}> — **{p['main_score']}** points — {prizes[i]}" for i, p in enumerate(rows[:3])) or "No qualifying players."
            channel = await event_channel()
            if channel:
                await channel.send(embed=embed("🏆 Forbidden Forest — Winners", body + "\n\nDraw commitment: `" + get_meta("draw_commit") + "`\nDraw secret (verification): `" + get_meta("draw_secret") + "`"))
            log(None, "FINAL", "Winners announced")


@tasks.loop(seconds=30)
async def scheduler():
    try:
        await reconcile()
    except Exception:
        import traceback
        traceback.print_exc()


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | guild={GUILD_ID} | phase={game_phase()}")


@bot.event
async def setup_hook():
    init_db()
    bot.add_view(StartView())
    bot.tree.add_command(forest, guild=discord.Object(id=GUILD_ID))
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    scheduler.start()


if __name__ == "__main__":
    if not TOKEN or not GUILD_ID or not CHANNEL_ID:
        raise RuntimeError("Set DISCORD_TOKEN, GUILD_ID and EVENT_CHANNEL_ID in environment")
    bot.run(TOKEN)
