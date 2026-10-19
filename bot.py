import json
import os
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.environ.get("DISCORD_TOKEN")
CONFIG_FILE = Path("/data/config.json")

# Digits only. Change this regex if you want to allow decimals/negatives,
# e.g. r"^-?\d+(\.\d+)?$"
NUMBERS_ONLY = re.compile(r"^\d+$")

# ---------- boost reactions ----------
BOOST_CHANNEL_ID = 1544838991973580940
BOOST_EMOJI = discord.PartialEmoji(name="559271milkheart", id=1548041621084442674)
BOOST_TYPES = {
    discord.MessageType.premium_guild_subscription,
    discord.MessageType.premium_guild_tier_1,
    discord.MessageType.premium_guild_tier_2,
    discord.MessageType.premium_guild_tier_3,
}

# ---------- automod ----------
AUTOMOD_REPORT_CHANNEL_ID = 1561861985878937703

intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Developer Portal


# ---------- config storage ----------
def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


config = load_config()


def guild_cfg(guild_id: int) -> dict:
    cfg = config.setdefault(str(guild_id), {})
    cfg.setdefault("channel", None)
    cfg.setdefault("roles", [])
    cfg.setdefault("blacklist", [])
    return cfg


# ---------- bot ----------
class NumbersBot(commands.Bot):
    async def setup_hook(self):
        await self.tree.sync()


bot = NumbersBot(command_prefix="!", intents=intents)


async def enforce(message: discord.Message) -> None:
    """Delete the message if it breaks the numbers-only rule."""
    if message.guild is None or message.author.bot:
        return

    cfg = config.get(str(message.guild.id))
    if not cfg or cfg.get("channel") != message.channel.id:
        return

    # Whitelisted roles are exempt
    author_role_ids = {r.id for r in getattr(message.author, "roles", [])}
    if author_role_ids & set(cfg.get("roles", [])):
        return

    if NUMBERS_ONLY.fullmatch(message.content.strip()):
        return

    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass


def find_blacklisted(content: str, words: list[str]) -> list[str]:
    """Return every blacklisted word/phrase found in the content (whole-word, case-insensitive)."""
    found = []
    for word in words:
        pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
        if re.search(pattern, content, re.IGNORECASE):
            found.append(word)
    return found


async def automod(message: discord.Message) -> bool:
    """Delete messages containing blacklisted words and report them.
    Returns True if the message was caught."""
    if message.guild is None or message.author.bot:
        return False

    cfg = config.get(str(message.guild.id))
    if not cfg or not cfg.get("blacklist"):
        return False

    matched = find_blacklisted(message.content, cfg["blacklist"])
    if not matched:
        return False

    deleted = True
    try:
        await message.delete()
    except discord.NotFound:
        pass  # already deleted
    except discord.Forbidden:
        deleted = False

    await report_violation(message, matched, deleted)
    return True


