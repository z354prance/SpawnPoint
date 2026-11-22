"""Hostnames. Routing itself happens in proxy.py (built into SpawnPoint)."""
from . import config


def hostname(sid: str, override: str | None = None) -> str:
    return (override or f"{sid}.{config.BASE_DOMAIN}").lower()
