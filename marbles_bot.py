import discord
from discord.ext import commands, tasks
from discord import ui
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, date, timedelta
import pytz
import asyncio
import csv
import random
import os

# ---- Load secrets from .env file ----
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ---- Supabase client ----
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---- Discord bot setup ----
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

EST = pytz.timezone("America/New_York")

# Timeout settings (in minutes)
NO_VOTE_CANCEL_MINUTES   = 120  # Both players silent for 2 hrs → cancel, no transfer
ONE_VOTE_WARNING_MINUTES = 60   # One vote in, other silent for 1 hr → warning ping
ONE_VOTE_AWARD_MINUTES   = 90   # One vote in, other silent for 1.5 hrs → auto-award


# ==============================================================
#  HELPER FUNCTIONS
# ==============================================================

def get_player(user_id: str):
    res = supabase.table("players").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None


def get_active_challenge(user_id: str):
    res = (
        supabase.table("challenges")
        .select("*")
        .in_("status", ["pending", "active"])
        .execute()
    )
    for c in res.data:
        if c["challenger_id"] == user_id or c["opponent_id"] == user_id:
            return c
    return None


def update_player(user_id: str, data: dict):
    supabase.table("players").update(data).eq("user_id", user_id).execute()


def update_challenge(challenge_id: str, data: dict):
    supabase.table("challenges").update(data).eq("id", challenge_id).execute()


def is_marble_admin(ctx):
    return discord.utils.get(ctx.author.roles, name="Marble Admin") is not None


def get_random_quote() -> str:
    """Load a random quote from quotes.csv and return it formatted for Discord."""
    try:
        with open("quotes.csv", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="|")
            quotes = list(reader)
        if not quotes:
            return ""
        pick = random.choice(quotes)
        return f'*"{pick["quote"]}"*\n— {pick["author"]}'
    except FileNotFoundError:
        return ""


def minutes_since(timestamp_str: str) -> float:
    """Return how many minutes have passed since a UTC timestamp string."""
    if not timestamp_str:
        return 0
    # Supabase returns timestamps like "2024-01-01T12:00:00.000000+00:00"
    ts = datetime.fromisoformat(timestamp_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=pytz.utc)
    now = datetime.now(pytz.utc)
    return (now - ts).total_seconds() / 60


# ==============================================================
#  VIEWS (Buttons)
# ==============================================================

class ChallengeView(ui.View):
    """Accept / Decline buttons on a challenge message."""

    def __init__(self, challenger_id: int, opponent_id: int):
        super().__init__(timeout=300)
        self.challenger_id = challenger_id
        self.opponent_id = opponent_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message(
                "These buttons aren't for you!", ephemeral=True
            )
            return False
        return True

    @ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept_button(self, interaction: discord.Interaction, button: ui.Button):
        uid = str(interaction.user.id)
        player = get_player(uid)
        ch = get_active_challenge(uid)

        if not ch or ch["status"] != "pending":
            await interaction.response.send_message(
                "This challenge no longer exists.", ephemeral=True
            )
            self.stop()
            return

        if player["marbles"] == 0:
            await interaction.response.send_message(
                "You have 0 marbles — you can't accept a challenge right now! "
                "Use `!bonusmarble` or wait for the midnight drop.",
                ephemeral=True
            )
            return

        challenger = get_player(str(self.challenger_id))
        now_utc = datetime.now(pytz.utc).isoformat()
        update_challenge(ch["id"], {
            "status": "active",
            "challenger_stakes": challenger["marbles"],
            "opponent_stakes": player["marbles"],
            "accepted_at": now_utc,
        })
        update_player(str(self.challenger_id), {"in_match": True})
        update_player(uid, {"in_match": True})

        challenger_user = await bot.fetch_user(self.challenger_id)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} accepted the challenge!\n"
            f"**{challenger_user.display_name}** ({challenger['marbles']} 🔮) vs "
            f"**{interaction.user.display_name}** ({player['marbles']} 🔮)\n"
            f"Go play your match, then both report `!winner @player` when done.\n\n"
            f"{get_random_quote()}"
        )
        self.stop()

    @ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline_button(self, interaction: discord.Interaction, button: ui.Button):
        uid = str(interaction.user.id)
        ch = get_active_challenge(uid)

        if not ch or ch["status"] != "pending":
            await interaction.response.send_message(
                "This challenge no longer exists.", ephemeral=True
            )
            self.stop()
            return

        update_challenge(ch["id"], {"status": "cancelled"})
        challenger_user = await bot.fetch_user(self.challenger_id)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"❌ {interaction.user.mention} declined the challenge from {challenger_user.mention}. "
            f"No marbles were harmed."
        )
        self.stop()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


