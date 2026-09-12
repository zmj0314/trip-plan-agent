"""Application composition root.

Wiring lives here so every other module can stay free of global state. Missing
optional infrastructure (store, channels) must degrade rather than crash: the
graph is required, persistence is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.capabilities.defaults import build_default_registry
from app.capabilities.registry import CapabilityRegistry
from app.config.settings import Settings, get_settings
from app.graph.deps import GraphDeps
from app.graph.checkpointer import build_checkpointer
from app.graph.runner import GraphRunner
from app.llm.credential import CredentialProvider
from app.llm.factory import build_llm_client
from app.llm.profile import LLMProfile, resolve_profile
from app.llm.telemetry import TELEMETRY

logger = logging.getLogger(__name__)


@dataclass
class Repositories:
    sessions: Any = None
    plans: Any = None
    consents: Any = None
    ledger: Any = None
    events: Any = None
    receipts: Any = None
    audit: Any = None
    llm_usage: Any = None


@dataclass
class AppContext:
    settings: Settings
    registry: CapabilityRegistry
    credentials: CredentialProvider
    runner: GraphRunner
    repositories: Repositories = field(default_factory=Repositories)
    resolver: Any = None
    db: Any = None
    #: The one event bus the app uses. Both the runner (publishing streamed
    #: deltas) and the SSE route (subscribing) must share this instance; two
    #: buses would look like streaming that silently never arrives.
    bus: Any = None

    def build_deps_for_call(self, request_key: str | None = None) -> GraphDeps:
        """Return per-request deps.

        The key is resolved here and handed straight to the client; it is never
        written into graph state (framework §11.3.4).
        """

        client, _key, _scope = build_llm_client(self.settings, self.credentials, request_key=request_key)
        return GraphDeps(
            settings=self.settings,
            registry=self.registry,
            resolver=self.resolver,
            llm=client,
        )

    def apply_request_credentials(
        self,
        request_key: str | None = None,
        *,
        profile: LLMProfile | None = None,
    ) -> str:
        """Point the *live* graph at the right LLM client for this request.

        The compiled graph captures the ``GraphDeps`` object at build time, so
        replacing ``runner.deps`` would have no effect on the nodes. The switch
        therefore mutates that same object in place.

        v1 is single-user (§1.4); a multi-user deployment must carry this in a
        ``ContextVar`` instead, because concurrent requests with different keys
        would otherwise race.

        Returns the active client name for observability. Never returns key
        material.
        """

        client, _key, scope = build_llm_client(
            self.settings, self.credentials, request_key=request_key, profile=profile
        )
        self.runner.deps.llm = client
        # healthz reads credentials.resolve(None), which cannot see a per-request
        # key; record what this request actually used so the panel stops lying.
        # `scope` is None exactly when the offline stub was selected.
        if profile is not None and profile.is_local:
            TELEMETRY.note_key_source("local")
        else:
            TELEMETRY.note_key_source(scope.value if scope is not None else "none")
        return active_llm_name(self.runner.deps)

    def profile_from_headers(
        self,
        *,
        provider: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> LLMProfile | None:
        """Build a profile from request headers; ``None`` means "use defaults"."""

        if not any([provider, base_url, model]):
            return None
        return resolve_profile(
            self.settings, provider=provider, base_url=base_url, model=model, api_key=api_key
        )

    def key_source(self, request_key: str | None = None) -> str:
        key, scope = self.credentials.resolve(request_key)
        if not key or scope is None:
            return TELEMETRY.snapshot().get("last_key_source") or "none"
        return scope.value


def active_llm_name(deps: GraphDeps) -> str:
    return client_name(deps.llm)


def client_name(client: object | None) -> str:
    if client is None:
        return "none"
    return str(getattr(client, "model", type(client).__name__))


def build_context(settings: Settings | None = None) -> AppContext:
    settings = settings or get_settings()
    settings.ensure_dirs()

    registry = build_default_registry()
    repositories = Repositories()
    db = None
    resolver = None

    # --- store (optional) --------------------------------------------------
    try:
        from app.store.db import Database
        from app.store.repositories import build_repositories

        db = Database(settings.db_path)
        db.migrate()
        repositories = build_repositories(db)
    except Exception as exc:  # pragma: no cover - store may not exist yet
        logger.warning("store unavailable, running without persistence: %s", type(exc).__name__)

    # --- channels (optional) ----------------------------------------------
    try:
        from app.channels.bootstrap import build_resolver

        resolver = build_resolver(settings, registry, db=db)
    except Exception as exc:  # pragma: no cover - channels may not exist yet
        logger.warning("channels unavailable, capabilities will degrade: %s", type(exc).__name__)

    credentials = CredentialProvider(settings)
    client, _key, _scope = build_llm_client(settings, credentials)
    deps = GraphDeps(settings=settings, registry=registry, resolver=resolver, llm=client)
    checkpointer = build_checkpointer(settings.data_dir / "checkpoints.sqlite3")
    # One bus for the app: the runner publishes streamed deltas into it and the
    # SSE endpoint subscribes to it, so both must be looking at the same object.
    from app.events.bus import EventBus

    bus = EventBus()
    runner = GraphRunner(
        deps=deps, checkpointer=checkpointer, repositories=repositories, bus=bus
    )

    return AppContext(
        settings=settings,
        registry=registry,
        credentials=credentials,
        runner=runner,
        repositories=repositories,
        resolver=resolver,
        db=db,
        bus=bus,
    )
