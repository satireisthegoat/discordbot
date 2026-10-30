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

# ---------- raid forwarding ----------
RAID_ROLE_ID = 1545038660850688080
RAID_LEADER_ROLE_ID = 1545040101929656413
RAIDS_CHANNEL_ID = 1545037570600079390
CHAT_CHANNEL_ID = 1534598572362961120
RL_CHAT_CHANNEL_ID = 1545040735420555355
UNL_WORD = re.compile(r"(?<!\w)unl(?!\w)", re.IGNORECASE)  # "unl" as its own word, any capitalization

# ---------- Q list ----------
Q_CHANNEL_ID = 1560393413566930974
QCHECK_ROLE_ID = 1545418339696771152
QLIST_ALLOWED_ROLE_IDS = {
    1545205609148850176,
    1544841710968897617,
    1545048463169818825,
    1544841601849884672,
    1560987977918709840,
    1544841503950766137,
    1545040101929656413,
}
QLIST_HISTORY_LIMIT = 5000  # how many recent messages to scan while looking for the ping
Q_NUMBER = re.compile(r"[0-9]+")  # whole digits only (no spaces, signs, decimals, commas)

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


# ---------- raid forwarding ----------
def mentions_raid_role(message: discord.Message) -> bool:
    """True if the message pings the @raid role."""
    if f"<@&{RAID_ROLE_ID}>" in message.content:
        return True
    return any(role.id == RAID_ROLE_ID for role in message.role_mentions)


