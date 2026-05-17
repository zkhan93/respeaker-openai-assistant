"""Configuration for alt-alexa-music-mcp.

Layered loader, highest priority last:
  1. Pydantic field defaults
  2. `config/config.toml` (or path from `MUSIC_MCP_CONFIG_FILE`)
  3. `MUSIC_MCP_*` environment variables

Env vars use the `MUSIC_MCP_` prefix with `__` as the nested-key delimiter,
e.g. `MUSIC_MCP_NAVIDROME__BASE_URL=https://music.khancave.in`.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class NavidromeConfig(BaseModel):
    base_url: str = "http://localhost:4533"
    username: str = ""
    password: str = ""
    api_version: str = "1.16.1"


class LibraryConfig(BaseModel):
    path: Path = Path("/library")
    youtube_subdir: str = "youtube"

    @property
    def youtube_path(self) -> Path:
        return self.path / self.youtube_subdir


class PlayerConfig(BaseModel):
    mpv_socket: Path = Path("/run/mpv/mpv.sock")
    default_volume: int = Field(default=80, ge=0, le=100)
    extra_args: list[str] = Field(default_factory=lambda: ["--ao=pulse"])


class SearchConfig(BaseModel):
    fuzzy_threshold: int = Field(default=60, ge=0, le=100)
    candidate_limit: int = Field(default=20, ge=1, le=100)


class YouTubeConfig(BaseModel):
    format: str = "bestaudio/best"
    max_duration_seconds: int = 900
    audio_codec: str = "mp3"
    audio_quality: str = "192"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 7801
    transport: str = "http"


class LoggingConfig(BaseModel):
    level: str = "INFO"


def _resolve_config_path() -> Path:
    explicit = os.environ.get("MUSIC_MCP_CONFIG_FILE")
    if explicit:
        return Path(explicit)
    return Path("config/config.toml")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSIC_MCP_",
        env_nested_delimiter="__",
        extra="ignore",
        toml_file=_resolve_config_path(),
    )

    navidrome: NavidromeConfig = Field(default_factory=NavidromeConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    player: PlayerConfig = Field(default_factory=PlayerConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Highest priority first: env > init > toml > defaults.
        return (
            env_settings,
            init_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def load_settings() -> Settings:
    return Settings()
