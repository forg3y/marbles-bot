import discord
from discord.ext import tasks
from discord import ui, app_commands
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, date, timedelta
from typing import Optional
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
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

EST = pytz.timezone("America/New_York")

# Timeout settings (in minutes)
NO_VOTE_CANCEL_MINUTES   = 120
ONE_VOTE_WARNING_MINUTES = 60
ONE_VOTE_AWARD_MINUTES   = 90


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


def is_marble_admin(interaction: discord.Interaction) -> bool:
    return discord.utils.get(interaction.user.roles, name="Marble Admin") is not None


def get_random_quote() -> str:
    try:
        with open("quotes.csv", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="|")
            quotes = list(reader)
        if not quotes:
            return ""
        pick = random.choice(quotes)
        return f'*"{pick["quote"]}"*\n- {pick["author"]}'
    except FileNotFoundError:
        return ""


def minutes_since(timestamp_str: str) -> float:
    if not timestamp_str:
        return 0
    ts = datetime.fromisoformat(timestamp_str)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=pytz.utc)
    now = datetime.now(pytz.utc)
    return (now - ts).total_seconds() / 60


def get_rank_title(p: dict) -> str:
    """Return a fun rank title based on player stats. Multiple can apply - returns the most notable."""
    titles = []
    if (p.get("peak_marbles") or 0) >= 1000:
        titles.append("🐉 Dragon Hoard")
    if (p.get("wins") or 0) >= 10:
        titles.append("🏆 Veteran")
    if (p.get("current_streak") or 0) >= 3:
        titles.append("📈 On a Roll")
    if (p.get("current_streak") or 0) <= -3:
        titles.append("📉 Not On a Roll")
    if (p.get("total_matches") or 0) >= 20:
        titles.append("🎰 Degenerate Gambler")
    if (p.get("times_gave_beg") or 0) >= 5:
        titles.append("😇 Saint")
    if (p.get("times_begged") or 0) >= 10:
        titles.append("🤲 Charity Case")
    if (p.get("times_broke") or 0) >= 5:
        titles.append("🪨 Broke Enthusiast")
    if (p.get("total_matches") or 0) < 3:
        titles.append("🐣 Newcomer")
    return titles[0] if titles else "🔮 Marble Player"


def apply_match_result(winner_id: str, loser_id: str, marbles_won: int, marbles_lost: int):
    """Update all stats for both players after a confirmed match result."""
    winner = get_player(winner_id)
    loser = get_player(loser_id)

    new_winner_marbles = winner["marbles"] + loser["marbles"]
    winner_streak = (winner.get("current_streak") or 0)
    new_winner_streak = winner_streak + 1 if winner_streak >= 0 else 1
    new_peak = max(winner.get("peak_marbles") or 0, new_winner_marbles)

    update_player(winner_id, {
        "marbles": new_winner_marbles,
        "in_match": False,
        "wins": (winner.get("wins") or 0) + 1,
        "total_matches": (winner.get("total_matches") or 0) + 1,
        "current_streak": new_winner_streak,
        "best_win": max(winner.get("best_win") or 0, marbles_won),
        "peak_marbles": new_peak,
        "marbles_won_gambling": (winner.get("marbles_won_gambling") or 0) + marbles_won,
    })

    loser_streak = (loser.get("current_streak") or 0)
    new_loser_streak = loser_streak - 1 if loser_streak <= 0 else -1
    new_times_broke = (loser.get("times_broke") or 0) + (1 if loser["marbles"] > 0 else 0)

    update_player(loser_id, {
        "marbles": 0,
        "in_match": False,
        "losses": (loser.get("losses") or 0) + 1,
        "total_matches": (loser.get("total_matches") or 0) + 1,
        "current_streak": new_loser_streak,
        "worst_loss": max(loser.get("worst_loss") or 0, marbles_lost),
        "times_broke": new_times_broke,
        "marbles_lost_gambling": (loser.get("marbles_lost_gambling") or 0) + marbles_lost,
    })


# ==============================================================
#  VIEWS (Buttons)
# ==============================================================

