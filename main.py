"""Chronicle: a private, persistent Discord roleplay companion."""
import asyncio
import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from nanny.config import Config
from nanny.discord_app import ChronicleCog
from nanny.engine import OpenRouterClient, StoryEngine
from nanny.store import Store


class ChronicleBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.store = Store(config.database, config.encryption_key)
        self.router = OpenRouterClient()
        self.engine = StoryEngine(self.store, self.router)

    async def setup_hook(self):
        await self.add_cog(ChronicleCog(self, self.store, self.engine, self.router))
        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        await self.change_presence(activity=discord.Game(name="your next chapter · /session"))
        logging.getLogger("chronicle").info("Chronicle connected and ready")

    async def close(self):
        try:
            await self.router.close()
        finally:
            self.store.close()
            await super().close()


async def run(config: Config):
    async with ChronicleBot(config) as bot:
        await bot.start(config.token)


def main():
    load_dotenv()
    # Avoid framework exception tracebacks containing interaction payloads/API responses.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for name in ("discord", "aiohttp", "nanny"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        config = Config.from_env(os.environ)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit("Chronicle stopped. Check configuration, Discord access, and database availability.") from None


if __name__ == "__main__":
    main()
