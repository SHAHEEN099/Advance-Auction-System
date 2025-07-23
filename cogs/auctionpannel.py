import discord
import json
import os
import requests
import logging
import asyncio
from typing import Optional
from discord.ext import commands, tasks
from discord import app_commands
from rapidfuzz import process, fuzz

# --------------------------------------------------------------------
# Logger Setup
# --------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------
# JSON Storage for Guild, Ticket & Panel Template Data
# --------------------------------------------------------------------
CONFIG_FILE = "guild_configs.json"
TICKETS_FILE = "auction_tickets.json"
ITEMS_FILE = "items.json"  # Cache for the API data
AUC_PANEL_TEMPLATE_FILE = "auc_panel_template.json"

def load_json(filename, default):
    """Load data from a JSON file or return `default` if not found/corrupt."""
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {filename}: {e}")
    return default

def save_json(filename, data):
    """Save `data` to a JSON file with indentation."""
    try:
        with open(filename, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving {filename}: {e}")

# Load existing data
guild_configs = load_json(CONFIG_FILE, {})      # { str(guild_id): {...} }
auction_tickets = load_json(TICKETS_FILE, {})     # { str(channel_id): {...} }
cached_items = load_json(ITEMS_FILE, [])          # The cached list of items from the API

# Default panel template for the auction ticket panel
DEFAULT_PANEL_TEMPLATE = {
    "title": "Auction Ticket Panel",
    "description": "Click **Create Auction Ticket** below to open a private channel for your auction.",
    "footer": "Auction Ticket Panel",
    "colour": "blue",
    "thumbnail": "",
    "button_text": "Create Auction Ticket",
    "button_colour": "success"
}

def load_auc_panel_template():
    return load_json(AUC_PANEL_TEMPLATE_FILE, DEFAULT_PANEL_TEMPLATE.copy())

def save_auc_panel_template(template: dict):
    save_json(AUC_PANEL_TEMPLATE_FILE, template)

def save_configs():
    save_json(CONFIG_FILE, guild_configs)

def save_tickets():
    save_json(TICKETS_FILE, auction_tickets)

def save_items():
    save_json(ITEMS_FILE, cached_items)

# --------------------------------------------------------------------
# API & Caching Logic
# --------------------------------------------------------------------
def fetch_items_from_api() -> list:
    """
    Synchronously fetch items from https://api.gwapes.com/items.
    Return the list of item dicts on success, else empty list.
    """
    url = "https://api.gwapes.com/items"
    try:
        resp = requests.get(url, timeout=5)
        data = resp.json()
        if data.get("success") and isinstance(data.get("body"), list):
            return data["body"]
    except Exception as e:
        logger.error(f"Error fetching items from API: {e}")
    return []

async def update_items_cache():
    global cached_items
    logger.info("Refreshing items cache from API...")
    new_items = fetch_items_from_api()
    if new_items:
        cached_items = new_items
        save_items()
        logger.info(f"Successfully updated items cache with {len(new_items)} items.")
    else:
        logger.warning("Failed to fetch new items or API returned empty data.")

def get_cached_items() -> list:
    return cached_items

def fuzzy_search_item(query: str, items: list, threshold=65):
    if not items:
        return None
    item_names = [item["name"] for item in items]
    best_match, score, idx = process.extractOne(query, item_names, scorer=fuzz.WRatio)
    if best_match and score >= threshold:
        return items[idx]
    return None

# --------------------------------------------------------------------
# Numeric Parsing: allow "1m", "500k", "1.5m", "1b", etc.
# --------------------------------------------------------------------
def parse_amount(value_str: str) -> int:
    """
    Convert strings like '1m', '500k', '1.5m', '1000000' to an integer.
    - 'm' => million
    - 'k' => thousand
    - 'b' => billion
    - decimals are allowed (1.5m => 1,500,000)
    Raise ValueError if invalid.
    """
    val = value_str.lower().replace(",", "").replace("_", "").strip()
    multiplier = 1

    if val.endswith("m"):
        multiplier = 1_000_000
        val = val[:-1]
    elif val.endswith("k"):
        multiplier = 1_000
        val = val[:-1]
    elif val.endswith("b"):
        multiplier = 1_000_000_000
        val = val[:-1]

    # If there's a decimal, parse float; else parse int
    if "." in val:
        number = float(val)
    else:
        number = int(val)

    return int(number * multiplier)

# --------------------------------------------------------------------
# Asynchronous helper to delete a channel after a delay
# --------------------------------------------------------------------
async def schedule_channel_deletion(channel: discord.TextChannel, delay: int = 3600):
    await asyncio.sleep(delay)
    try:
        await channel.delete(reason="Auto-delete after ticket was closed for 1 hour.")
    except Exception as e:
        logger.error(f"Error auto-deleting channel {channel.id}: {e}")

# --------------------------------------------------------------------
# TICKET CREATION MODAL
# --------------------------------------------------------------------
class AuctionCreateModal(discord.ui.Modal, title="Create Auction"):
    item_name = discord.ui.TextInput(
        label="Item Name",
        placeholder="Enter item name (partial or exact)",
        required=True,
        max_length=100
    )
    quantity = discord.ui.TextInput(
        label="Quantity",
        placeholder="e.g. 10, 10k, 1m, etc.",
        required=True,
        max_length=20
    )
    starting_bid = discord.ui.TextInput(
        label="Starting Bid",
        placeholder="e.g. 500k, 5m, 5000000",
        required=True,
        max_length=20
    )

    def __init__(self, bot: commands.Bot, guild_config: dict):
        super().__init__()
        self.bot = bot
        self.guild_config = guild_config

    async def on_submit(self, interaction: discord.Interaction):
        """
        1) Parse quantity & bid.
        2) Fuzzy-match item name from the cached list.
        3) Check total worth >= 10M, enforce 30% rule.
        4) Create private ticket channel & save ticket data.
        """
        try:
            quantity_val = parse_amount(self.quantity.value.strip())
            starting_bid_val = parse_amount(self.starting_bid.value.strip())
        except ValueError:
            await interaction.response.send_message(
                "Invalid numeric format. Use plain integers or suffixes like 'k', 'm', or 'b'.",
                ephemeral=True
            )
            return

        items = get_cached_items()
        matched_item = fuzzy_search_item(self.item_name.value.strip(), items, threshold=65)
        if not matched_item:
            await interaction.response.send_message(
                f"**{self.item_name.value}** not found in the cached item list. Try a more precise name.",
                ephemeral=True
            )
            return

        value_each = matched_item.get("value", 0)
        if value_each <= 0:
            await interaction.response.send_message(
                f"**{matched_item['name']}** has invalid 'value'. Cannot proceed.",
                ephemeral=True
            )
            return

        total_worth = value_each * quantity_val
        if total_worth < 10_000_000:
            await interaction.response.send_message(
                f"Total worth must be **≥ 10,000,000**.\nCurrent total worth: ◊ {total_worth:,}",
                ephemeral=True
            )
            return

        max_allowed = int(total_worth * 0.30)
        if starting_bid_val > max_allowed:
            msg = (
                f"Your starting bid must be **≤ 30%** of total worth.\n\n"
                f"**Total Worth**: ◊ {total_worth:,}\n"
                f"**Max Allowed (30%)**: ◊ {max_allowed:,}\n"
                f"**Your Bid**: ◊ {starting_bid_val:,}\n\n"
                f"Please adjust your bid."
            )
            await interaction.response.send_message(msg, ephemeral=True)
            return

        guild_id_str = str(interaction.guild_id)
        config = self.guild_config
        current_ticket_num = config.get("ticket_counter", 0) + 1
        config["ticket_counter"] = current_ticket_num
        guild_configs[guild_id_str] = config
        save_configs()

        guild = interaction.guild
        staff_role_ids = config.get("staff_roles", [])
        category_id = config.get("category_id")
        channel_name = f"auction-{interaction.user.name.lower().replace(' ', '-')}-{interaction.user.discriminator}"

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                use_application_commands=True
            )
        }
        for r_id in staff_role_ids:
            role = guild.get_role(r_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    use_application_commands=True
                )

        category = guild.get_channel(category_id) if category_id else None
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason="Creating an auction ticket"
        )

        # --------------------------------------------------------------------
        # ADD the "attachment" key to the ticket data
        # --------------------------------------------------------------------
        auction_tickets[str(ticket_channel.id)] = {
            "ticket_id": current_ticket_num,
            "guild_id": guild_id_str,
            "user_id": interaction.user.id,
            "item_name": matched_item["name"],
            "attachment": matched_item.get("attachment", None),
            "value": value_each,
            "quantity": quantity_val,
            "starting_bid": starting_bid_val
        }
        save_tickets()

        embed = discord.Embed(
            title="Auction Ticket Opened",
            description=f"Please donate **{quantity_val}x {matched_item['name']}** as required.",
            color=discord.Color.green()
        )
        embed.add_field(name="Item", value=f"{quantity_val}x {matched_item['name']}", inline=False)
        embed.add_field(name="Starting Bid", value=f"◊ {starting_bid_val:,}", inline=True)
        embed.add_field(name="Total Worth", value=f"◊ {total_worth:,}", inline=True)
        embed.add_field(name="Value Each", value=f"◊ {value_each:,}", inline=True)
        embed.set_footer(text=f"Ticket ID #{current_ticket_num} | Opened by {interaction.user}")

        if matched_item.get("attachment"):
            embed.set_thumbnail(url=matched_item["attachment"])

        view = AuctionTicketView(interaction.user, self.bot)

        # Send the embed message
        await ticket_channel.send(
            content=f"{interaction.user.mention}, welcome to your auction ticket!",
            embed=embed,
            view=view
        )

        # --------------------------------------------------------------------
        # Send the 3 separate lines in a new message (below the embed)
        # --------------------------------------------------------------------
        line1 = "**Please donate your auction items using Dank Memer**"
        line2 = f"Example: `/serverevents donate quantity:{quantity_val} item:{matched_item['name']}`"
        line3 = "Once your donation is complete, it will be recorded for auction processing."
        await ticket_channel.send(f"{line1}\n{line2}\n{line3}")
        # --------------------------------------------------------------------

        await interaction.response.send_message(
            f"Auction ticket created! Check {ticket_channel.mention}.",
            ephemeral=True
        )