class BegView(ui.View):
    """Accept / Deny buttons on a beg message."""

    def __init__(self, beggar_id: int, target_id: int):
        super().__init__(timeout=300)
        self.beggar_id = beggar_id
        self.target_id = target_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.target_id:
            await interaction.response.send_message(
                "This isn't your beg to respond to!", ephemeral=True
            )
            return False
        return True

    @ui.button(label="🤲 Give 1 Marble", style=discord.ButtonStyle.success)
    async def give_button(self, interaction: discord.Interaction, button: ui.Button):
        giver = get_player(str(self.target_id))
        beggar = get_player(str(self.beggar_id))

        if not giver or not beggar:
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
            self.stop()
            return
        if giver["marbles"] < 1:
            await interaction.response.send_message(
                "You don't have any marbles to give!", ephemeral=True
            )
            return
        if beggar["marbles"] > 0:
            await interaction.response.send_message(
                "They already have marbles — no need!", ephemeral=True
            )
            return

        update_player(str(self.target_id), {"marbles": giver["marbles"] - 1})
        update_player(str(self.beggar_id), {"marbles": beggar["marbles"] + 1})

        beggar_user = await bot.fetch_user(self.beggar_id)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"🤲 {interaction.user.display_name} gave 1 marble to **{beggar_user.display_name}**. "
            f"A charitable soul. Marbles match??"
        )
        self.stop()

    @ui.button(label="🚫 Deny", style=discord.ButtonStyle.secondary)
    async def deny_button(self, interaction: discord.Interaction, button: ui.Button):
        beggar_user = await bot.fetch_user(self.beggar_id)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message(
            f"🚫 {interaction.user.display_name} said no. "
            f"**{beggar_user.display_name}** must wait for midnight. 😔"
        )
        self.stop()


# ==============================================================
#  BOT EVENTS
# ==============================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Marbles bot is online!")
    midnight_marble_drop.start()
    match_timeout_check.start()


# ==============================================================
#  PLAYER COMMANDS
# ==============================================================

@bot.command()
async def join(ctx):
    uid = str(ctx.author.id)
    if get_player(uid):
        await ctx.send(f"{ctx.author.mention} You're already in the marbles system! 🔮")
        return
    supabase.table("players").insert({
        "user_id": uid,
        "display_name": ctx.author.display_name,
        "marbles": 10,
        "in_match": False,
    }).execute()
    await ctx.send(
        f"🔮 Welcome to the marbles arena, {ctx.author.mention}! "
        f"You start with **10 marbles**. Good luck."
    )


@bot.command()
async def marbles(ctx, member: discord.Member = None):
    target = member or ctx.author
    player = get_player(str(target.id))
    if not player:
        await ctx.send(f"{target.display_name} hasn't joined yet. Tell them to run `!join`!")
        return
    await ctx.send(f"🔮 **{target.display_name}** has **{player['marbles']} marble(s)**.")


