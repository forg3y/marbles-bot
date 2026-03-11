
import discord
from discord.ext import commands

BOT_TOKEN = "****"

# Bot setup — the "!" prefix means commands look like  !marbles, !ping, etc.
intents = discord.Intents.default()
intents.message_content = True  # Required to read message content

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Marbles bot is online!")


@bot.command()
async def ping(ctx):
    """Simple health check — replies 'pong!'"""
    await ctx.send("pong! 🏓")


@bot.command()
async def marbles(ctx):
    """Placeholder — just confirms the bot is alive and aware of marbles"""
    await ctx.send(
        f"🔮 **Marbles Match??**\n"
        f"Hey {ctx.author.display_name}! The marbles system is coming soon. "
        f"Get ready to gamble it all."
    )


@bot.command()
async def hello(ctx):
    """Friendly greeting"""
    await ctx.send(f"Hey {ctx.author.mention}! Welcome to the marbles arena 🎮")


# Run the bot
bot.run(BOT_TOKEN)