class ChallengeView(ui.View):
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
                "You have 0 marbles - you can't accept right now! "
                "Use `/bonusmarble` or wait for the midnight drop.",
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
            f"Go play your match, then both report `/winner` when done.\n\n"
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
                "They already have marbles - no need!", ephemeral=True
            )
            return

        update_player(str(self.target_id), {
            "marbles": giver["marbles"] - 1,
            "times_gave_beg": (giver.get("times_gave_beg") or 0) + 1,
        })
        update_player(str(self.beggar_id), {
            "marbles": beggar["marbles"] + 1,
            "times_begged": (beggar.get("times_begged") or 0) + 1,
        })

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
    await tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Slash commands synced.")
    print("Marbles bot is online!")
    midnight_marble_drop.start()
    match_timeout_check.start()


# ==============================================================
#  PLAYER COMMANDS
# ==============================================================

@tree.command(name="join", description="Join the marbles system and start with 10 marbles.")
async def join(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    if get_player(uid):
        await interaction.response.send_message(
            f"{interaction.user.mention} You're already in the marbles system! 🔮"
        )
        return

    supabase.table("players").insert({
        "user_id": uid,
        "display_name": interaction.user.display_name,
        "marbles": 10,
        "in_match": False,
        "wins": 0,
        "losses": 0,
        "total_matches": 0,
        "current_streak": 0,
        "best_win": 0,
        "worst_loss": 0,
        "peak_marbles": 10,
        "times_broke": 0,
        "times_used_bonus": 0,
        "times_begged": 0,
        "times_gave_beg": 0,
        "marbles_from_daily": 0,
        "marbles_won_gambling": 0,
        "marbles_lost_gambling": 0,
    }).execute()

    await interaction.response.send_message(
        f"🔮 Welcome to the marbles arena, {interaction.user.mention}! "
        f"You start with **10 marbles**. Good luck."
    )


@tree.command(name="marbles", description="Check your own or another player's marble count.")
async def marbles(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    player = get_player(str(target.id))
    if not player:
        await interaction.response.send_message(
            f"{target.display_name} hasn't joined yet. Tell them to run `/join`!"
        )
        return
    await interaction.response.send_message(
        f"🔮 **{target.display_name}** has **{player['marbles']} marble(s)**."
    )


@tree.command(name="leaderboard", description="Show all players ranked by marble count.")
async def leaderboard(interaction: discord.Interaction):
    res = supabase.table("players").select("*").order("marbles", desc=True).execute()
    if not res.data:
        await interaction.response.send_message("Nobody has joined yet! Run `/join` to start.")
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
        lines.append(f"{prefix} **{p['display_name']}** - {p['marbles']} marble(s)")

    await interaction.response.send_message("🔮 **Marbles Leaderboard**\n" + "\n".join(lines))


# ==============================================================
#  STATS COMMAND
# ==============================================================

@tree.command(name="stats", description="View a player's stat card.")
async def stats(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    p = get_player(str(target.id))

    if not p:
        await interaction.response.send_message(
            f"{target.display_name} hasn't joined yet.", ephemeral=True
        )
        return

    wins = p.get("wins") or 0
    losses = p.get("losses") or 0
    total = p.get("total_matches") or 0
    win_rate = f"{(wins / total * 100):.1f}%" if total > 0 else "N/A"
    streak = p.get("current_streak") or 0
    if streak > 0:
        streak_str = f"🔥 {streak}W"
    elif streak < 0:
        streak_str = f"❄️ {abs(streak)}L"
    else:
        streak_str = "None"

    daily = p.get("marbles_from_daily") or 0
    gambling_won = p.get("marbles_won_gambling") or 0
    gambling_lost = p.get("marbles_lost_gambling") or 0
    net_gambling = gambling_won - gambling_lost
    if daily > 0 and net_gambling != 0:
        ratio = f"+{net_gambling} from gambling vs +{daily} from dailies"
    else:
        ratio = "Not enough data yet"

    rank_title = get_rank_title(p)

    embed = discord.Embed(
        title=f"{target.display_name}'s Marble Stats",
        description=f"{rank_title}",
        color=discord.Color.purple()
    )

    embed.add_field(
        name="💰 Current Standing",
        value=(
            f"Marbles: **{p['marbles']}**\n"
            f"Peak ever: **{p.get('peak_marbles') or 0}**"
        ),
        inline=True
    )

    embed.add_field(
        name="⚔️ Match Record",
        value=(
            f"W/L: **{wins}/{losses}**\n"
            f"Win rate: **{win_rate}**\n"
            f"Total played: **{total}**\n"
            f"Streak: **{streak_str}**"
        ),
        inline=True
    )

    embed.add_field(
        name="📊 Biggest Moments",
        value=(
            f"Biggest win: **{p.get('best_win') or 0}** marbles\n"
            f"Worst loss: **{p.get('worst_loss') or 0}** marbles"
        ),
        inline=False
    )

    embed.add_field(
        name="📈 Marble Income",
        value=(
            f"From dailies: **{daily}**\n"
            f"Won gambling: **{gambling_won}**\n"
            f"Lost gambling: **{gambling_lost}**\n"
            f"Net gambling: **{'+' if net_gambling >= 0 else ''}{net_gambling}**"
        ),
        inline=True
    )

    embed.add_field(
        name="📉 Misfortune",
        value=(
            f"Times broke: **{p.get('times_broke') or 0}**\n"
            f"Bonus marbles used: **{p.get('times_used_bonus') or 0}**\n"
            f"Times begged: **{p.get('times_begged') or 0}**\n"
            f"Times gave beg: **{p.get('times_gave_beg') or 0}**"
        ),
        inline=True
    )

    await interaction.response.send_message(embed=embed)


# ==============================================================
#  HELP COMMAND
# ==============================================================

@tree.command(name="help", description="Show all available commands.")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔮 Marbles Bot",
        description=(
            "Everyone starts with **10 marbles**. Challenge other players and bet ALL your marbles "
            "on the outcome. The winner takes everything.\n\n"
            "**Reporting results:** after a match, both players run `/winner` and must vote for the "
            "same person. If votes don't match, you get a warning and vote again. Second mismatch "
            "gets a final warning. Third mismatch and **both players lose all their marbles.**\n\n"
            "**Running out:** you get +1 marble automatically every midnight EST. If you hit 0, "
            "use `/bonusmarble` once per day, or `/beg` another player for one."
        ),
        color=discord.Color.purple()
    )

    embed.add_field(name="👤 Player", value=(
        "`/join` - Join the marbles system (starts you at 10)\n"
        "`/marbles` - Check your marble count\n"
        "`/marbles @user` - Check someone else's count\n"
        "`/leaderboard` - See everyone ranked\n"
        "`/stats` - View your stat card\n"
        "`/stats @user` - View someone else's stat card"
    ), inline=False)

    embed.add_field(name="⚔️ Challenges", value=(
        "`/challenge @user` - Challenge someone (all marbles on the line)\n"
        "`/accept` - Accept a pending challenge\n"
        "`/decline` - Decline a pending challenge\n"
        "`/cancel` - Cancel your outgoing challenge\n"
        "`/forfeit` - Forfeit your active match (you lose)"
    ), inline=False)

    embed.add_field(name="🏆 Results", value=(
        "`/winner @user` - Report who won your match\n"
        "Both players must submit matching votes to confirm"
    ), inline=False)

    embed.add_field(name="💎 Marbles Income", value=(
        "Automatic +1 marble for everyone at midnight EST\n"
        "`/bonusmarble` - Emergency marble if you're at 0 (once/day)\n"
        "`/beg @user` - Ask another player for 1 marble when broke"
    ), inline=False)

    embed.add_field(name="🛡️ Admin (Marble Admin role required)", value=(
        "`/give @user amount` - Add marbles to a player\n"
        "`/take @user amount` - Remove marbles from a player\n"
        "`/setmarbles @user amount` - Set a player's exact count\n"
        "`/cancelmatch @user` - Cancel a match with no transfer"
    ), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==============================================================
#  DAILY / BONUS MARBLE
# ==============================================================

@tasks.loop(hours=24)
async def midnight_marble_drop():
    res = supabase.table("players").select("user_id, marbles, marbles_from_daily").execute()
    for p in res.data:
        update_player(p["user_id"], {
            "marbles": p["marbles"] + 1,
            "marbles_from_daily": (p.get("marbles_from_daily") or 0) + 1,
        })
    now_est = datetime.now(EST)
    print(f"[{now_est.strftime('%Y-%m-%d %H:%M')} EST] Midnight marble drop - {len(res.data)} players updated.")


@midnight_marble_drop.before_loop
async def before_midnight_drop():
    await bot.wait_until_ready()
    now_est = datetime.now(EST)
    midnight = now_est.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = midnight + timedelta(days=1)
    wait_seconds = (next_midnight - now_est).total_seconds()
    print(f"Next marble drop in {wait_seconds/3600:.2f} hours.")
    await asyncio.sleep(wait_seconds)


@tree.command(name="bonusmarble", description="Claim an emergency marble if you're at 0 (once per day).")
async def bonusmarble(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    player = get_player(uid)
    if not player:
        await interaction.response.send_message(
            f"{interaction.user.mention} You haven't joined yet. Run `/join` first!"
        )
        return
    if player["marbles"] > 0:
        await interaction.response.send_message(
            f"{interaction.user.mention} You still have **{player['marbles']} marble(s)** - "
            f"`/bonusmarble` is only for players at 0!"
        )
        return
    today = date.today().isoformat()
    if player["last_bonus_marble"] == today:
        await interaction.response.send_message(
            f"{interaction.user.mention} You already claimed your bonus marble today. "
            f"Hang tight until midnight! 🕛"
        )
        return
    update_player(uid, {
        "marbles": 1,
        "last_bonus_marble": today,
        "times_used_bonus": (player.get("times_used_bonus") or 0) + 1,
    })
    await interaction.response.send_message(
        f"🆘 {interaction.user.mention} has been thrown a lifeline - **+1 bonus marble!** "
        f"Don't waste it. Marbles match??"
    )


# ==============================================================
#  MATCH TIMEOUT BACKGROUND TASK
# ==============================================================

@tasks.loop(minutes=5)
async def match_timeout_check():
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

        channel = None
        if ch.get("channel_id"):
            channel = bot.get_channel(int(ch["channel_id"]))
        if not channel:
            continue

        challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
        opponent_user = await bot.fetch_user(int(ch["opponent_id"]))

        # Stage 1: No votes after 2 hours - cancel, no transfer
        if vote_count == 0 and elapsed >= NO_VOTE_CANCEL_MINUTES:
            update_player(ch["challenger_id"], {"in_match": False})
            update_player(ch["opponent_id"], {"in_match": False})
            update_challenge(ch["id"], {"status": "cancelled"})
            await channel.send(
                f"⏰ {challenger_user.mention} {opponent_user.mention} - "
                f"Your match was automatically cancelled after {NO_VOTE_CANCEL_MINUTES // 60} hours "
                f"with no result reported. No marbles transferred. "
                f"Run `/challenge` again when you're ready!"
            )

        # Stage 2: One vote in, no warning yet, 1 hour elapsed - warn
        elif vote_count == 1 and not ch.get("vote_warning_sent") and elapsed >= ONE_VOTE_WARNING_MINUTES:
            slow_user = opponent_user if has_challenger_vote else challenger_user
            remaining = ONE_VOTE_AWARD_MINUTES - ONE_VOTE_WARNING_MINUTES
            update_challenge(ch["id"], {"vote_warning_sent": True})
            await channel.send(
                f"⏰ {slow_user.mention} - your match result is waiting on you! "
                f"Your opponent already submitted their vote. "
                f"Run `/winner` in the next **{remaining} minutes** "
                f"or the match will be awarded to your opponent automatically."
            )

        # Stage 3: One vote in, warning sent, 1.5 hours elapsed - auto-award
        elif vote_count == 1 and ch.get("vote_warning_sent") and elapsed >= ONE_VOTE_AWARD_MINUTES:
            winner_id = ch["challenger_id"] if has_challenger_vote else ch["opponent_id"]
            loser_id = ch["opponent_id"] if has_challenger_vote else ch["challenger_id"]
            winner_player = get_player(winner_id)
            loser_player = get_player(loser_id)
            marbles_won = loser_player["marbles"]
            marbles_lost = loser_player["marbles"]
            apply_match_result(winner_id, loser_id, marbles_won, marbles_lost)
            update_challenge(ch["id"], {"status": "complete"})

            winner_user = await bot.fetch_user(int(winner_id))
            loser_user = await bot.fetch_user(int(loser_id))
            total = winner_player["marbles"] + loser_player["marbles"]
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

@tree.command(name="beg", description="Beg another player for 1 marble when you're at 0.")
async def beg(interaction: discord.Interaction, target: discord.Member):
    uid = str(interaction.user.id)
    tid = str(target.id)

    if uid == tid:
        await interaction.response.send_message(
            f"{interaction.user.mention} You can't beg yourself... 😐"
        )
        return

    beggar = get_player(uid)
    target_player = get_player(tid)

    if not beggar:
        await interaction.response.send_message(
            f"{interaction.user.mention} You haven't joined yet. Run `/join` first!"
        )
        return
    if not target_player:
        await interaction.response.send_message(
            f"{target.display_name} hasn't joined the marbles system yet."
        )
        return
    if beggar["marbles"] > 0:
        await interaction.response.send_message(
            f"{interaction.user.mention} You have **{beggar['marbles']} marble(s)** - "
            f"you're not broke enough to beg!"
        )
        return
    if target_player["marbles"] < 1:
        await interaction.response.send_message(
            f"{interaction.user.mention} {target.display_name} is also broke. "
            f"The blind leading the blind out here."
        )
        return

    view = BegView(beggar_id=interaction.user.id, target_id=target.id)
    await interaction.response.send_message(
        f"🙏 **{interaction.user.display_name}** is down to 0 marbles and is begging "
        f"**{target.display_name}** for 1 marble...\n"
        f"({target.display_name} - will you show mercy?)",
        view=view
    )


# ==============================================================
#  CHALLENGE COMMANDS
# ==============================================================

@tree.command(name="challenge", description="Challenge another player - all your marbles on the line.")
async def challenge(interaction: discord.Interaction, opponent: discord.Member):
    uid = str(interaction.user.id)
    oid = str(opponent.id)

    if uid == oid:
        await interaction.response.send_message(
            f"{interaction.user.mention} You can't challenge yourself! 😅"
        )
        return

    challenger = get_player(uid)
    opp_player = get_player(oid)

    if not challenger:
        await interaction.response.send_message(
            f"{interaction.user.mention} You haven't joined yet. Run `/join` first!"
        )
        return
    if not opp_player:
        await interaction.response.send_message(
            f"{opponent.display_name} hasn't joined yet. They need to run `/join`!"
        )
        return
    if challenger["marbles"] == 0:
        await interaction.response.send_message(
            f"{interaction.user.mention} You have **0 marbles!**\n"
            f"- Use `/bonusmarble` for a free emergency marble (once per day)\n"
            f"- Use `/beg` to ask {opponent.display_name} for 1 marble\n"
            f"- Or wait for the midnight marble drop 🕛"
        )
        return
    if challenger["in_match"]:
        await interaction.response.send_message(
            f"{interaction.user.mention} You're already in an active match!"
        )
        return
    if opp_player["in_match"]:
        await interaction.response.send_message(
            f"{opponent.display_name} is already in an active match!"
        )
        return
    if get_active_challenge(uid):
        await interaction.response.send_message(
            f"{interaction.user.mention} You already have a pending challenge out. Use `/cancel` first."
        )
        return
    if get_active_challenge(oid):
        await interaction.response.send_message(
            f"{opponent.display_name} already has a pending challenge."
        )
        return

    supabase.table("challenges").insert({
        "challenger_id": uid,
        "opponent_id": oid,
        "status": "pending",
        "challenger_stakes": None,
        "opponent_stakes": None,
        "vote_mismatches": 0,
        "channel_id": str(interaction.channel_id),
    }).execute()

    view = ChallengeView(challenger_id=interaction.user.id, opponent_id=opponent.id)
    await interaction.response.send_message(
        f"🔮 {interaction.user.mention} has challenged {opponent.mention} to a **MARBLES MATCH!**\n"
        f"Stakes: **ALL marbles** on the line.",
        view=view
    )


@tree.command(name="accept", description="Accept a pending challenge.")
async def accept(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    player = get_player(uid)

    if not player:
        await interaction.response.send_message(
            f"{interaction.user.mention} You haven't joined yet. Run `/join` first!"
        )
        return
    if player["marbles"] == 0:
        await interaction.response.send_message(
            f"{interaction.user.mention} You have 0 marbles - you can't accept! "
            f"Use `/bonusmarble` or wait for midnight."
        )
        return

    ch = get_active_challenge(uid)
    if not ch or ch["opponent_id"] != uid or ch["status"] != "pending":
        await interaction.response.send_message(
            f"{interaction.user.mention} You don't have a pending challenge to accept."
        )
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
    await interaction.response.send_message(
        f"✅ {interaction.user.mention} accepted the challenge!\n"
        f"**{challenger_user.display_name}** ({challenger['marbles']} 🔮) vs "
        f"**{interaction.user.display_name}** ({player['marbles']} 🔮)\n"
        f"Go play your match, then both report `/winner` when done.\n\n"
        f"{get_random_quote()}"
    )


@tree.command(name="decline", description="Decline a pending challenge.")
async def decline(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    ch = get_active_challenge(uid)
    if not ch or ch["opponent_id"] != uid or ch["status"] != "pending":
        await interaction.response.send_message(
            f"{interaction.user.mention} You don't have a pending challenge to decline."
        )
        return
    update_challenge(ch["id"], {"status": "cancelled"})
    challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
    await interaction.response.send_message(
        f"❌ {interaction.user.mention} declined the challenge from {challenger_user.mention}. "
        f"No marbles were harmed."
    )


@tree.command(name="cancel", description="Cancel your outgoing pending challenge.")
async def cancel(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    ch = get_active_challenge(uid)
    if not ch or ch["challenger_id"] != uid or ch["status"] != "pending":
        await interaction.response.send_message(
            f"{interaction.user.mention} You don't have a pending challenge to cancel."
        )
        return
    update_challenge(ch["id"], {"status": "cancelled"})
    opponent_user = await bot.fetch_user(int(ch["opponent_id"]))
    await interaction.response.send_message(
        f"🚫 {interaction.user.mention} cancelled their challenge against {opponent_user.mention}."
    )


@tree.command(name="forfeit", description="Forfeit your active match. You lose your marbles.")
async def forfeit(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    ch = get_active_challenge(uid)
    if not ch or ch["status"] != "active":
        await interaction.response.send_message(
            f"{interaction.user.mention} You're not in an active match to forfeit."
        )
        return

    winner_id = ch["opponent_id"] if uid == ch["challenger_id"] else ch["challenger_id"]
    loser_id = uid
    winner_player = get_player(winner_id)
    loser_player = get_player(loser_id)
    marbles_changing = loser_player["marbles"]
    total = winner_player["marbles"] + loser_player["marbles"]
    apply_match_result(winner_id, loser_id, marbles_changing, marbles_changing)
    update_challenge(ch["id"], {"status": "complete"})

    winner_user = await bot.fetch_user(int(winner_id))
    await interaction.response.send_message(
        f"🏳️ {interaction.user.mention} has forfeited the match.\n"
        f"**{winner_user.display_name}** wins **{total} marble(s)** by default!"
    )


# ==============================================================
#  WINNER REPORTING
# ==============================================================

@tree.command(name="winner", description="Report the winner of your match.")
async def winner(interaction: discord.Interaction, reported_winner: discord.Member):
    uid = str(interaction.user.id)
    rwid = str(reported_winner.id)

    ch = get_active_challenge(uid)
    if not ch or ch["status"] != "active":
        await interaction.response.send_message(
            f"{interaction.user.mention} You're not in an active match right now."
        )
        return
    if rwid not in [ch["challenger_id"], ch["opponent_id"]]:
        await interaction.response.send_message(
            f"{interaction.user.mention} You can only vote for one of the two players in the match!"
        )
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
            marbles_changing = loser_player["marbles"]
            total = winner_player["marbles"] + loser_player["marbles"]
            apply_match_result(winner_id, loser_id, marbles_changing, marbles_changing)
            update_challenge(ch["id"], {"status": "complete"})
            winner_user = await bot.fetch_user(int(winner_id))
            loser_user = await bot.fetch_user(int(loser_id))
            await interaction.response.send_message(
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
                await interaction.response.send_message(
                    f"⚠️ {challenger_user.mention} {opponent_user.mention} - "
                    f"Votes didn't match! Figure it out and both vote again with `/winner`."
                )
            elif mismatches == 2:
                await interaction.response.send_message(
                    f"🚨 {challenger_user.mention} {opponent_user.mention} - "
                    f"Votes mismatched again! **Final warning:** next mismatch and "
                    f"**BOTH players lose ALL their marbles.** 🔮"
                )
            else:
                winner_p = get_player(ch["challenger_id"])
                opp_p = get_player(ch["opponent_id"])
                update_player(ch["challenger_id"], {
                    "marbles": 0,
                    "in_match": False,
                    "times_broke": (winner_p.get("times_broke") or 0) + 1,
                })
                update_player(ch["opponent_id"], {
                    "marbles": 0,
                    "in_match": False,
                    "times_broke": (opp_p.get("times_broke") or 0) + 1,
                })
                update_challenge(ch["id"], {"status": "complete"})
                await interaction.response.send_message(
                    f"💀 {challenger_user.mention} {opponent_user.mention} - "
                    f"Three mismatched votes. **BOTH players set to 0 marbles.** "
                    f"Learn to agree next time. 🔮"
                )
    else:
        await interaction.response.send_message(
            f"✅ {interaction.user.mention} submitted their vote. "
            f"Waiting on the other player to run `/winner`."
        )


# ==============================================================
#  ADMIN COMMANDS
# ==============================================================

@tree.command(name="give", description="[Marble Admin] Add marbles to a player.")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not is_marble_admin(interaction):
        await interaction.response.send_message(
            f"{interaction.user.mention} You need the **Marble Admin** role to do that."
        )
        return
    if amount <= 0:
        await interaction.response.send_message("Amount must be a positive number.")
        return
    player = get_player(str(member.id))
    if not player:
        await interaction.response.send_message(f"{member.display_name} hasn't joined yet.")
        return
    new_total = player["marbles"] + amount
    update_player(str(member.id), {"marbles": new_total})
    await interaction.response.send_message(
        f"✅ Gave **{amount}** marble(s) to {member.mention}. They now have **{new_total}**."
    )


@tree.command(name="take", description="[Marble Admin] Remove marbles from a player.")
async def take(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not is_marble_admin(interaction):
        await interaction.response.send_message(
            f"{interaction.user.mention} You need the **Marble Admin** role to do that."
        )
        return
    if amount <= 0:
        await interaction.response.send_message("Amount must be a positive number.")
        return
    player = get_player(str(member.id))
    if not player:
        await interaction.response.send_message(f"{member.display_name} hasn't joined yet.")
        return
    new_total = max(0, player["marbles"] - amount)
    update_player(str(member.id), {"marbles": new_total})
    await interaction.response.send_message(
        f"✅ Took **{amount}** marble(s) from {member.mention}. They now have **{new_total}**."
    )


@tree.command(name="setmarbles", description="[Marble Admin] Set a player's marble count exactly.")
async def setmarbles(interaction: discord.Interaction, member: discord.Member, amount: int):
    if not is_marble_admin(interaction):
        await interaction.response.send_message(
            f"{interaction.user.mention} You need the **Marble Admin** role to do that."
        )
        return
    if amount < 0:
        await interaction.response.send_message("Amount can't be negative.")
        return
    player = get_player(str(member.id))
    if not player:
        await interaction.response.send_message(f"{member.display_name} hasn't joined yet.")
        return
    update_player(str(member.id), {"marbles": amount})
    await interaction.response.send_message(
        f"✅ Set {member.mention}'s marbles to **{amount}**."
    )


@tree.command(name="cancelmatch", description="[Marble Admin] Cancel a match with no marble transfer.")
async def cancelmatch(interaction: discord.Interaction, member: discord.Member):
    if not is_marble_admin(interaction):
        await interaction.response.send_message(
            f"{interaction.user.mention} You need the **Marble Admin** role to do that."
        )
        return
    ch = get_active_challenge(str(member.id))
    if not ch:
        await interaction.response.send_message(
            f"{member.display_name} isn't in an active or pending match."
        )
        return
    if ch["status"] == "active":
        update_player(ch["challenger_id"], {"in_match": False})
        update_player(ch["opponent_id"], {"in_match": False})
    update_challenge(ch["id"], {"status": "cancelled"})
    challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
    opponent_user = await bot.fetch_user(int(ch["opponent_id"]))
    await interaction.response.send_message(
        f"🛑 Match between **{challenger_user.display_name}** and **{opponent_user.display_name}** "
        f"cancelled by an admin. No marbles transferred."
    )


# ==============================================================
#  RUN
# ==============================================================
bot.run(BOT_TOKEN)
