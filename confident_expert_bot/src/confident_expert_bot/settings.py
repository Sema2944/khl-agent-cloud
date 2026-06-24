from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_admin_ids(value: str | None) -> set[int]:
    if not value:
        return set()
    return {int(item.strip()) for item in value.split(",") if item.strip()}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_bot_token: str
    openai_api_key: str
    admin_ids: set[int] = set()
    database_path: Path = Path("./data/bot.db")
    s3_bucket: str
    s3_region: str = "eu-central-1"
    s3_access_key_id: str
    s3_secret_access_key: str
    s3_endpoint_url: str | None = None

    @classmethod
    def parse_admin_ids(cls, value: str | None) -> set[int]:
        return _parse_admin_ids(value)

    def model_post_init(self, __context: object) -> None:
        if isinstance(self.admin_ids, str):
            self.admin_ids = _parse_admin_ids(self.admin_ids)


settings = Settings()
