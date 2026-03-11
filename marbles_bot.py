import discord
from discord.ext import commands, tasks
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, date, timedelta
import pytz
import asyncio
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

# ==============================================================
#  HELPER FUNCTIONS
# ==============================================================

def get_player(user_id: str):
    """Fetch a player row from Supabase. Returns None if not found."""
    res = supabase.table("players").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None


def get_active_challenge(user_id: str):
    """Get a pending or active challenge involving this user (as either side)."""
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


# ==============================================================
#  BOT EVENTS
# ==============================================================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Marbles bot is online!")
    midnight_marble_drop.start()


# ==============================================================
#  PLAYER COMMANDS
# ==============================================================

@bot.command()
async def join(ctx):
    """Register yourself in the marbles system."""
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
    """Check your own or another player's marble count."""
    target = member or ctx.author
    player = get_player(str(target.id))

    if not player:
        await ctx.send(f"{target.display_name} hasn't joined yet. Tell them to run `!join`!")
        return

    await ctx.send(f"🔮 **{target.display_name}** has **{player['marbles']} marble(s)**.")


@bot.command()
async def leaderboard(ctx):
    """Show all players ranked by marble count."""
    res = supabase.table("players").select("*").order("marbles", desc=True).execute()

    if not res.data:
        await ctx.send("Nobody has joined yet! Run `!join` to start.")
        return

    lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, p in enumerate(res.data):
        prefix = medals[i] if i < 3 else f"`{i+1}.`"
        lines.append(f"{prefix} **{p['display_name']}** — {p['marbles']} marble(s)")

    await ctx.send("🔮 **Marbles Leaderboard**\n" + "\n".join(lines))


# ==============================================================
#  DAILY / BONUS MARBLE
# ==============================================================

@tasks.loop(hours=24)
async def midnight_marble_drop():
    """Give every registered player +1 marble at midnight EST."""
    res = supabase.table("players").select("user_id, marbles").execute()
    for p in res.data:
        update_player(p["user_id"], {"marbles": p["marbles"] + 1})

    now_est = datetime.now(EST)
    print(f"[{now_est.strftime('%Y-%m-%d %H:%M')} EST] Midnight marble drop complete — {len(res.data)} players updated.")


@midnight_marble_drop.before_loop
async def before_midnight_drop():
    """Wait until the bot is ready, then sleep until the next midnight EST."""
    await bot.wait_until_ready()
    now_est = datetime.now(EST)
    midnight = now_est.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = midnight + timedelta(days=1)
    wait_seconds = (next_midnight - now_est).total_seconds()
    print(f"Next marble drop in {wait_seconds/3600:.2f} hours.")
    await asyncio.sleep(wait_seconds)


@bot.command()
async def bonusmarble(ctx):
    """Claim 1 emergency marble. Only available if you have 0 marbles, once per day."""
    uid = str(ctx.author.id)
    player = get_player(uid)

    if not player:
        await ctx.send(f"{ctx.author.mention} You haven't joined yet. Run `!join` first!")
        return

    if player["marbles"] > 0:
        await ctx.send(
            f"{ctx.author.mention} You still have **{player['marbles']} marble(s)** — "
            f"`!bonusmarble` is only for players at 0. Get back out there!"
        )
        return

    today = date.today().isoformat()
    if player["last_bonus_marble"] == today:
        await ctx.send(
            f"{ctx.author.mention} You already claimed your bonus marble today. "
            f"Hang tight until midnight for your daily marble! 🕛"
        )
        return

    update_player(uid, {"marbles": 1, "last_bonus_marble": today})
    await ctx.send(
        f"🆘 {ctx.author.mention} has been thrown a lifeline — **+1 bonus marble!** "
        f"Don't waste it. Marbles match??"
    )


# ==============================================================
#  CHALLENGE COMMANDS
# ==============================================================

@bot.command()
async def challenge(ctx, opponent: discord.Member):
    """Challenge another player to a marbles match. Stakes = everyone's full count."""
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
            f"{ctx.author.mention} You have **0 marbles** — use `!bonusmarble` first, "
            f"then come back swinging."
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

    supabase.table("challenges").insert({
        "challenger_id": uid,
        "opponent_id": oid,
        "status": "pending",
        "challenger_stakes": None,
        "opponent_stakes": None,
        "vote_mismatches": 0,
    }).execute()

    await ctx.send(
        f"🔮 {ctx.author.mention} has challenged {opponent.mention} to a **MARBLES MATCH!**\n"
        f"Stakes: **ALL marbles** on the line.\n"
        f"{opponent.mention} — use `!accept` to lock in or `!decline` to back down."
    )


