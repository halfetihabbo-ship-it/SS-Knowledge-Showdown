"""Autumn Pattern Recall — discord.py 2.4+ standalone bot."""
import asyncio
import json
import logging
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.environ['DISCORD_TOKEN']
HOST_ID = int(os.getenv('HOST_ID', '1501587787294703657'))
ROLE_ID = int(os.getenv('ROLE_ID', '1567982021106602065'))
GUILD_ID = int(os.getenv('GUILD_ID', '0'))
DB_PATH = os.getenv('DB_PATH', 'pattern.db')
SYMBOLS = ['🍁', '🍂', '🌰', '🎃']
ROUNDS = 15
TIE_ROUNDS = 5
SHOW_SECONDS = 7
TIE_HOURS = 24
LOG = logging.getLogger('autumn')
logging.basicConfig(level=logging.INFO)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute('PRAGMA journal_mode=WAL')
db.executescript('''
CREATE TABLE IF NOT EXISTS events (
 guild INTEGER PRIMARY KEY, channel INTEGER NOT NULL, status TEXT NOT NULL,
 ends TEXT NOT NULL, tie_stage INTEGER NOT NULL DEFAULT 0, tie_ends TEXT,
 panel_message INTEGER, game_message INTEGER
);
CREATE TABLE IF NOT EXISTS players (
 guild INTEGER NOT NULL, user INTEGER NOT NULL, round INTEGER NOT NULL DEFAULT 0,
 score INTEGER NOT NULL DEFAULT 0, tie_score INTEGER NOT NULL DEFAULT 0,
 tie_round INTEGER NOT NULL DEFAULT 0, tie_stage INTEGER NOT NULL DEFAULT 0,
 sequence TEXT, entered TEXT NOT NULL DEFAULT '[]', phase TEXT NOT NULL DEFAULT 'idle',
 PRIMARY KEY(guild,user)
);
CREATE TABLE IF NOT EXISTS tie_players (
 guild INTEGER NOT NULL, stage INTEGER NOT NULL, user INTEGER NOT NULL,
 PRIMARY KEY(guild,stage,user)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild INTEGER, user INTEGER,
 action TEXT, details TEXT, at TEXT
);
''')
db.commit()

def now(): return datetime.now(timezone.utc)
def stamp(dt): return dt.isoformat()
def parse(s): return datetime.fromisoformat(s)
def event(guild): return db.execute('SELECT * FROM events WHERE guild=?', (guild,)).fetchone()
def player(guild,user): return db.execute('SELECT * FROM players WHERE guild=? AND user=?',(guild,user)).fetchone()
def audit(guild,user,action,details=''):
 db.execute('INSERT INTO audit(guild,user,action,details,at) VALUES(?,?,?,?,?)',(guild,user,action,details,stamp(now())))
 db.commit()
def is_host(interaction): return interaction.user.id == HOST_ID

def eligible(guild,user,e):
 if e['status']=='main': return True
 if e['status']=='tie':
  return bool(db.execute('SELECT 1 FROM tie_players WHERE guild=? AND stage=? AND user=?',(guild,e['tie_stage'],user)).fetchone())
 return False

