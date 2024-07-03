import discord
from discord.ext import commands
from discord.commands import Option
import openai
import json, os
import sqlite3

class Roleplay(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.conn = sqlite3.connect('database.db')
        self.cursor = self.conn.cursor()
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS guilds
                                (id INTEGER PRIMARY KEY, data TEXT)''')
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS roleplay_sessions
                                (user_id INTEGER, guild_id INTEGER, channel_id INTEGER,
                                 scenario TEXT, image_url TEXT, persona_name TEXT)''')

    @commands.slash_command(name="roleplay-start", description="Start a roleplay session")
    async def start_roleplay(self, ctx, scenario: str,
                             persona_name: Option(str, "Name of the persona you want the bot to take on",
                                                  required=True),
                             image_url: Option(str, "URL of the image you want the bot to use as its pfp",
                                               required=False)):

        # Check if the guild exists in the database, if not, create a new entry
        self.cursor.execute("SELECT data FROM guilds WHERE id = ?", (ctx.guild.id,))
        guild_data = self.cursor.fetchone()
        if not guild_data:
            self.cursor.execute("INSERT INTO guilds (id, data) VALUES (?, ?)",
                                (ctx.guild.id, os.getenv('OPENAI_API_KEY')))
            self.conn.commit()

        # Check if the user already has an active roleplay session in the server
        self.cursor.execute("SELECT * FROM roleplay_sessions WHERE user_id = ? AND guild_id = ?",
                            (ctx.author.id, ctx.guild.id))
        existing_session = self.cursor.fetchone()
        if existing_session:
            existing_channel = ctx.guild.get_channel(existing_session[2])
            if existing_channel:
                await ctx.send(
                    "You already have an active roleplay session in this server. Please end your previous session with /stop-roleplay before starting a new one.")
                return
            else:
                self.cursor.execute("DELETE FROM roleplay_sessions WHERE user_id = ? AND guild_id = ?",
                                    (ctx.author.id, ctx.guild.id))
                self.conn.commit()
        # Create or get the "Roleplay" category
        roleplay_category = discord.utils.get(ctx.guild.categories, name="Roleplay")
        if not roleplay_category:
            roleplay_category = await ctx.guild.create_category("Roleplay")

        # Create a new channel with specific permissions
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            ctx.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            ctx.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        roleplay_channel = await ctx.guild.create_text_channel(f"roleplay-{ctx.author.name}", overwrites=overwrites,
                                                               category=roleplay_category, slowmode_delay=60)

        # Add the user's ID and the created channel's ID to the SQLite database
        self.cursor.execute(
            "INSERT INTO roleplay_sessions (user_id, guild_id, channel_id, scenario, image_url, persona_name) VALUES (?, ?, ?, ?, ?, ?)",
            (ctx.author.id, ctx.guild.id, roleplay_channel.id, scenario, image_url if image_url else 'YOUR_IMAGE_URL',
             persona_name if persona_name else self.bot.user.name))
        self.conn.commit()

        # Send a message to the new channel with the initial scenario
        await ctx.respond('Roleplay created! I have pinged you in that channel')
        await roleplay_channel.send(
            f"{ctx.author.mention}, welcome to your roleplay session! Here's the description based on your prompt: {scenario}")

    @commands.slash_command(name="roleplay-stop", description="Stop a roleplay session")
    async def stop_roleplay(self, ctx, export: Option(bool, "Would you like to export the chat history?", required=False, default=False)):
        # Check if the user has an active roleplay session in the server
        self.cursor.execute("SELECT * FROM roleplay_sessions WHERE user_id = ? AND guild_id = ?",
                            (ctx.author.id, ctx.guild.id))
        roleplay_session = self.cursor.fetchone()
        if not roleplay_session:
            await ctx.respond("You don't have an active roleplay session to stop.")
            return

        # Retrieve the roleplay channel
        roleplay_channel = self.bot.get_channel(roleplay_session[2])
        if not roleplay_channel:
            await ctx.respond("The roleplay channel for your session no longer exists.")
            return

        # Export chat history if requested
        if export:
            chat_history = []
            async for message in roleplay_channel.history(limit=200):
                role = "user" if message.author == ctx.author else "assistant"
                chat_history.append({"role": role, "content": message.content})

            # Reverse the chat history to have it in chronological order
            chat_history = chat_history[::-1]

            # Save chat history to a JSON file
            chat_history_json = json.dumps(chat_history, indent=4)
            with open(f"{ctx.author.id}_roleplay_history.json", "w") as file:
                file.write(chat_history_json)

            # Send the JSON file to the user's DM
            try:
                await ctx.author.send("Here is your exported roleplay chat history.", file=discord.File(f"{ctx.author.id}_roleplay_history.json"))
            except discord.Forbidden:
                await ctx.respond("I couldn't send you a DM. Please make sure your DMs are open and try again.")
                return
            finally:
                os.remove(f"{ctx.author.id}_roleplay_history.json")  # Clean up the file after sending

        await ctx.respond('Roleplay deleted!')
        # Delete the roleplay channel
        if roleplay_channel:
            webhooks = await roleplay_channel.webhooks()
            webhook = next((w for w in webhooks if w.user.id == self.bot.user.id), None)
            if webhook:
                await webhook.delete()
            await roleplay_channel.delete()

        # Remove the roleplay session from the database
        self.cursor.execute("DELETE FROM roleplay_sessions WHERE user_id = ? AND guild_id = ?",
                            (ctx.author.id, ctx.guild.id))
        self.conn.commit()

    @commands.slash_command(name="roleplay-edit", description="Edit the AI's most recent reply in a roleplay session")
    async def edit_roleplay_reply(self, ctx, new_reply: str):
        # Check if the user has an active roleplay session in the server
        self.cursor.execute("SELECT * FROM roleplay_sessions WHERE user_id = ? AND guild_id = ?",
                            (ctx.author.id, ctx.guild.id))
        roleplay_session = self.cursor.fetchone()
        if not roleplay_session:
            await ctx.respond("You don't have an active roleplay session to edit.")
            return

        # Get the roleplay channel
        roleplay_channel = self.bot.get_channel(roleplay_session[2])
        if not roleplay_channel:
            await ctx.respond("The roleplay channel for your session no longer exists.", ephemeral=True)
            return

        # Get the last message sent by the bot in the roleplay channel
        async for message in roleplay_channel.history(limit=100):
            if message.author.id == self.bot.user.id:
                bot_message = message
                break
        else:
            await ctx.respond("I couldn't find my last message in this roleplay channel.")
            return

        # Update the bot's last message with the new reply
        await bot_message.edit(content=new_reply)

        # Update the database with the new reply
        self.cursor.execute("UPDATE roleplay_sessions SET last_reply = ? WHERE user_id = ? AND guild_id = ?",
                            (new_reply, ctx.author.id, ctx.guild.id))
        self.conn.commit()

        await ctx.respond("My last reply has been updated.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        # Ignore messages from the bot itself
        if message.author.id == self.bot.user.id:
            return

        if not message.guild:
            return
        # Check if the guild exists in the database
        self.cursor.execute("SELECT data FROM guilds WHERE id = ?", (message.guild.id,))
        guild_data = self.cursor.fetchone()
        if not guild_data:
            return

        # Check if the message is in a roleplay channel
        self.cursor.execute("SELECT * FROM roleplay_sessions WHERE user_id = ? AND channel_id = ?",
                            (message.author.id, message.channel.id))
        roleplay_session = self.cursor.fetchone()
        if roleplay_session:

            # Show that the bot is typing
            await message.channel.trigger_typing()

            # The bot should respond to the user's message
            # Get the previous ten messages in the roleplay channel and format them into a dict "conv"
            conv = []
            async for history_message in message.channel.history(limit=200):
                if history_message.author == message.author:
                    role = "user"
                else:
                    role = "assistant"

                # Check if we should drop the last message in conv
                if len(conv) >= 1 and conv[-1]['role'] == role:
                    conv.pop()

                conv.append({"role": role, "content": history_message.content})

            # Check if the last message in conv is from the user, and if so, remove it
            if conv and conv[-1]['role'] == 'assistant':
                conv.pop()

            # Reverse the order of the conversation
            conv = conv[::-1]

            # create our rp functionality str
            sys_prompt_rp = f"Roleplay the following scenario provided by the user: {roleplay_session[3]}. Engage in the roleplay by responding to their dialogue and actions in character."

            # Add the system prompt to the beginning of the conversation
            system_message = {"role": "system", "content": sys_prompt_rp}
            conv.insert(0, system_message)

            # Query AI model and return
            response = openai.ChatCompletion.create(api_base=os.getenv("OPENAI_API_URL"), api_key=os.getenv('OPENAI_API_KEY'), model=os.getenv('OPENAI_API_MODEL'),
                                                    messages=conv)

            # Create or fetch a webhook to use for sending the message
            webhooks = await message.channel.webhooks()
            webhook = next((w for w in webhooks if w.user.id == self.bot.user.id), None)
            if not webhook:
                webhook = await message.channel.create_webhook(name=roleplay_session[5])

            # Send the message using the webhook
            await webhook.send(content=response['choices'][0]['message']['content'][:1900],
                               avatar_url=roleplay_session[4])
