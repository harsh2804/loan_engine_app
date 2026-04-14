"""
config package
───────────────
Re-exports Settings and the cached getter.
"""
from config.settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]