def length_for(round_index, tie=False):
 return min(13, (8 if tie else 3) + (round_index // (2 if tie else 2)))

def ranking(guild):
 return db.execute('SELECT * FROM players WHERE guild=? ORDER BY score DESC,tie_score DESC,user ASC',(guild,)).fetchall()

def tied_prize_groups(guild):
 """Return users in tied groups that intersect the top three places."""
 rows=ranking(guild)
 if not rows: return []
 groups=[]; index=0
 while index < len(rows):
  j=index+1
  while j<len(rows) and (rows[j]['score'],rows[j]['tie_score'])==(rows[index]['score'],rows[index]['tie_score']): j+=1
  if j-index>1 and index<3: groups.extend(r['user'] for r in rows[index:j])
  index=j
 return groups

intents=discord.Intents.default()
intents.members=True
intents.reactions=True
bot=commands.Bot(command_prefix='!',intents=intents)

async def announce(channel,content):
 try: await channel.send(content,allowed_mentions=discord.AllowedMentions(everyone=False,roles=True,users=True))
 except discord.HTTPException: LOG.exception('Announcement failed')

class RolePanelView(discord.ui.View):
 """Reaction role is handled in on_raw_reaction_add/remove; no buttons needed."""
 pass

class GameView(discord.ui.View):
 def __init__(self):
  super().__init__(timeout=None)

 @discord.ui.button(label='Start / Resume',style=discord.ButtonStyle.success,emoji='🍁',custom_id='autumn:begin')
 async def begin(self,interaction:discord.Interaction,button:discord.ui.Button):
  g=interaction.guild_id
  if not g:
   return await interaction.response.send_message('Use this in the server.',ephemeral=True)
  e=event(g)
  if not e or e['status'] not in ('main','tie'):
   return await interaction.response.send_message('There is no active challenge.',ephemeral=True)
  if now()>=parse(e['ends'] if e['status']=='main' else e['tie_ends']):
   return await interaction.response.send_message('This round has closed.',ephemeral=True)
  if not eligible(g,interaction.user.id,e):
   return await interaction.response.send_message('You are not in this tiebreaker.',ephemeral=True)
  p=player(g,interaction.user.id)
  if not p:
   db.execute('INSERT INTO players(guild,user) VALUES(?,?)',(g,interaction.user.id));db.commit()
   audit(g,interaction.user.id,'joined')
   p=player(g,interaction.user.id)
  if e['status']=='tie' and p['tie_stage']!=e['tie_stage']:
   db.execute("UPDATE players SET tie_stage=?,tie_round=0,phase='idle',sequence=NULL,entered='[]' WHERE guild=? AND user=?",(e['tie_stage'],g,interaction.user.id));db.commit()
   p=player(g,interaction.user.id)
  count=p['round'] if e['status']=='main' else p['tie_round']
  maximum=ROUNDS if e['status']=='main' else TIE_ROUNDS
  if count>=maximum:
   return await interaction.response.send_message('You have completed all rounds for this stage!',ephemeral=True)
  if p['phase']=='input' and p['sequence']:
   return await interaction.response.send_message(f'Round {count+1}/{maximum}: continue entering your pattern.',view=InputView(g,interaction.user.id),ephemeral=True)
  # A displayed pattern is not kept visible on resume; restart its display.
  seq=[random.randrange(len(SYMBOLS)) for _ in range(length_for(count,e['status']=='tie'))]
  db.execute("UPDATE players SET sequence=?,entered='[]',phase='show' WHERE guild=? AND user=?",(json.dumps(seq),g,interaction.user.id));db.commit()
  await interaction.response.send_message(f'**Round {count+1}/{maximum} — Memorise this sequence!**\n\n'+' '.join(SYMBOLS[x] for x in seq)+f'\n\nIt disappears in {SHOW_SECONDS} seconds.',ephemeral=True)
  await asyncio.sleep(SHOW_SECONDS)
  # Guard against old concurrent reveal tasks overwriting newer rounds.
  p2=player(g,interaction.user.id)
  if not p2 or p2['phase']!='show' or p2['sequence']!=json.dumps(seq): return
  db.execute("UPDATE players SET phase='input' WHERE guild=? AND user=?",(g,interaction.user.id));db.commit()
  try:
   await interaction.edit_original_response(content=f'**Round {count+1}/{maximum}** — Reproduce the sequence in the correct order.\nUse the buttons below. One submission only.',view=InputView(g,interaction.user.id))
  except discord.HTTPException: LOG.exception('Unable to hide sequence')

class InputView(discord.ui.View):
 def __init__(self,guild,user):
  super().__init__(timeout=900)
  self.guild=guild;self.user=user
  for i,sym in enumerate(SYMBOLS):
   btn=discord.ui.Button(emoji=sym,style=discord.ButtonStyle.secondary,row=0)
   async def push(interaction, idx=i): await self.push(interaction,idx)
   btn.callback=push;self.add_item(btn)
  undo=discord.ui.Button(label='Undo',style=discord.ButtonStyle.secondary,row=1)
  undo.callback=self.undo;self.add_item(undo)
  submit=discord.ui.Button(label='Lock Answer',style=discord.ButtonStyle.success,row=1)
  submit.callback=self.submit;self.add_item(submit)

 def validate(self,interaction):
  e=event(self.guild);p=player(self.guild,self.user)
  valid=(interaction.user.id==self.user and e and p and e['status'] in ('main','tie') and
         now()<parse(e['ends'] if e['status']=='main' else e['tie_ends']) and
         eligible(self.guild,self.user,e) and p['phase']=='input' and p['sequence'])
  return e,p,valid

 async def push(self,interaction,idx):
  e,p,ok=self.validate(interaction)
  if not ok: return await interaction.response.send_message('This input has expired or belongs to another player. Use Start / Resume.',ephemeral=True)
  entered=json.loads(p['entered']); seq=json.loads(p['sequence'])
  if len(entered)<len(seq): entered.append(idx)
  db.execute('UPDATE players SET entered=? WHERE guild=? AND user=?',(json.dumps(entered),self.guild,self.user));db.commit()
  await interaction.response.edit_message(content=f'**Enter pattern** ({len(entered)}/{len(seq)})\n'+ ' '.join(SYMBOLS[x] for x in entered),view=InputView(self.guild,self.user))

 async def undo(self,interaction):
  e,p,ok=self.validate(interaction)
  if not ok: return await interaction.response.send_message('This input has expired. Use Start / Resume.',ephemeral=True)
  entered=json.loads(p['entered']);seq=json.loads(p['sequence'])
  if entered: entered.pop()
  db.execute('UPDATE players SET entered=? WHERE guild=? AND user=?',(json.dumps(entered),self.guild,self.user));db.commit()
  await interaction.response.edit_message(content=f'**Enter pattern** ({len(entered)}/{len(seq)})\n'+' '.join(SYMBOLS[x] for x in entered),view=InputView(self.guild,self.user))

 async def submit(self,interaction):
  e,p,ok=self.validate(interaction)
  if not ok: return await interaction.response.send_message('This input has expired. Use Start / Resume.',ephemeral=True)
  entered=json.loads(p['entered']);seq=json.loads(p['sequence'])
  if len(entered)!=len(seq): return await interaction.response.send_message(f'Enter all {len(seq)} symbols before submitting.',ephemeral=True)
  correct=entered==seq;tie=e['status']=='tie'
  col='tie_round' if tie else 'round'
  score_col='tie_score' if tie else 'score'
  db.execute(f"UPDATE players SET {col}={col}+1,{score_col}={score_col}+?,sequence=NULL,entered='[]',phase='idle' WHERE guild=? AND user=?",(10 if correct else 0,self.guild,self.user))
  db.commit()
  audit(self.guild,self.user,'tiebreaker_answer' if tie else 'answer',f'round={p[col]+1},correct={correct},expected={seq},received={entered}')
  total=TIE_ROUNDS if tie else ROUNDS
  msg=f'Answer locked. **Round {p[col]+1}/{total} complete.**'
  if p[col]+1<total: msg+='\nClick **Start / Resume** on the public game panel for the next round.'
  else: msg+='\nYou have completed this stage! Results will be revealed when it closes.'
  await interaction.response.edit_message(content=msg,view=None)

async def transition(guild):
 e=event(guild)
 if not e: return
 channel=bot.get_channel(e['channel'])
 if not channel:
  try: channel=await bot.fetch_channel(e['channel'])
  except discord.HTTPException: return
 if e['status']=='main' and now()>=parse(e['ends']):
  participants=ranking(guild)
  if not participants:
   db.execute("UPDATE events SET status='done' WHERE guild=?",(guild,));db.commit()
   await announce(channel,'🍁 Autumn Pattern Recall closed with no participants.')
   return
  tied=tied_prize_groups(guild)
  if tied: await start_tie(guild,channel,tied,1)
  else: await finish(guild,channel)
 elif e['status']=='tie' and now()>=parse(e['tie_ends']):
  tied=tied_prize_groups(guild)
  if tied: await start_tie(guild,channel,tied,e['tie_stage']+1)
  else: await finish(guild,channel)

async def start_tie(guild,channel,users,stage):
 deadline=now()+timedelta(hours=TIE_HOURS)
 db.execute("UPDATE events SET status='tie',tie_stage=?,tie_ends=? WHERE guild=?",(stage,stamp(deadline),guild))
 for u in users:
  db.execute('INSERT OR IGNORE INTO tie_players(guild,stage,user) VALUES(?,?,?)',(guild,stage,u))
  db.execute("UPDATE players SET tie_stage=?,tie_round=0,phase='idle',sequence=NULL,entered='[]' WHERE guild=? AND user=?",(stage,guild,u))
 db.commit()
 audit(guild,0,'tiebreaker_started',f'stage={stage},users={users}')
 mentions=' '.join(f'<@{u}>' for u in users)
 await announce(channel,f'🏆 **Automatic Tiebreaker — Stage {stage}**\n{mentions}\nYou are tied for a prize position! Complete **five additional memory rounds** using **Start / Resume** on the game panel. You have **24 hours**, until <t:{int(deadline.timestamp())}:F>. Incomplete rounds earn no points.')

async def finish(guild,channel):
 db.execute("UPDATE events SET status='done' WHERE guild=?",(guild,));db.commit()
 rows=ranking(guild)
 lines=[]
 for i,r in enumerate(rows[:10],1):
  medal=['🥇','🥈','🥉'][i-1] if i<=3 else f'{i}.'
  lines.append(f'{medal} <@{r["user"]}> — {r["score"]}/{ROUNDS*10} main, {r["tie_score"]} tiebreaker')
 audit(guild,0,'event_finished',f'players={len(rows)}')
 await announce(channel,'🍁 **AUTUMN PATTERN RECALL — FINAL RESULTS**\n'+'\n'.join(lines))

@tasks.loop(seconds=30)
async def deadline_watcher():
 for e in db.execute("SELECT guild FROM events WHERE status IN ('main','tie')").fetchall():
  try: await transition(e['guild'])
  except Exception: LOG.exception('Deadline processing failed for %s',e['guild'])

@deadline_watcher.before_loop
async def before_deadline(): await bot.wait_until_ready()

@bot.event
async def on_ready():
 LOG.info('Logged in as %s',bot.user)

@bot.event
async def setup_hook():
 bot.add_view(GameView())
 if GUILD_ID:
  guild_obj=discord.Object(id=GUILD_ID)
  bot.tree.copy_global_to(guild=guild_obj)
  await bot.tree.sync(guild=guild_obj)
 else: await bot.tree.sync()
 deadline_watcher.start()

@bot.event
async def on_raw_reaction_add(payload):
 if payload.user_id==bot.user.id or str(payload.emoji)!='🍁': return
 e=event(payload.guild_id)
 if not e or e['panel_message']!=payload.message_id: return
 guild=bot.get_guild(payload.guild_id)
 if not guild: return
 member=payload.member or guild.get_member(payload.user_id)
 if not member:
  try: member=await guild.fetch_member(payload.user_id)
  except discord.HTTPException: return
 role=guild.get_role(ROLE_ID)
 if role:
  try:
   await member.add_roles(role,reason='Autumn Pattern Recall signup')
   audit(guild.id,member.id,'role_added')
  except discord.Forbidden: LOG.error('Cannot assign role; check Manage Roles and hierarchy')

@bot.event
async def on_raw_reaction_remove(payload):
 if str(payload.emoji)!='🍁': return
 e=event(payload.guild_id)
 if not e or e['panel_message']!=payload.message_id: return
 guild=bot.get_guild(payload.guild_id)
 if not guild: return
 try: member=await guild.fetch_member(payload.user_id)
 except discord.HTTPException: return
 role=guild.get_role(ROLE_ID)
 if role:
  try:
   await member.remove_roles(role,reason='Autumn Pattern Recall signup removed')
   audit(guild.id,member.id,'role_removed')
  except discord.Forbidden: LOG.error('Cannot remove role; check hierarchy')

pattern=app_commands.Group(name='pattern',description='Autumn Pattern Recall event commands')

@pattern.command(name='panel',description='Post the @everyone reaction-role signup graphic')
@app_commands.guild_only()
async def panel_cmd(interaction:discord.Interaction):
 if not is_host(interaction): return await interaction.response.send_message('Host only.',ephemeral=True)
 await interaction.response.defer(ephemeral=True)
 g=interaction.guild_id;e=event(g)
 if not e:
  db.execute('INSERT INTO events(guild,channel,status,ends) VALUES(?,?,?,?)',(g,interaction.channel_id,'setup',stamp(now())));db.commit()
 role=interaction.guild.get_role(ROLE_ID)
 if not role: return await interaction.followup.send('Role ID not found in this server.',ephemeral=True)
 content='@everyone\n🍁 **AUTUMN PATTERN RECALL — SIGN UP!**\nTest your memory across **15 increasingly difficult rounds**. You will have **48 hours** to join the challenge once it opens, and automatic tiebreakers will decide tied prize positions.\n\n**React with 🍁 to receive the SS Quiz role** and competition updates!'
 file=discord.File('panel.png',filename='autumn-pattern-recall.png') if Path('panel.png').exists() else None
 msg=await interaction.channel.send(content,file=file,allowed_mentions=discord.AllowedMentions(everyone=True))
 await msg.add_reaction('🍁')
 db.execute('UPDATE events SET panel_message=? WHERE guild=?',(msg.id,g));db.commit()
 audit(g,interaction.user.id,'panel_posted',str(msg.id))
 await interaction.followup.send(f'Role panel posted: {msg.jump_url}',ephemeral=True)

@pattern.command(name='start',description='Open the 48-hour competition and post the game button')
@app_commands.guild_only()
async def start_cmd(interaction:discord.Interaction):
 if not is_host(interaction): return await interaction.response.send_message('Host only.',ephemeral=True)
 e=event(interaction.guild_id)
 if e and e['status'] in ('main','tie'): return await interaction.response.send_message('An event is already running.',ephemeral=True)
 await interaction.response.defer(ephemeral=True)
 ends=now()+timedelta(hours=48)
 g=interaction.guild_id
 db.execute('DELETE FROM players WHERE guild=?',(g,))
 db.execute('DELETE FROM tie_players WHERE guild=?',(g,))
 db.execute('INSERT INTO events(guild,channel,status,ends,tie_stage,tie_ends) VALUES(?,?,?,?,0,NULL) ON CONFLICT(guild) DO UPDATE SET channel=excluded.channel,status=excluded.status,ends=excluded.ends,tie_stage=0,tie_ends=NULL',(g,interaction.channel_id,'main',stamp(ends)))
 db.commit()
 msg=await interaction.channel.send(f'🍁 **AUTUMN PATTERN RECALL IS LIVE!**\n15 increasingly difficult memory rounds. Click below to start or resume.\nEntry closes <t:{int(ends.timestamp())}:R>. One attempt per round. Each sequence is shown for {SHOW_SECONDS} seconds.\nScores remain private until the event closes.',view=GameView())
 db.execute('UPDATE events SET game_message=? WHERE guild=?',(msg.id,g));db.commit()
 audit(g,interaction.user.id,'event_started',str(msg.id))
 await interaction.followup.send(f'Game started: {msg.jump_url}',ephemeral=True)

@pattern.command(name='dashboard',description='Private host overview')
@app_commands.guild_only()
async def dashboard(interaction:discord.Interaction):
 if not is_host(interaction): return await interaction.response.send_message('Host only.',ephemeral=True)
 e=event(interaction.guild_id)
 if not e: return await interaction.response.send_message('No event configured.',ephemeral=True)
 rows=ranking(interaction.guild_id)
 lines=[f'<@{p["user"]}> — {p["round"]}/15, main {p["score"]}, tie {p["tie_score"]} ({p["tie_round"]}/5)' for p in rows[:30]]
 await interaction.response.send_message(f'**Status:** {e["status"]} | **Players:** {len(rows)} | **Tie stage:** {e["tie_stage"]}\n'+'\n'.join(lines or ['No participants yet.']),ephemeral=True)

@pattern.command(name='logs',description='Private recent event audit entries')
@app_commands.guild_only()
async def logs(interaction:discord.Interaction):
 if not is_host(interaction): return await interaction.response.send_message('Host only.',ephemeral=True)
 rows=db.execute('SELECT * FROM audit WHERE guild=? ORDER BY id DESC LIMIT 20',(interaction.guild_id,)).fetchall()
 lines=[f'`{r["at"][11:19]}` <@{r["user"]}> {r["action"]}: {r["details"][:85]}' for r in rows]
 await interaction.response.send_message('**Latest 20 log entries**\n'+'\n'.join(lines or ['No logs.']),ephemeral=True)

@pattern.command(name='player',description='Private detailed player record')
@app_commands.guild_only()
@app_commands.describe(member='Member to inspect')
async def player_cmd(interaction:discord.Interaction,member:discord.Member):
 if not is_host(interaction): return await interaction.response.send_message('Host only.',ephemeral=True)
 p=player(interaction.guild_id,member.id)
 if not p: return await interaction.response.send_message('No record for this player.',ephemeral=True)
 await interaction.response.send_message(f'**{member.mention}**\nMain: {p["round"]}/15, {p["score"]} points\nTiebreaker: stage {p["tie_stage"]}, {p["tie_round"]}/5, {p["tie_score"]} total points\nPhase: {p["phase"]}',ephemeral=True)

@pattern.command(name='leaderboard',description='Private host leaderboard')
@app_commands.guild_only()
async def leaderboard(interaction:discord.Interaction):
 if not is_host(interaction): return await interaction.response.send_message('Host only.',ephemeral=True)
 rows=ranking(interaction.guild_id)
 await interaction.response.send_message('**Current leaderboard (host only)**\n'+'\n'.join(f'{i}. <@{r["user"]}> — {r["score"]} + {r["tie_score"]} tie' for i,r in enumerate(rows[:25],1)) if rows else 'No players.',ephemeral=True)

bot.tree.add_command(pattern)
bot.run(TOKEN)
