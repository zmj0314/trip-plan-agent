"""Runtime configuration.

Credentials never enter code, logs or the repository (framework §6.11).
They are read from the environment only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- app ---
    app_env: Literal["dev", "test", "prod"] = "dev"
    data_dir: Path = Path("var")
    access_token: str | None = None  # DL-3: minimal access control

    # --- llm (DL-*) ---
    llm_credential_mode: Literal["server", "request", "both"] = "both"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    # Local inference (DM-3). ``llama-server`` from llama.cpp exposes an
    # OpenAI-compatible surface, so a local model needs no new client -- only a
    # different base_url. Reference: Tracord ships Qwen3.5-0.8B on port 8080.
    llm_provider: Literal["deepseek", "local"] = "deepseek"
    local_llm_base_url: str = "http://127.0.0.1:8080/v1"
    local_llm_model: str = "local-model"
    local_llm_request_timeout_seconds: float = 120.0
    llm_offline: bool = False  # force deterministic stub (tests / no key)

    # --- llm budget (DN-*) ---
    llm_monthly_token_budget: int = 50_000_000
    llm_daily_budget_divisor: int = 20
    llm_session_token_cap: int = 500_000
    llm_warn_ratio: float = 0.80
    llm_degrade_ratio: float = 0.95
    # standard-token equivalent weights (DN-4)
    llm_weight_input: float = 1.0
    llm_weight_output: float = 4.0
    llm_weight_cached_input: float = 0.25

    # --- conversation (DC-2, DE-5) ---
    question_max_rounds: int = 4
    consent_suspend_minutes: int = 30
    checkpoint_retention_days: int = 7
    scope_strike_limit: int = 3

    # --- content layer (F1) ---
    #: Beyond this the forecast window runs out, so a longer request is trimmed
    #: and the trim is disclosed as an assumption.
    max_trip_days: int = 15
    #: How many stops one day may hold. Four is already a full day of sightseeing
    #: once travel time is counted.
    per_day_max_attractions: int = 4
    #: Upper bound on city stays. Past this the parse is a sentence, not a trip.
    max_segments: int = 4

    # --- browser channel (§7) ---
    #: Chrome must be started with ``--remote-debugging-port=9222`` for the
    #: browser channel to attach. Absent, every ``browser.*`` capability reports
    #: UNAVAILABLE and the resolver falls through to its next provider -- which is
    #: the designed behaviour, not a failure.
    browser_cdp_endpoint: str = "http://127.0.0.1:9222"
    #: Optional substring to choose which tab to drive when several are open.
    browser_target_url_contains: str | None = None
    #: How long a session holds the single browser page before another may take it.
    browser_lease_minutes: int = 15

    # --- data sources (all optional; missing => provider unavailable) ---
    amap_key: str | None = None
    variflight_api_key: str | None = None
    meituan_ht_token: str | None = None
    #: Keyless MCP sources (OpenStreetMap, 12306) spawn `npx`, which downloads a
    #: package on first use and can stall a turn for minutes. They are therefore
    #: opt-in; the keyless HTTP sources below cover the default path.
    enable_mcp_sources: bool = True
    #: A cold `npx` cache has to download the package before it can answer, so
    #: MCP sources get their own, much longer connect budget than HTTP ones.
    mcp_connect_timeout_seconds: float = 90.0
    open_meteo_base_url: str = "https://api.open-meteo.com/v1"
    #: Both are free and keyless, which is what lets M1 work before any
    #: credential exists (framework §5.2 "免 key 备").
    open_meteo_geocoding_base_url: str = "https://geocoding-api.open-meteo.com/v1"
    osrm_base_url: str = "https://router.project-osrm.org"
    #: Valhalla is used instead of OSRM for multi-modal work because OSRM's
    #: public demo serves the driving profile for *every* profile in the URL --
    #: walking and cycling silently return driving numbers.
    valhalla_base_url: str = "https://valhalla1.openstreetmap.de"

    # --- channels (DG-*) ---
    channel_connect_timeout_seconds: float = 5.0
    channel_health_interval_seconds: float = 60.0
    channel_retry_max_attempts: int = 2
    cache_coord_precision: int = 4

    # --- quotas ---
    quota_pool_amap_lbs: int = 150_000
    quota_pool_amap_search: int = 5_000
    quota_pool_variflight_points: int = 5_000
    quota_warn_ratio: float = 0.80
    quota_degrade_ratio: float = 0.95

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "travel_agent.sqlite3"

    @property
    def llm_daily_token_budget(self) -> int:
        return max(1, self.llm_monthly_token_budget // self.llm_daily_budget_divisor)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