@bot.command()
async def leaderboard(ctx):
    res = supabase.table("players").select("*").order("marbles", desc=True).execute()
    if not res.data:
        await ctx.send("Nobody has joined yet! Run `!join` to start.")
        return

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    rank = 0
    prev_marbles = None

    for i, p in enumerate(res.data):
        if p["marbles"] != prev_marbles:
            rank = i + 1
        prev_marbles = p["marbles"]
        prefix = medals[rank - 1] if rank <= 3 else f"`{rank}.`"
        lines.append(f"{prefix} **{p['display_name']}** — {p['marbles']} marble(s)")

    await ctx.send("🔮 **Marbles Leaderboard**\n" + "\n".join(lines))


# ==============================================================
#  DAILY / BONUS MARBLE
# ==============================================================

@tasks.loop(hours=24)
async def midnight_marble_drop():
    res = supabase.table("players").select("user_id, marbles").execute()
    for p in res.data:
        update_player(p["user_id"], {"marbles": p["marbles"] + 1})
    now_est = datetime.now(EST)
    print(f"[{now_est.strftime('%Y-%m-%d %H:%M')} EST] Midnight marble drop — {len(res.data)} players updated.")


@midnight_marble_drop.before_loop
async def before_midnight_drop():
    await bot.wait_until_ready()
    now_est = datetime.now(EST)
    midnight = now_est.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = midnight + timedelta(days=1)
    wait_seconds = (next_midnight - now_est).total_seconds()
    print(f"Next marble drop in {wait_seconds/3600:.2f} hours.")
    await asyncio.sleep(wait_seconds)


@bot.command()
async def bonusmarble(ctx):
    uid = str(ctx.author.id)
    player = get_player(uid)
    if not player:
        await ctx.send(f"{ctx.author.mention} You haven't joined yet. Run `!join` first!")
        return
    if player["marbles"] > 0:
        await ctx.send(
            f"{ctx.author.mention} You still have **{player['marbles']} marble(s)** — "
            f"`!bonusmarble` is only for players at 0!"
        )
        return
    today = date.today().isoformat()
    if player["last_bonus_marble"] == today:
        await ctx.send(
            f"{ctx.author.mention} You already claimed your bonus marble today. "
            f"Hang tight until midnight! 🕛"
        )
        return
    update_player(uid, {"marbles": 1, "last_bonus_marble": today})
    await ctx.send(
        f"🆘 {ctx.author.mention} has been thrown a lifeline — **+1 bonus marble!** "
        f"Don't waste it. Marbles match??"
    )


# ==============================================================
#  MATCH TIMEOUT BACKGROUND TASK
# ==============================================================

