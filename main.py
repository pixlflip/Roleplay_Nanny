"""
__main__.py Version 2.0
Author: PixlFlip
Date: March 7, 2024

Major improvements since it's my private project now not public anyone can use.
The focus now is not to handle everything bot related for my discord needs anymore,
but instead to be the best assistant I can make.
"""
import discord
from discord.ext import commands
import roleplay
import json, requests, os
from dotenv import load_dotenv
from datetime import datetime
import sqlite3

# Load environment variables from .env
load_dotenv()

# Initialize Discord Bot and Cogs
bot = commands.Bot(command_prefix=os.getenv("COMMAND_PREFIX"), intents=discord.Intents.all())
bot.add_cog(roleplay.Roleplay(bot))


@bot.event
async def on_ready():
    print('Logged in as: {0.user.name} \nWith ID:{0.user.id}'.format(bot))


@bot.event
async def on_message(message):
    # ignore messages from the bot itself
    if message.author == bot.user:
        return

# the single line that actually runs the program
bot.run(os.getenv("DISCORD_TOKEN"))