# --------------------------------------------------------------------
# TICKET PANEL VIEW with Customization Support
# --------------------------------------------------------------------
class TicketPanelView(discord.ui.View):
    def __init__(self, bot: commands.Bot, guild_config: dict, template: dict):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_config = guild_config
        self.template = template

        # Create a custom button using template values
        button_label = template.get("button_text", "Create Auction Ticket")
        button_colour = template.get("button_colour", "success")
        button_style = self.map_button_colour(button_colour)
        button = discord.ui.Button(label=button_label, style=button_style, custom_id="ticket_panel_create")
        button.callback = self.create_auction_button
        self.add_item(button)

    def map_button_colour(self, colour_str: str):
        mapping = {
            "primary": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger
        }
        return mapping.get(colour_str.lower(), discord.ButtonStyle.success)

    async def create_auction_button(self, interaction: discord.Interaction):
        modal = AuctionCreateModal(bot=self.bot, guild_config=self.guild_config)
        await interaction.response.send_modal(modal)

# --------------------------------------------------------------------
# TICKET VIEW
# --------------------------------------------------------------------
class AuctionTicketView(discord.ui.View):
    def __init__(self, ticket_owner: discord.User, bot: commands.Bot):
        super().__init__(timeout=None)
        self.ticket_owner = ticket_owner
        self.bot = bot

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="auction_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = auction_tickets.get(str(interaction.channel.id))
        if not ticket:
            await interaction.response.send_message(
                "This channel is not recognized as an auction ticket.",
                ephemeral=True
            )
            return

        is_owner = (ticket["user_id"] == interaction.user.id)
        is_staff = any(r.permissions.manage_channels for r in interaction.user.roles)
        if not (is_owner or is_staff):
            await interaction.response.send_message(
                "Only the ticket owner or staff can cancel this auction.",
                ephemeral=True
            )
            return

        await interaction.response.send_message("Cancelling and deleting channel...", ephemeral=True)
        auction_tickets.pop(str(interaction.channel.id), None)
        save_tickets()
        await interaction.channel.delete(reason="Auction ticket cancelled")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.primary, custom_id="auction_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = auction_tickets.get(str(interaction.channel.id))
        if not ticket:
            await interaction.response.send_message(
                "This channel is not recognized as an auction ticket.",
                ephemeral=True
            )
            return

        is_owner = (ticket["user_id"] == interaction.user.id)
        is_staff = any(r.permissions.manage_channels for r in interaction.user.roles)
        if not (is_owner or is_staff):
            await interaction.response.send_message(
                "Only the ticket owner or staff can close this auction.",
                ephemeral=True
            )
            return

        channel = interaction.channel
        owner_member = channel.guild.get_member(ticket["user_id"])
        if owner_member:
            try:
                await channel.set_permissions(owner_member, overwrite=discord.PermissionOverwrite(view_channel=False))
            except Exception as e:
                logger.error(f"Error removing owner's perms: {e}")

        new_name = "closed-ticket"
        if owner_member:
            safe_name = owner_member.name.lower().replace(' ', '-')
            new_name = f"closed-{safe_name}-ticket"

        try:
            await channel.edit(name=new_name, reason="Auction ticket closed")
        except Exception as e:
            logger.error(f"Error renaming channel: {e}")

        auction_tickets.pop(str(channel.id), None)
        save_tickets()

        await interaction.response.send_message(
            "Ticket closed. Owner access removed. This channel will auto-delete in 1 hour.",
            ephemeral=True
        )
        self.bot.loop.create_task(schedule_channel_deletion(channel, 3600))