@tasks.loop(minutes=5)
async def match_timeout_check():
    """
    Runs every 5 minutes. Handles two timeout scenarios:

    1. Both players silent for NO_VOTE_CANCEL_MINUTES (2 hrs)
       → Cancel the match, no marble transfer.

    2. Exactly one vote in for ONE_VOTE_WARNING_MINUTES (1 hr), warning not yet sent
       → Ping the non-voter with a 30-minute warning.

    3. Exactly one vote in for ONE_VOTE_AWARD_MINUTES (1.5 hrs)
       → Auto-award the match to the player who voted.
    """
    res = supabase.table("challenges").select("*").eq("status", "active").execute()

    for ch in res.data:
        if not ch.get("accepted_at"):
            continue

        elapsed = minutes_since(ch["accepted_at"])
        cv = ch["challenger_vote"]
        ov = ch["opponent_vote"]
        has_challenger_vote = cv is not None
        has_opponent_vote = ov is not None
        vote_count = sum([has_challenger_vote, has_opponent_vote])

        challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
        opponent_user = await bot.fetch_user(int(ch["opponent_id"]))

        # Find a channel to post in — use the guild's system channel or first available text channel
        # We store the channel_id on the challenge (see note below)
        channel = None
        if ch.get("channel_id"):
            channel = bot.get_channel(int(ch["channel_id"]))

        if not channel:
            continue  # Can't message without a channel reference

        # ── Stage 1: No votes at all after 2 hours ──
        if vote_count == 0 and elapsed >= NO_VOTE_CANCEL_MINUTES:
            update_player(ch["challenger_id"], {"in_match": False})
            update_player(ch["opponent_id"], {"in_match": False})
            update_challenge(ch["id"], {"status": "cancelled"})
            await channel.send(
                f"⏰ {challenger_user.mention} {opponent_user.mention} — "
                f"Your match was automatically cancelled after {NO_VOTE_CANCEL_MINUTES // 60} hours "
                f"with no result reported. No marbles were transferred. "
                f"Run `!challenge` again when you're ready!"
            )

        # ── Stage 2: One vote in, warning not yet sent, 1 hour elapsed ──
        elif vote_count == 1 and not ch.get("vote_warning_sent") and elapsed >= ONE_VOTE_WARNING_MINUTES:
            # Figure out who hasn't voted
            if has_challenger_vote and not has_opponent_vote:
                slow_user = opponent_user
            else:
                slow_user = challenger_user

            remaining = ONE_VOTE_AWARD_MINUTES - ONE_VOTE_WARNING_MINUTES
            update_challenge(ch["id"], {"vote_warning_sent": True})
            await channel.send(
                f"⏰ {slow_user.mention} — your match result is waiting on you! "
                f"Your opponent already submitted their vote. "
                f"Run `!winner @player` in the next **{remaining} minutes** "
                f"or the match will be awarded to your opponent automatically."
            )

        # ── Stage 3: One vote in, warning already sent, 1.5 hours elapsed ──
        elif vote_count == 1 and ch.get("vote_warning_sent") and elapsed >= ONE_VOTE_AWARD_MINUTES:
            # Award to whoever voted
            if has_challenger_vote:
                winner_id = ch["challenger_id"]
                loser_id = ch["opponent_id"]
            else:
                winner_id = ch["opponent_id"]
                loser_id = ch["challenger_id"]

            winner_player = get_player(winner_id)
            loser_player = get_player(loser_id)
            total = winner_player["marbles"] + loser_player["marbles"]
            update_player(winner_id, {"marbles": total, "in_match": False})
            update_player(loser_id, {"marbles": 0, "in_match": False})
            update_challenge(ch["id"], {"status": "complete"})

            winner_user = await bot.fetch_user(int(winner_id))
            loser_user = await bot.fetch_user(int(loser_id))
            await channel.send(
                f"⏰ Time's up! {loser_user.mention} never reported a result.\n"
                f"🏆 **{winner_user.display_name}** is awarded the match by default "
                f"and wins **{total} marble(s)**!"
            )


@match_timeout_check.before_loop
async def before_timeout_check():
    await bot.wait_until_ready()


# ==============================================================
#  BEG COMMAND
# ==============================================================

@bot.command()
async def beg(ctx, target: discord.Member):
    uid = str(ctx.author.id)
    tid = str(target.id)

    if uid == tid:
        await ctx.send(f"{ctx.author.mention} You can't beg yourself... 😐")
        return

    beggar = get_player(uid)
    target_player = get_player(tid)

    if not beggar:
        await ctx.send(f"{ctx.author.mention} You haven't joined yet. Run `!join` first!")
        return
    if not target_player:
        await ctx.send(f"{target.display_name} hasn't joined the marbles system yet.")
        return
    if beggar["marbles"] > 0:
        await ctx.send(
            f"{ctx.author.mention} You have **{beggar['marbles']} marble(s)** — "
            f"you're not broke enough to beg!"
        )
        return
    if target_player["marbles"] < 1:
        await ctx.send(
            f"{ctx.author.mention} {target.display_name} is also broke. "
            f"The blind leading the blind out here."
        )
        return

    view = BegView(beggar_id=ctx.author.id, target_id=target.id)
    await ctx.send(
        f"🙏 **{ctx.author.display_name}** is down to 0 marbles and is begging "
        f"**{target.display_name}** for 1 marble...\n"
        f"({target.display_name} — will you show mercy?)",
        view=view
    )


# ==============================================================
#  CHALLENGE COMMANDS
# ==============================================================

