"""All settings come from environment variables or a .env file next to the project."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), extra="ignore")

    # storage: the database sits in the data folder unless DATABASE_URL says otherwise (in the cloud, the folder is the mounted disk)
    database_url: str = ""
    data_dir: Path = ROOT / "data"
    # nightly database backups: data/backups/ always, plus this folder when set (cloud-synced folder, external drive)
    backup_dir: Optional[Path] = None
    backup_keep: int = 7
    # in the cloud: a private GitHub repository whose releases hold the nightly backups, and a token that can write to it
    backup_github_repo: Optional[str] = None
    github_token: Optional[str] = None
    # the address is reachable from the internet (set automatically on Railway, Render, Fly): an access code is required
    public: bool = False

    # SEC: required. "Your Name your@email.com" per SEC fair-access policy.
    sec_user_agent: str = ""
    sec_rps: float = 8.0  # SEC ceiling is 10 requests/second

    # Claude
    anthropic_api_key: str = ""
    ai_model_light: str = "claude-haiku-5-5"
    ai_model_card: str = "claude-sonnet-5-5"
    ai_model_review: str = "claude-opus-5-5"
    ai_deep_scope: str = "focus"  # "focus" = holdings + watchlist, "all" = every company
    ai_use_batch: bool = True
    ai_monthly_budget_usd: float = 30.0

    # prices
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_feed: str = "delayed_sip"  # iex | delayed_sip | sip

    # earnings calendar, surprises, news
    finnhub_api_key: str = ""

    # optional: packaged transcripts
    fmp_api_key: str = ""

    # IBKR
    ibkr_flex_token: str = ""
    ibkr_flex_query_id: str = ""
    ibkr_live: bool = False
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 4001
    ibkr_client_id: int = 17
    ibkr_max_lines: int = 90

    # alerts
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    quiet_start: str = "23:30"  # Israel time
    quiet_end: str = "07:00"
    brief_time: str = "08:00"
    local_tz: str = "Asia/Jerusalem"

    # server
    host: str = "127.0.0.1"
    port: int = 8000
    access_token: str = ""  # set when the server is reachable from other machines

    # trading defaults (used by the trade calculator)
    portfolio_size_usd: float = 100_000
    risk_per_trade_pct: float = 1.0

    @model_validator(mode="after")
    def _db_in_data_dir(self) -> "Settings":
        if not self.database_url:
            self.database_url = f"sqlite:///{self.data_dir / 'metropolin.db'}"
        return self

    @property
    def sec_ready(self) -> bool:
        return "@" in self.sec_user_agent and " " in self.sec_user_agent.strip()

    @property
    def alpaca_ready(self) -> bool:
        return bool(self.alpaca_key_id and self.alpaca_secret_key)

    @property
    def flex_ready(self) -> bool:
        return bool(self.ibkr_flex_token and self.ibkr_flex_query_id)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