@bot.command()
async def accept(ctx):
    """Accept an incoming challenge."""
    uid = str(ctx.author.id)
    player = get_player(uid)

    if not player:
        await ctx.send(f"{ctx.author.mention} You haven't joined yet. Run `!join` first!")
        return

    ch = get_active_challenge(uid)
    if not ch or ch["opponent_id"] != uid or ch["status"] != "pending":
        await ctx.send(f"{ctx.author.mention} You don't have a pending challenge to accept.")
        return

    challenger = get_player(ch["challenger_id"])

    update_challenge(ch["id"], {
        "status": "active",
        "challenger_stakes": challenger["marbles"],
        "opponent_stakes": player["marbles"],
    })
    update_player(ch["challenger_id"], {"in_match": True})
    update_player(uid, {"in_match": True})

    challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
    await ctx.send(
        f"✅ {ctx.author.mention} accepted the challenge!\n"
        f"**{challenger_user.display_name}** ({challenger['marbles']} 🔮) vs "
        f"**{ctx.author.display_name}** ({player['marbles']} 🔮)\n"
        f"Go play your match, then both report `!winner @player` when done. "
        f"**Marbles match??**"
    )


@bot.command()
async def decline(ctx):
    """Decline an incoming pending challenge."""
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
    """Cancel your pending outgoing challenge before it's accepted."""
    uid = str(ctx.author.id)
    ch = get_active_challenge(uid)

    if not ch or ch["challenger_id"] != uid or ch["status"] != "pending":
        await ctx.send(f"{ctx.author.mention} You don't have a pending challenge to cancel.")
        return

    update_challenge(ch["id"], {"status": "cancelled"})
    opponent_user = await bot.fetch_user(int(ch["opponent_id"]))
    await ctx.send(
        f"🚫 {ctx.author.mention} cancelled their challenge against {opponent_user.mention}."
    )


# ==============================================================
#  WINNER REPORTING
# ==============================================================

@bot.command()
async def winner(ctx, reported_winner: discord.Member):
    """Report the winner of your active match. Both players must agree."""
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

    # Re-fetch to get latest votes from both sides
    res = supabase.table("challenges").select("*").eq("id", ch["id"]).execute()
    ch = res.data[0]

    cv = ch["challenger_vote"]
    ov = ch["opponent_vote"]

    if cv and ov:
        challenger_user = await bot.fetch_user(int(ch["challenger_id"]))
        opponent_user = await bot.fetch_user(int(ch["opponent_id"]))

        if cv == ov:
            # ✅ Agreement — transfer marbles
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
                f"{loser_user.mention} has been cleaned out. "
                f"Use `!bonusmarble` if you're at 0. Marbles match??"
            )
        else:
            # ❌ Mismatch — escalate
            mismatches = ch["vote_mismatches"] + 1
            update_challenge(ch["id"], {
                "vote_mismatches": mismatches,
                "challenger_vote": None,
                "opponent_vote": None,
            })

            if mismatches == 1:
                await ctx.send(
                    f"⚠️ {challenger_user.mention} {opponent_user.mention} — "
                    f"Your votes didn't match! Figure it out and both vote again with `!winner @player`."
                )
            elif mismatches == 2:
                await ctx.send(
                    f"🚨 {challenger_user.mention} {opponent_user.mention} — "
                    f"Votes mismatched again! **Final warning:** if your next votes don't agree, "
                    f"**BOTH players lose ALL their marbles.** Vote carefully. 🔮"
                )
            else:
                # 💀 Third mismatch — nuke both
                update_player(ch["challenger_id"], {"marbles": 0, "in_match": False})
                update_player(ch["opponent_id"], {"marbles": 0, "in_match": False})
                update_challenge(ch["id"], {"status": "complete"})

                await ctx.send(
                    f"💀 {challenger_user.mention} {opponent_user.mention} — "
                    f"Three mismatched votes. You had your chance. "
                    f"**BOTH players have been set to 0 marbles.** "
                    f"Learn to agree next time. 🔮"
                )
    else:
        await ctx.send(
            f"✅ {ctx.author.mention} submitted their vote. "
            f"Waiting on the other player to run `!winner @player`."
        )


# ==============================================================
#  ADMIN COMMANDS  (requires "Marble Admin" Discord role)
# ==============================================================

def is_marble_admin(ctx):
    return discord.utils.get(ctx.author.roles, name="Marble Admin") is not None


@bot.command()
async def give(ctx, member: discord.Member, amount: int):
    """[Marble Admin] Add marbles to a player."""
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
    """[Marble Admin] Remove marbles from a player."""
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
    """[Marble Admin] Set a player's marble count to an exact number."""
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


# ==============================================================
#  RUN
# ==============================================================
bot.run(BOT_TOKEN)
