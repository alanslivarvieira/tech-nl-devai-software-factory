"""Runtime configuration (environment variables; thresholds optionally from a YAML file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from factory_core.models import FactoryConfig


@dataclass
class Settings:
    workspaces: Path = field(default_factory=lambda: Path(os.environ.get("FACTORY_WORKSPACES", "workspaces")).resolve())
    database_url: str | None = field(default_factory=lambda: os.environ.get("DATABASE_URL"))
    fake_agent: str | None = field(default_factory=lambda: os.environ.get("FACTORY_FAKE_AGENT"))
    sandbox: str = field(default_factory=lambda: os.environ.get("FACTORY_SANDBOX", "local"))
    sandbox_image: str = field(
        default_factory=lambda: os.environ.get("FACTORY_SANDBOX_IMAGE", "factory-sandbox:latest")
    )
    github_token: str | None = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN"))
    github_api: str = field(default_factory=lambda: os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    notify_url: str | None = field(default_factory=lambda: os.environ.get("FACTORY_NOTIFY_URL"))
    factory: FactoryConfig = field(default_factory=lambda: load_factory_config(os.environ.get("FACTORY_CONFIG")))


def load_factory_config(path: str | None) -> FactoryConfig:
    if path and Path(path).exists():
        return FactoryConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    return FactoryConfig()
