# XAUUSD HTF FVG Telegram Bot — GitHub Actions setup

This runs the bot for free on GitHub's servers on a schedule (every ~5
minutes), so it works even when your own computer is off.

## Important: make the repo PUBLIC

GitHub Actions meters minutes on **private** repos (2,000 free/month).
At 5-minute intervals that's roughly 8,600+ runs/month — well past the
free allowance, which would mean a real (if modest, ~$50/month) bill.
On a **public** repo, GitHub does not meter Actions minutes at all, so
5-minute checks stay completely free.

Going public means anyone could see the code and the committed state
file (`fvg_bot_state.json` — shows entry/SL/TP levels, lot sizes,
status). Your Telegram bot token and chat ID are NOT affected either
way — they live in encrypted repo secrets, never in the code or state
file. If you'd rather not have any of this public, switch the cron in
`.github/workflows/fvg_alert.yml` back to `*/15 * * * *` and keep the
repo private instead — comfortably inside the free private minutes.

## Setup

1. Create a new GitHub repository — this time choose **Public** when
   creating it (not Private).

2. Upload these files into it, keeping the folder structure exactly as-is:
       xauusd_fvg_telegram_bot.py
       requirements.txt
       .github/workflows/fvg_alert.yml
   Easiest way: on the repo's GitHub page, click "Add file" > "Upload
   files", drag this whole folder's contents in, and commit. GitHub
   preserves the .github/workflows/ path automatically.

3. Add your secrets (Settings > Secrets and variables > Actions >
   "New repository secret"):
       Name: TELEGRAM_BOT_TOKEN     Value: <your bot token>
       Name: TELEGRAM_CHAT_ID       Value: <your chat id>
   Do NOT paste these into the .py file for this copy — the whole point
   of using secrets is that your token never sits in the repo's code or
   history, even though the repo itself is public.

4. Go to the "Actions" tab of the repo. If asked, click "I understand my
   workflows, enable them". Click into "XAUUSD FVG Telegram Bot" on the
   left, then "Run workflow" (workflow_dispatch) to fire it manually
   once — this is your test run. You should get a Telegram message
   within about a minute if the token/chat ID are correct, and the run's
   log (click into it) will show what happened either way.

5. That's it. From here it fires automatically on the cron schedule.
   Every run commits an updated `fvg_bot_state.json` back to the repo —
   that's how it remembers an active setup / already-sent alerts across
   runs, since each GitHub Actions run starts from a completely fresh
   machine with no memory of the last one.

## Notes

- GitHub disables scheduled workflows on a repo after 60 days with no
  activity. Since every run commits a state-file update, this repo
  stays "active" on its own — but if you ever see alerts stop and the
  Actions tab shows the schedule as disabled, just re-enable it there.
- 5 minutes is GitHub's minimum cron granularity, and schedules can
  still run a few minutes late under load — treat "5 minutes" as
  "roughly 5 minutes, sometimes a bit more."
- This still uses Yahoo Finance data (same caveats as running it
  locally: it can drift slightly from your broker's feed).
