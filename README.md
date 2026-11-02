# SS Knowledge Showdown — Discord bot

## Setup (Windows / VS Code)

1. Install Python 3.11+.
2. Extract the ZIP and open its folder in VS Code.
3. In a terminal run `py -m pip install -r requirements.txt`.
4. Create a Discord bot at https://discord.com/developers/applications, invite it with the **bot** and **applications.commands** scopes. Give it View Channel, Send Messages, Embed Links and Read Message History permissions.
5. Set environment variables `DISCORD_TOKEN` to the bot token and (recommended) `GUILD_ID` to your Discord server ID. For PowerShell in the same terminal: `$env:DISCORD_TOKEN="YOUR_TOKEN"` and `$env:GUILD_ID="YOUR_SERVER_ID"`.
6. Run `py bot.py`.
7. In Discord use `/quiz` and select **start** under the **action** dropdown. Manage Server permission is required to host. Players join with the button. Other actions are pause, resume, leaderboard and end.

## Game

- 50-player lobby, open for 120 seconds; at least 2 players required.
- 25 questions: 2 easy, 9 medium, 14 hard, randomly drawn from 53 candidate questions.
- Four buttons per question, 15 seconds each, private locked-in answer acknowledgements.
- 100 points per correct answer, +100 on every third consecutive correct answer.
- Leaderboard every 5 questions. Prize-place ties use 10-second sudden-death questions from the unused pool.
- `/quiz` **skip** is intentionally unavailable; pause takes effect between questions.
- Scores are in memory only and will be lost if the bot restarts.

## IMPORTANT: question verification

`questions.json` contains 53 **candidate** questions assembled from the earlier chat's draft questions and answers. They have **not been independently reverified against the current live SS policy**. Each has `review_status: CHECK_CURRENT_POLICY`. Review every question and its answer against the current relevant page on https://habboss.org/ before using the bot for an official prize event. `source` is a general site reference, not proof of verification. Remove or edit outdated or ambiguous questions. Ensure you still have at least 2 easy, 9 medium, 14 hard, and 35 total questions.

The bot does not check `review_status` automatically. The host is responsible for reviewing the bank.