@bot.command()
async def challenge(ctx, opponent: discord.Member):
    uid = str(ctx.author.id)
    oid = str(opponent.id)

    if uid == oid:
        await ctx.send(f"{ctx.author.mention} You can't challenge yourself! 😅")
        return

    challenger = get_player(uid)
    opp_player = get_player(oid)

    if not challenger:
        await ctx.send(f"{ctx.author.mention} You haven't joined yet. Run `!join` first!")
        return
    if not opp_player:
        await ctx.send(f"{opponent.display_name} hasn't joined yet. They need to run `!join`!")
        return
    if challenger["marbles"] == 0:
        await ctx.send(
            f"{ctx.author.mention} You have **0 marbles!**\n"
            f"• Use `!bonusmarble` for a free emergency marble (once per day)\n"
            f"• Use `!beg @{opponent.display_name}` to ask them for 1 marble\n"
            f"• Or wait for the midnight marble drop 🕛"
        )
        return
    if challenger["in_match"]:
        await ctx.send(f"{ctx.author.mention} You're already in an active match!")
        return
    if opp_player["in_match"]:
        await ctx.send(f"{opponent.display_name} is already in an active match!")
        return
    if get_active_challenge(uid):
        await ctx.send(f"{ctx.author.mention} You already have a pending challenge out. Use `!cancel` first.")
        return
    if get_active_challenge(oid):
        await ctx.send(f"{opponent.display_name} already has a pending challenge.")
        return

    result = supabase.table("challenges").insert({
        "challenger_id": uid,
        "opponent_id": oid,
        "status": "pending",
        "challenger_stakes": None,
        "opponent_stakes": None,
        "vote_mismatches": 0,
        "channel_id": str(ctx.channel.id),  # Save channel so timeout task can post
    }).execute()

    view = ChallengeView(challenger_id=ctx.author.id, opponent_id=opponent.id)
    await ctx.send(
        f"🔮 {ctx.author.mention} has challenged {opponent.mention} to a **MARBLES MATCH!**\n"
        f"Stakes: **ALL marbles** on the line.",
        view=view
    )


@bot.command()
async def accept(ctx):
    uid = str(ctx.author.id)
    player = get_player(uid)

    if not player:
        await ctx.send(f"{ctx.author.mention} You haven't joined yet. Run `!join` first!")
        return
    if player["marbles"] == 0:
        await ctx.send(
            f"{ctx.author.mention} You have 0 marbles — you can't accept! "
            f"Use `!bonusmarble` or wait for midnight."
        )
        return

    ch = get_active_challenge(uid)
    if not ch or ch["opponent_id"] != uid or ch["status"] != "pending":
        await ctx.send(f"{ctx.author.mention} You don't have a pending challenge to accept.")
        return

    challenger = get_player(ch["challenger_id"])
    now_utc = datetime.now(pytz.utc).isoformat()
    update_challenge(ch["id"], {
        "status": "active",
        "challenger_stakes": challenger["marbles"],
        "opponent_stakes": player["marbles"],
        "accepted_at": now_utc,
    })
    update_player(ch["challenger_id"], {"in_match": True})
    update_player(uid, {"in_match": True})

    challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
    await ctx.send(
        f"✅ {ctx.author.mention} accepted the challenge!\n"
        f"**{challenger_user.display_name}** ({challenger['marbles']} 🔮) vs "
        f"**{ctx.author.display_name}** ({player['marbles']} 🔮)\n"
        f"Go play your match, then both report `!winner @player` when done.\n\n"
        f"{get_random_quote()}"
    )


@bot.command()
async def decline(ctx):
    uid = str(ctx.author.id)
    ch = get_active_challenge(uid)
    if not ch or ch["opponent_id"] != uid or ch["status"] != "pending":
        await ctx.send(f"{ctx.author.mention} You don't have a pending challenge to decline.")
        return
    update_challenge(ch["id"], {"status": "cancelled"})
    challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
    await ctx.send(
        f"❌ {ctx.author.mention} declined the challenge from {challenger_user.mention}. "
        f"No marbles were harmed."
    )


