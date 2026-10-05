"""Compatibility import for integrations; launch the redesigned bot with main.py."""
from nanny.discord_app import ChronicleCog

Roleplay = ChronicleCog
__all__ = ["ChronicleCog", "Roleplay"]
