"""ClipForge - VOD-to-shorts pipeline for TheEnchantingChicken.

Transcribe a Twitch VOD, detect the most clippable moments locally (with an
optional Claude re-rank), then render vertical 9:16 clips with a facecam-top /
gameplay-bottom layout, animated word-by-word captions, and a persistent
Twitch follow watermark.

See README.md for usage. All tunable numbers live in pipeline/config.py.
"""

__version__ = "1.0.0"