@bot.command()
async def cancel(ctx):
    uid = str(ctx.author.id)
    ch = get_active_challenge(uid)
    if not ch or ch["challenger_id"] != uid or ch["status"] != "pending":
        await ctx.send(f"{ctx.author.mention} You don't have a pending challenge to cancel.")
        return
    update_challenge(ch["id"], {"status": "cancelled"})
    opponent_user = await bot.fetch_user(int(ch["opponent_id"]))
    await ctx.send(f"🚫 {ctx.author.mention} cancelled their challenge against {opponent_user.mention}.")


# ==============================================================
#  FORFEIT
# ==============================================================

@bot.command()
async def forfeit(ctx):
    uid = str(ctx.author.id)
    ch = get_active_challenge(uid)
    if not ch or ch["status"] != "active":
        await ctx.send(f"{ctx.author.mention} You're not in an active match to forfeit.")
        return

    winner_id = ch["opponent_id"] if uid == ch["challenger_id"] else ch["challenger_id"]
    loser_id = uid
    winner_player = get_player(winner_id)
    loser_player = get_player(loser_id)
    total = winner_player["marbles"] + loser_player["marbles"]
    update_player(winner_id, {"marbles": total, "in_match": False})
    update_player(loser_id, {"marbles": 0, "in_match": False})
    update_challenge(ch["id"], {"status": "complete"})

    winner_user = await bot.fetch_user(int(winner_id))
    await ctx.send(
        f"🏳️ {ctx.author.mention} has forfeited the match.\n"
        f"**{winner_user.display_name}** wins **{total} marble(s)** by default!"
    )


# ==============================================================
#  WINNER REPORTING
# ==============================================================

@bot.command()
async def winner(ctx, reported_winner: discord.Member):
    uid = str(ctx.author.id)
    rwid = str(reported_winner.id)

    ch = get_active_challenge(uid)
    if not ch or ch["status"] != "active":
        await ctx.send(f"{ctx.author.mention} You're not in an active match right now.")
        return
    if rwid not in [ch["challenger_id"], ch["opponent_id"]]:
        await ctx.send(f"{ctx.author.mention} You can only vote for one of the two players in the match!")
        return

    if uid == ch["challenger_id"]:
        update_challenge(ch["id"], {"challenger_vote": rwid})
    else:
        update_challenge(ch["id"], {"opponent_vote": rwid})

    res = supabase.table("challenges").select("*").eq("id", ch["id"]).execute()
    ch = res.data[0]
    cv = ch["challenger_vote"]
    ov = ch["opponent_vote"]

    if cv and ov:
        challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
        opponent_user = await bot.fetch_user(int(ch["opponent_id"]))

        if cv == ov:
            winner_id = cv
            loser_id = ch["opponent_id"] if winner_id == ch["challenger_id"] else ch["challenger_id"]
            winner_player = get_player(winner_id)
            loser_player = get_player(loser_id)
            total = winner_player["marbles"] + loser_player["marbles"]
            update_player(winner_id, {"marbles": total, "in_match": False})
            update_player(loser_id, {"marbles": 0, "in_match": False})
            update_challenge(ch["id"], {"status": "complete"})
            winner_user = await bot.fetch_user(int(winner_id))
            loser_user = await bot.fetch_user(int(loser_id))
            await ctx.send(
                f"🏆 Match confirmed! **{winner_user.display_name}** wins **{total} marble(s)!**\n"
                f"{loser_user.mention} has been cleaned out. Marbles match??"
            )
        else:
            mismatches = ch["vote_mismatches"] + 1
            update_challenge(ch["id"], {
                "vote_mismatches": mismatches,
                "challenger_vote": None,
                "opponent_vote": None,
            })
            if mismatches == 1:
                await ctx.send(
                    f"⚠️ {challenger_user.mention} {opponent_user.mention} — "
                    f"Votes didn't match! Figure it out and vote again with `!winner @player`."
                )
            elif mismatches == 2:
                await ctx.send(
                    f"🚨 {challenger_user.mention} {opponent_user.mention} — "
                    f"Votes mismatched again! **Final warning:** next mismatch and "
                    f"**BOTH players lose ALL their marbles.** 🔮"
                )
            else:
                update_player(ch["challenger_id"], {"marbles": 0, "in_match": False})
                update_player(ch["opponent_id"], {"marbles": 0, "in_match": False})
                update_challenge(ch["id"], {"status": "complete"})
                await ctx.send(
                    f"💀 {challenger_user.mention} {opponent_user.mention} — "
                    f"Three mismatched votes. **BOTH players set to 0 marbles.** "
                    f"Learn to agree next time. 🔮"
                )
    else:
        await ctx.send(
            f"✅ {ctx.author.mention} submitted their vote. "
            f"Waiting on the other player to run `!winner @player`."
        )