async def get_channel_by_id(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return channel


async def forward_message(message: discord.Message, channel_id: int) -> None:
    """Forward the message to a channel. Falls back to a plain copy if forwarding fails."""
    channel = await get_channel_by_id(channel_id)
    if channel is None:
        return

    try:
        await message.forward(channel)  # native Discord forward (needs discord.py 2.5+)
        return
    except (AttributeError, discord.HTTPException):
        pass

    # Fallback: post a copy with the sender and a link to the original
    text = (
        f"**{discord.utils.escape_markdown(message.author.display_name)}** "
        f"in {message.channel.mention}:\n{message.content}\n{message.jump_url}"
    )
    try:
        await channel.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


async def handle_raid_call(message: discord.Message) -> None:
    """When @raid and "unl" are in the same message in the raids channel, forward it
    to the chat and RL chat channels, then ping the raid leader role in RL chat."""
    if message.guild is None or message.channel.id != RAIDS_CHANNEL_ID:
        return
    if bot.user is not None and message.author.id == bot.user.id:
        return  # never react to the bot's own messages
    if not mentions_raid_role(message):
        return
    if not UNL_WORD.search(message.content):
        return

    await forward_message(message, CHAT_CHANNEL_ID)
    await forward_message(message, RL_CHAT_CHANNEL_ID)

    # Ping the raid leader role in the RL chat channel (just the ping, no text)
    rl_channel = await get_channel_by_id(RL_CHAT_CHANNEL_ID)
    if rl_channel is not None:
        try:
            await rl_channel.send(
                f"<@&{RAID_LEADER_ROLE_ID}>",
                allowed_mentions=discord.AllowedMentions(
                    roles=[discord.Object(id=RAID_LEADER_ROLE_ID)]
                ),
            )
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

    # Forward raid "unl" calls
    await handle_raid_call(message)

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


# ---------- automod: word management helpers ----------
def parse_words(raw: str) -> list[str]:
    """Split on commas or new lines, lowercase, trim, and remove duplicates."""
    seen = set()
    words = []
    for part in re.split(r"[,\n]", raw):
        word = part.strip().lower()
        if word and word not in seen:
            seen.add(word)
            words.append(word)
    return words


def add_words_to_blacklist(guild_id: int, raw: str) -> tuple[list[str], list[str]]:
    cfg = guild_cfg(guild_id)
    existing = set(cfg["blacklist"])
    added, skipped = [], []
    for word in parse_words(raw):
        if word in existing:
            skipped.append(word)
        else:
            cfg["blacklist"].append(word)
            added.append(word)
    if added:
        save_config(config)
    return added, skipped


def remove_words_from_blacklist(guild_id: int, raw: str) -> tuple[list[str], list[str]]:
    cfg = guild_cfg(guild_id)
    removed, missing = [], []
    for word in parse_words(raw):
        if word in cfg["blacklist"]:
            cfg["blacklist"].remove(word)
            removed.append(word)
        else:
            missing.append(word)
    if removed:
        save_config(config)
    return removed, missing


def fmt(words: list[str]) -> str:
    return ", ".join(f"`{w}`" for w in words)


def add_result_text(added: list[str], skipped: list[str]) -> str:
    if not added and not skipped:
        return "❌ I didn't find any words in that."
    lines = []
    if added:
        lines.append(f"✅ Added ({len(added)}): {fmt(added)}")
    if skipped:
        lines.append(f"Already on the list: {fmt(skipped)}")
    return "\n".join(lines)[:2000]


def remove_result_text(removed: list[str], missing: list[str]) -> str:
    if not removed and not missing:
        return "❌ I didn't find any words in that."
    lines = []
    if removed:
        lines.append(f"✅ Removed ({len(removed)}): {fmt(removed)}")
    if missing:
        lines.append(f"Not on the list: {fmt(missing)}")
    return "\n".join(lines)[:2000]


def list_text(guild_id: int) -> str:
    cfg = guild_cfg(guild_id)
    if not cfg["blacklist"]:
        return "The blacklist is empty."
    words = fmt(sorted(cfg["blacklist"]))
    return f"**Blacklisted words ({len(cfg['blacklist'])}):**\n{words}"[:2000]


# ---------- automod: panel (buttons + pop-up forms) ----------
class AddWordsModal(discord.ui.Modal, title="Add blacklisted words"):
    words = discord.ui.TextInput(
        label="Words (commas or new lines)",
        style=discord.TextStyle.paragraph,
        placeholder="word one, word two, a phrase",
        required=True,
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        added, skipped = add_words_to_blacklist(interaction.guild_id, self.words.value)
        await interaction.response.send_message(
            add_result_text(added, skipped), ephemeral=True
        )


class RemoveWordsModal(discord.ui.Modal, title="Remove blacklisted words"):
    words = discord.ui.TextInput(
        label="Words (commas or new lines)",
        style=discord.TextStyle.paragraph,
        placeholder="word one, word two",
        required=True,
        max_length=2000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        removed, missing = remove_words_from_blacklist(interaction.guild_id, self.words.value)
        await interaction.response.send_message(
            remove_result_text(removed, missing), ephemeral=True
        )


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.manage_guild:
            return True
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Add words", style=discord.ButtonStyle.green, emoji="➕")
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddWordsModal())

    @discord.ui.button(label="Remove words", style=discord.ButtonStyle.red, emoji="➖")
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RemoveWordsModal())

    @discord.ui.button(label="View list", style=discord.ButtonStyle.secondary, emoji="📋")
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(list_text(interaction.guild_id), ephemeral=True)


# ---------- slash commands: automod ----------
automod_group = app_commands.Group(
    name="automod",
    description="Manage the blacklisted words",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@automod_group.command(name="panel", description="Open the blacklist control panel")
async def panel(interaction: discord.Interaction):
    count = len(guild_cfg(interaction.guild_id)["blacklist"])
    await interaction.response.send_message(
        f"**Automod panel**\n{count} word(s) currently blacklisted. "
        "Use the buttons below to add or remove words.",
        view=PanelView(),
        ephemeral=True,
    )


@automod_group.command(name="add_word", description="Add one or more words (separate with commas)")
@app_commands.describe(words="Words or phrases, separated by commas")
async def add_word(interaction: discord.Interaction, words: str):
    added, skipped = add_words_to_blacklist(interaction.guild_id, words)
    await interaction.response.send_message(add_result_text(added, skipped), ephemeral=True)


@automod_group.command(name="remove_word", description="Remove one or more words (separate with commas)")
@app_commands.describe(words="Words or phrases, separated by commas")
async def remove_word(interaction: discord.Interaction, words: str):
    removed, missing = remove_words_from_blacklist(interaction.guild_id, words)
    await interaction.response.send_message(remove_result_text(removed, missing), ephemeral=True)


@automod_group.command(name="list_words", description="Show all blacklisted words")
async def list_words(interaction: discord.Interaction):
    await interaction.response.send_message(list_text(interaction.guild_id), ephemeral=True)


bot.tree.add_command(automod_group)


# ---------- slash command: Q list ----------
def is_qcheck_ping(message: discord.Message) -> bool:
    """True if the message pings the @QCHECK role."""
    if f"<@&{QCHECK_ROLE_ID}>" in message.content:
        return True
    return any(role.id == QCHECK_ROLE_ID for role in message.role_mentions)


def short_name(member: discord.abc.User) -> str:
    """Server display name, keeping only the part before the "|".
    "Walk | bunnislayer0" -> "Walk". If there's no "|", the whole name is used."""
    full = member.display_name
    left = full.split("|", 1)[0].strip()
    return left or full.strip() or full


@bot.tree.command(
    name="q-list",
    description="Post the sorted list of numbers sent after the latest @QCHECK ping",
)
@app_commands.guild_only()
async def q_list(interaction: discord.Interaction):
    # Only the allowed roles can use this
    user_role_ids = {r.id for r in getattr(interaction.user, "roles", [])}
    if not user_role_ids & QLIST_ALLOWED_ROLE_IDS:
        await interaction.response.send_message(
            "You don't have permission to use this command.", ephemeral=True
        )
        return

    await interaction.response.defer()  # reading history can take a few seconds

    channel = bot.get_channel(Q_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(Q_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.followup.send("❌ I can't find the Q channel.", ephemeral=True)
            return

    # Walk backwards (newest first) until we reach the most recent @QCHECK ping.
    # The first number we see from each person is therefore their latest one.
    entries: dict[int, tuple[int, object, discord.abc.User]] = {}
    ping_found = False

    try:
        async for msg in channel.history(limit=QLIST_HISTORY_LIMIT):
            if is_qcheck_ping(msg):
                ping_found = True
                break

            if msg.author.bot:
                continue
            if msg.type not in (discord.MessageType.default, discord.MessageType.reply):
                continue

            content = msg.content.strip()
            if not Q_NUMBER.fullmatch(content):
                continue  # "8 people", "a1", "-5", "3.5", "1,000" etc. are ignored

            number = int(content)
            if number < 1:
                continue  # 0 isn't allowed

            if msg.author.id not in entries:
                entries[msg.author.id] = (number, msg.created_at, msg.author)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I don't have permission to read that channel's history.", ephemeral=True
        )
        return

    if not ping_found:
        await interaction.followup.send(
            f"❌ I couldn't find a <@&{QCHECK_ROLE_ID}> ping in the last "
            f"{QLIST_HISTORY_LIMIT} messages.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # Messages read from history usually give us a plain User (their global display
    # name), not the server Member (their server nickname). So look up each person as a
    # server member to get their server display name, e.g. "Aurora | bienpatyteves".
    named: list[tuple[int, object, str]] = []
    for author_id, (number, created, author) in entries.items():
        member = interaction.guild.get_member(author_id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(author_id)
            except discord.HTTPException:
                member = author  # left the server or lookup failed: use whatever name we have
        named.append((number, created, discord.utils.escape_markdown(short_name(member))))

    # Smallest to biggest; if two people sent the same number, whoever sent it first goes first
    ordered = sorted(named, key=lambda e: (e[0], e[1]))

    if not ordered:
        await interaction.followup.send("**Q LIST**\nNo numbers yet.")
        return

    lines = [f"{number} - {name}" for number, _created, name in ordered]

    # Split into several messages if the list is too long for one
    chunks = []
    current = "**Q LIST**"
    for line in lines:
        if len(current) + 1 + len(line) > 1900:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line
    chunks.append(current)

    for chunk in chunks:
        await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none())


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable first.")
    bot.run(TOKEN)
