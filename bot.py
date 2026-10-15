import json
import os
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.environ.get("DISCORD_TOKEN")
CONFIG_FILE = Path("config.json")

# Digits only. Change this regex if you want to allow decimals/negatives,
# e.g. r"^-?\d+(\.\d+)?$"
NUMBERS_ONLY = re.compile(r"^\d+$")

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
    return config.setdefault(str(guild_id), {"channel": None, "roles": []})


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
    if not cfg or cfg["channel"] != message.channel.id:
        return

    # Whitelisted roles are exempt
    author_role_ids = {r.id for r in getattr(message.author, "roles", [])}
    if author_role_ids & set(cfg["roles"]):
        return

    if NUMBERS_ONLY.fullmatch(message.content.strip()):
        return

    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    await enforce(message)


@bot.event
async def on_message_edit(_before: discord.Message, after: discord.Message):
    # Stops people from posting a number and editing it into text
    await enforce(after)


# ---------- slash commands ----------
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

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(TOKEN)