async def report_violation(message: discord.Message, matched: list[str], deleted: bool) -> None:
    channel = bot.get_channel(AUTOMOD_REPORT_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(AUTOMOD_REPORT_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    content = message.content[:1000].replace("```", "'''")

    embed = discord.Embed(
        title="🚫 Blacklisted word detected",
        color=discord.Color.red(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="User",
        value=f"{message.author.mention} ({message.author} • {message.author.id})",
        inline=False,
    )
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    embed.add_field(
        name="Matched",
        value=", ".join(f"`{w}`" for w in matched),
        inline=True,
    )
    embed.add_field(name="Message", value=f"```{content}```", inline=False)
    if not deleted:
        embed.add_field(
            name="⚠️ Not deleted",
            value="I don't have permission to delete this message.",
            inline=False,
        )

    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    # React to server boost announcements in the boost channel
    if message.channel.id == BOOST_CHANNEL_ID and message.type in BOOST_TYPES:
        try:
            await message.add_reaction(BOOST_EMOJI)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return

    # Automod first; if it caught the message, stop here
    if await automod(message):
        return

    await enforce(message)


@bot.event
async def on_message_edit(_before: discord.Message, after: discord.Message):
    # Stops people from editing a clean message into a rule-breaking one
    if await automod(after):
        return
    await enforce(after)


# ---------- slash commands: numbers-only ----------
numbers = app_commands.Group(
    name="numbersonly",
    description="Configure the numbers-only channel",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@numbers.command(name="set_channel", description="Choose the numbers-only channel")
async def set_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_cfg(interaction.guild_id)["channel"] = channel.id
    save_config(config)
    await interaction.response.send_message(
        f"✅ Only numbers are now allowed in {channel.mention} (except whitelisted roles).",
        ephemeral=True,
    )


@numbers.command(name="add_role", description="Whitelist a role so it can type anything")
async def add_role(interaction: discord.Interaction, role: discord.Role):
    cfg = guild_cfg(interaction.guild_id)
    if role.id not in cfg["roles"]:
        cfg["roles"].append(role.id)
        save_config(config)
    await interaction.response.send_message(
        f"✅ {role.mention} is whitelisted.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@numbers.command(name="remove_role", description="Remove a role from the whitelist")
async def remove_role(interaction: discord.Interaction, role: discord.Role):
    cfg = guild_cfg(interaction.guild_id)
    if role.id in cfg["roles"]:
        cfg["roles"].remove(role.id)
        save_config(config)
    await interaction.response.send_message(
        f"✅ {role.mention} removed from the whitelist.",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@numbers.command(name="disable", description="Turn off the numbers-only rule")
async def disable(interaction: discord.Interaction):
    guild_cfg(interaction.guild_id)["channel"] = None
    save_config(config)
    await interaction.response.send_message("✅ Numbers-only rule disabled.", ephemeral=True)


@numbers.command(name="status", description="Show the current settings")
async def status(interaction: discord.Interaction):
    cfg = guild_cfg(interaction.guild_id)
    channel = f"<#{cfg['channel']}>" if cfg["channel"] else "not set"
    roles = ", ".join(f"<@&{r}>" for r in cfg["roles"]) or "none"
    await interaction.response.send_message(
        f"**Channel:** {channel}\n**Whitelisted roles:** {roles}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


bot.tree.add_command(numbers)


# ---------- slash commands: automod ----------
automod_group = app_commands.Group(
    name="automod",
    description="Manage the blacklisted words",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@automod_group.command(name="add_word", description="Add a word or phrase to the blacklist")
@app_commands.describe(word="The word or phrase to blacklist")
async def add_word(interaction: discord.Interaction, word: str):
    word = word.strip().lower()
    if not word:
        await interaction.response.send_message("❌ Please enter a word.", ephemeral=True)
        return

    cfg = guild_cfg(interaction.guild_id)
    if word in cfg["blacklist"]:
        await interaction.response.send_message(
            f"`{word}` is already blacklisted.", ephemeral=True
        )
        return

    cfg["blacklist"].append(word)
    save_config(config)
    await interaction.response.send_message(f"✅ Blacklisted `{word}`.", ephemeral=True)


@automod_group.command(name="remove_word", description="Remove a word or phrase from the blacklist")
@app_commands.describe(word="The word or phrase to remove")
async def remove_word(interaction: discord.Interaction, word: str):
    word = word.strip().lower()
    cfg = guild_cfg(interaction.guild_id)
    if word not in cfg["blacklist"]:
        await interaction.response.send_message(
            f"`{word}` isn't on the blacklist.", ephemeral=True
        )
        return

    cfg["blacklist"].remove(word)
    save_config(config)
    await interaction.response.send_message(f"✅ Removed `{word}`.", ephemeral=True)


@automod_group.command(name="list_words", description="Show all blacklisted words")
async def list_words(interaction: discord.Interaction):
    cfg = guild_cfg(interaction.guild_id)
    if not cfg["blacklist"]:
        await interaction.response.send_message("The blacklist is empty.", ephemeral=True)
        return

    words = ", ".join(f"`{w}`" for w in sorted(cfg["blacklist"]))
    await interaction.response.send_message(
        f"**Blacklisted words ({len(cfg['blacklist'])}):**\n{words}"[:2000],
        ephemeral=True,
    )


bot.tree.add_command(automod_group)

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(TOKEN)