# ==============================================================
#  ADMIN COMMANDS
# ==============================================================

@bot.command()
async def give(ctx, member: discord.Member, amount: int):
    if not is_marble_admin(ctx):
        await ctx.send(f"{ctx.author.mention} You need the **Marble Admin** role to do that.")
        return
    if amount <= 0:
        await ctx.send("Amount must be a positive number.")
        return
    player = get_player(str(member.id))
    if not player:
        await ctx.send(f"{member.display_name} hasn't joined yet.")
        return
    new_total = player["marbles"] + amount
    update_player(str(member.id), {"marbles": new_total})
    await ctx.send(f"✅ Gave **{amount}** marble(s) to {member.mention}. They now have **{new_total}**.")


@bot.command()
async def take(ctx, member: discord.Member, amount: int):
    if not is_marble_admin(ctx):
        await ctx.send(f"{ctx.author.mention} You need the **Marble Admin** role to do that.")
        return
    if amount <= 0:
        await ctx.send("Amount must be a positive number.")
        return
    player = get_player(str(member.id))
    if not player:
        await ctx.send(f"{member.display_name} hasn't joined yet.")
        return
    new_total = max(0, player["marbles"] - amount)
    update_player(str(member.id), {"marbles": new_total})
    await ctx.send(f"✅ Took **{amount}** marble(s) from {member.mention}. They now have **{new_total}**.")


@bot.command()
async def setmarbles(ctx, member: discord.Member, amount: int):
    if not is_marble_admin(ctx):
        await ctx.send(f"{ctx.author.mention} You need the **Marble Admin** role to do that.")
        return
    if amount < 0:
        await ctx.send("Amount can't be negative.")
        return
    player = get_player(str(member.id))
    if not player:
        await ctx.send(f"{member.display_name} hasn't joined yet.")
        return
    update_player(str(member.id), {"marbles": amount})
    await ctx.send(f"✅ Set {member.mention}'s marbles to **{amount}**.")


@bot.command()
async def cancelmatch(ctx, member: discord.Member):
    """[Marble Admin] Cancel an active or pending match with no marble transfer."""
    if not is_marble_admin(ctx):
        await ctx.send(f"{ctx.author.mention} You need the **Marble Admin** role to do that.")
        return
    ch = get_active_challenge(str(member.id))
    if not ch:
        await ctx.send(f"{member.display_name} isn't in an active or pending match.")
        return
    if ch["status"] == "active":
        update_player(ch["challenger_id"], {"in_match": False})
        update_player(ch["opponent_id"], {"in_match": False})
    update_challenge(ch["id"], {"status": "cancelled"})
    challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
    opponent_user = await bot.fetch_user(int(ch["opponent_id"]))
    await ctx.send(
        f"🛑 Match between **{challenger_user.display_name}** and **{opponent_user.display_name}** "
        f"cancelled by an admin. No marbles transferred."
    )


# ==============================================================
#  RUN
# ==============================================================
bot.run(BOT_TOKEN)