# --------------------------------------------------------------------
# Helper: Map a colour string to a discord.Color
# --------------------------------------------------------------------
def map_embed_color(colour_str: str):
    mapping = {
        "red": discord.Color.red(),
        "blue": discord.Color.blue(),
        "green": discord.Color.green(),
        "black": discord.Color.from_rgb(0, 0, 0),
        "purple": discord.Color.purple(),
        "gold": discord.Color.gold(),
        "orange": discord.Color.orange()
    }
    return mapping.get(colour_str.lower(), discord.Color.blue())

# --------------------------------------------------------------------
# MAIN COG
# --------------------------------------------------------------------
class AuctionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Start background loop to refresh items every 6 hours
        self.refresh_loop.start()

    @tasks.loop(hours=6)
    async def refresh_loop(self):
        await update_items_cache()

    @refresh_loop.before_loop
    async def before_refresh_loop(self):
        await self.bot.wait_until_ready()
        logger.info("Refresh loop is about to start...")

    @app_commands.command(name="setup_ticket_panel", description="Set up the Auction Ticket Panel (Admin only).")
    async def setup_ticket_panel(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only Administrators can run this command.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        config = guild_configs.get(guild_id, {"category_id": None, "staff_roles": [], "ticket_counter": 0})
        # Load the panel template from file
        template = load_auc_panel_template()

        # Create an embed using the customized template
        embed = discord.Embed(
            title=template.get("title", DEFAULT_PANEL_TEMPLATE["title"]),
            description=template.get("description", DEFAULT_PANEL_TEMPLATE["description"]),
            color=map_embed_color(template.get("colour", DEFAULT_PANEL_TEMPLATE["colour"]))
        )
        if template.get("footer"):
            embed.set_footer(text=template["footer"])
        if template.get("thumbnail"):
            embed.set_thumbnail(url=template["thumbnail"])

        view = TicketPanelView(self.bot, config, template)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("Ticket panel created!", ephemeral=True)

    @app_commands.command(name="auction_config", description="Configure auction settings (Admin only).")
    @app_commands.describe(
        category_id="ID of the category channel for auction tickets.",
        staff_role="Staff role that can moderate auctions."
    )
    async def auction_config(
        self,
        interaction: discord.Interaction,
        category_id: Optional[str] = None,
        staff_role: Optional[discord.Role] = None
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only Administrators can run this command.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        config = guild_configs.get(guild_id, {"category_id": None, "staff_roles": [], "ticket_counter": 0})
        messages = []

        if category_id:
            try:
                cat_id_int = int(category_id)
                category_channel = interaction.guild.get_channel(cat_id_int)
                if category_channel and category_channel.type == discord.ChannelType.category:
                    config["category_id"] = cat_id_int
                    messages.append(f"Category set to **{category_channel.name}** (ID: {cat_id_int}).")
                else:
                    messages.append("Invalid category ID provided.")
            except ValueError:
                messages.append("Category ID must be numeric.")

        if staff_role:
            if staff_role.id not in config.get("staff_roles", []):
                config["staff_roles"].append(staff_role.id)
                messages.append(f"Staff role added: **{staff_role.name}** (ID: {staff_role.id}).")
            else:
                messages.append(f"Staff role **{staff_role.name}** is already configured.")

        guild_configs[guild_id] = config
        save_configs()

        if not messages:
            messages.append("No changes. Provide a category_id or staff_role.")
        await interaction.response.send_message("\n".join(messages), ephemeral=True)

    @app_commands.command(name="create_auction", description="Create a new auction ticket (Admin only).")
    async def create_auction(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only Administrators can run this command.", ephemeral=True)
            return

        guild_id = str(interaction.guild_id)
        config = guild_configs.get(guild_id, {"category_id": None, "staff_roles": [], "ticket_counter": 0})
        modal = AuctionCreateModal(bot=self.bot, guild_config=config)
        await interaction.response.send_modal(modal)

    @app_commands.command(name="edit_bid", description="Edit the starting bid of this auction ticket.")
    @app_commands.describe(new_bid="New starting bid (e.g. 500k, 5m, 5000000).")
    async def edit_bid(self, interaction: discord.Interaction, new_bid: str):
        channel_id = str(interaction.channel_id)
        ticket = auction_tickets.get(channel_id)
        if not ticket:
            await interaction.response.send_message("This channel is not recognized as an auction ticket.", ephemeral=True)
            return

        is_owner = (ticket["user_id"] == interaction.user.id)
        is_staff = any(r.permissions.manage_channels for r in interaction.user.roles)
        if not (is_owner or is_staff):
            await interaction.response.send_message("Only the ticket owner or staff can edit the bid.", ephemeral=True)
            return

        try:
            new_bid_val = parse_amount(new_bid.strip())
        except ValueError:
            await interaction.response.send_message(
                "Invalid numeric format for new bid. Use 'k'/'m'/'b' or plain numbers.",
                ephemeral=True
            )
            return

        value_each = ticket["value"]
        quantity = ticket["quantity"]
        total_worth = value_each * quantity
        max_allowed = int(total_worth * 0.30)
        if new_bid_val > max_allowed:
            await interaction.response.send_message(
                f"**{new_bid_val:,}** exceeds the 30% limit (**◊ {max_allowed:,}**).",
                ephemeral=True
            )
            return

        ticket["starting_bid"] = new_bid_val
        save_tickets()
        await interaction.response.send_message(
            f"Starting bid updated to **◊ {new_bid_val:,}**.",
            ephemeral=True
        )

    @app_commands.command(name="close_auction", description="Close this auction ticket.")
    async def close_auction(self, interaction: discord.Interaction):
        channel_id = str(interaction.channel_id)
        ticket = auction_tickets.get(channel_id)
        if not ticket:
            await interaction.response.send_message("This channel is not recognized as an auction ticket.", ephemeral=True)
            return

        is_owner = (ticket["user_id"] == interaction.user.id)
        is_staff = any(r.permissions.manage_channels for r in interaction.user.roles)
        if not (is_owner or is_staff):
            await interaction.response.send_message(
                "Only the ticket owner or staff can close this auction.",
                ephemeral=True
            )
            return

        channel = interaction.channel
        owner_member = channel.guild.get_member(ticket["user_id"])
        if owner_member:
            try:
                await channel.set_permissions(owner_member, overwrite=discord.PermissionOverwrite(view_channel=False))
            except Exception as e:
                logger.error(f"Error removing owner's perms: {e}")

        new_name = "closed-ticket"
        if owner_member:
            safe_name = owner_member.name.lower().replace(' ', '-')
            new_name = f"closed-{safe_name}-ticket"
        try:
            await channel.edit(name=new_name, reason="Auction ticket closed")
        except Exception as e:
            logger.error(f"Error renaming channel: {e}")

        auction_tickets.pop(channel_id, None)
        save_tickets()

        await interaction.response.send_message(
            "Auction ticket closed. Owner access removed. This channel will auto-delete in 1 hour.",
            ephemeral=True
        )
        self.bot.loop.create_task(schedule_channel_deletion(channel, 3600))

    @app_commands.command(name="refresh_cache", description="Manually refresh the items.json cache (Admin only).")
    async def refresh_cache(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only Administrators can run this command.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        await update_items_cache()
        await interaction.followup.send("Items cache has been refreshed from the API.", ephemeral=True)

    # ----------------------------------------------------------------
    # NEW: Customize Auction Panel Command
    # ----------------------------------------------------------------
    @app_commands.command(name="customize_auc_panel", description="Customize the Auction Ticket Panel template (Admin only).")
    async def customize_auc_panel(
        self,
        interaction: discord.Interaction,
        title: Optional[str] = None,
        description: Optional[str] = None,
        footer: Optional[str] = None,
        colour: Optional[str] = None,
        thumbnail: Optional[str] = None,
        button_text: Optional[str] = None,
        button_colour: Optional[str] = None
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Only Administrators can run this command.", ephemeral=True)
            return

        template = load_auc_panel_template()
        if title is not None:
            template["title"] = title
        if description is not None:
            template["description"] = description
        if footer is not None:
            template["footer"] = footer
        if colour is not None:
            template["colour"] = colour
        if thumbnail is not None:
            template["thumbnail"] = thumbnail
        if button_text is not None:
            template["button_text"] = button_text
        if button_colour is not None:
            template["button_colour"] = button_colour

        save_auc_panel_template(template)
        await interaction.response.send_message("Auction panel template updated successfully.", ephemeral=True)

# --------------------------------------------------------------------
# Cog Setup
# --------------------------------------------------------------------
async def setup(bot: commands.Bot):
    """Called by the bot to load this cog."""
    await bot.add_cog(AuctionCog(bot))
    logger.info("AuctionCog with caching and panel customization loaded successfully.")
