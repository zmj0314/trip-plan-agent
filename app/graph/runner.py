"""Session runner: owns persistence, drives the compiled graph.

Separation of concerns (DI-1): nodes only touch ``AgentState``; every business
table write happens here. Checkpoints are a recovery vehicle, not the source of
truth.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command

from app.domain.ids import new_id
from app.domain.models import PendingInterruptKind, Phase
from app.events.types import Event, EventType
from app.graph.deps import GraphDeps
from app.graph.state import AgentState, initial_state
from app.graph.trip_graph import build_trip_graph
from app.llm.streaming import DeltaSink

PHASE_BY_NAME = {p.value: p for p in Phase}


class GraphRunner:
    def __init__(
        self,
        *,
        deps: GraphDeps,
        checkpointer: Any = None,
        repositories: Any = None,
        bus: Any = None,
    ) -> None:
        self.deps = deps
        self.repositories = repositories
        #: Live event fan-out for SSE readers. Optional: a headless run has no
        #: stream to publish to, and streaming must not become required.
        self.bus = bus
        # Nodes reach the store through ``deps``; anything they must not hold
        # (a live database handle) stays out of ``AgentState`` by living here.
        deps.repositories = repositories
        self.graph = build_trip_graph(deps, checkpointer=checkpointer)

    def rebuild(self, *, checkpointer: Any) -> None:
        """Recompile against a different checkpointer (used at startup).

        Kept separate from ``__init__`` because the durable saver has to be
        created inside the async lifespan.
        """

        self.graph = build_trip_graph(self.deps, checkpointer=checkpointer)

    # ---------------------------------------------------------------- helpers
    def _config(self, session_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": session_id}}

    def _phase(self, state: dict[str, Any]) -> Phase:
        return PHASE_BY_NAME.get(str(state.get("phase") or "COLLECT"), Phase.COLLECT)

    def _to_events(
        self,
        raw: list[dict[str, Any]],
        *,
        session_id: str,
        state: dict[str, Any],
    ) -> list[Event]:
        """Turn collected outbox entries into events, preserving order.

        Events are gathered from the per-node stream rather than read off the
        final state: every node replaces ``outbox``, so the final state would
        only carry the last node's events.
        """

        events: list[Event] = []
        phase = self._phase(state)
        for offset, item in enumerate(raw, start=1):
            try:
                evt = EventType(item.get("type"))
            except ValueError:
                continue
            events.append(
                Event(
                    id=offset,
                    session_id=session_id,
                    type=evt,
                    phase=phase,
                    plan_version_id=state.get("current_plan_version_id"),
                    data=dict(item.get("data") or {}),
                )
            )
        return events

    def _publish_delta(self, session_id: str, data: dict[str, Any]) -> None:
        """Push one streamed chunk to live readers.

        Deltas bypass the persisted log on purpose (``REPLAYABLE_EVENT_TYPES``
        excludes them): a reconnect rebuilds the screen from a snapshot, so
        replaying half a sentence would be noise. They are still published to the
        bus so an attached SSE reader sees them as they are produced.
        """

        bus = getattr(self, "bus", None)
        if bus is None:
            return
        try:
            bus.publish(
                session_id,
                [
                    Event(
                        id=0,
                        session_id=session_id,
                        type=EventType.TEXT_DELTA,
                        phase=Phase.COLLECT,
                        data=dict(data),
                    )
                ],
            )
        except Exception:  # pragma: no cover - a slow reader must not break a turn
            pass

    def _persist_events(self, events: list[Event]) -> list[Event]:
        repo = getattr(self.repositories, "events", None)
        if repo is None:
            return events
        stored: list[Event] = []
        for event in events:
            try:
                seq = repo.append(event.session_id, event)
            except Exception:  # pragma: no cover - persistence must not break flow
                stored.append(event)
                continue
            stored.append(event.model_copy(update={"id": seq}) if isinstance(seq, int) else event)
        return stored

    def _sync_phase(self, session_id: str, state: dict[str, Any]) -> None:
        repo = getattr(self.repositories, "sessions", None)
        if repo is None:
            return
        try:
            repo.update_phase(session_id, str(state.get("phase") or "COLLECT"))
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------- api
    async def start(self, session_id: str | None = None, *, user_id: str = "local") -> dict[str, Any]:
        session_id = session_id or new_id("ses")
        repo = getattr(self.repositories, "sessions", None)
        if repo is not None:
            try:
                repo.create(session_id=session_id, user_id=user_id, phase="COLLECT")
            except Exception:  # pragma: no cover
                pass
        await self.graph.aupdate_state(
            self._config(session_id),
            initial_state(session_id=session_id, user_id=user_id),
        )
        return {"session_id": session_id, "phase": "COLLECT", "status": "active"}

    async def send(self, session_id: str, text: str) -> list[Event]:
        return await self._drive(session_id, {"last_user_input": text, "resume_payload": None})

    async def resume(self, session_id: str, payload: dict[str, Any]) -> list[Event]:
        state = dict((await self.graph.aget_state(self._config(session_id))).values)
        expected = state.get("plan_hash")
        incoming = payload.get("plan_hash")
        if incoming is not None and expected is not None and incoming != expected:
            # DE-2: refuse to resume against a superseded plan.
            return [
                Event(
                    id=0,
                    session_id=session_id,
                    type=EventType.ERROR,
                    phase=self._phase(state),
                    data={"code": "PLAN_VERSION_STALE", "recoverable": True},
                )
            ]
        return await self._drive(session_id, None, resume=payload)

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        state = dict((await self.graph.aget_state(self._config(session_id))).values)
        return {
            "session_id": session_id,
            "phase": state.get("phase"),
            "pending_interrupt": await self.pending_interrupt(session_id),
            "current_plan_version_id": state.get("current_plan_version_id"),
            "plan_hash": state.get("plan_hash"),
            "plan_status": state.get("plan_status"),
            "slots": state.get("slots") or {},
            # The gate computes some slots instead of asking (F1: trip length
            # from the two dates); the UI needs them to explain the day count.
            "derived_slots": state.get("derived_slots") or {},
            "missing_required": state.get("missing_required") or [],
            "assumptions": state.get("assumptions") or [],
            "preview_text": state.get("preview_text") or "",
            # Content layer (F1): the day-by-day plan, so the card can render
            # segments and days without recomputing anything.
            "planned_content": state.get("planned_content") or {},
            "trip": state.get("trip"),
            "actions": [
                {
                    "action_id": a["action_id"],
                    "capability_id": a["capability_id"],
                    "status": a["status"],
                    # P4: the key is what makes a retry safe, so the UI can show
                    # that the action is de-duplicated rather than re-run.
                    "idem_key": a.get("idem_key"),
                    # The deliverable itself (an export's content, a deep-link's
                    # URL), otherwise a reconnect loses what the user asked for.
                    "result": a.get("result"),
                }
                for a in (state.get("actions") or [])
            ],
            # The plan's own leg list, so a caller can see which legs became
            # actions and which had nothing to book.
            "planned_legs": state.get("planned_legs") or [],
            "degradation_log": state.get("degradation_log") or [],
            "scope": state.get("scope") or {},
        }

    async def pending_interrupt(self, session_id: str) -> dict[str, Any] | None:
        """Read the pending interrupt from the checkpoint.

        Derived from persisted state rather than cached in memory, so a process
        restart still knows what the session is waiting for (§5.4 Q2-B).
        """

        try:
            snapshot = await self.graph.aget_state(self._config(session_id))
        except Exception:  # pragma: no cover
            return None
        for task in getattr(snapshot, "tasks", ()) or ():
            interrupts = getattr(task, "interrupts", None)
            if interrupts:
                return _interrupt_payload(interrupts)
        return None

    # --------------------------------------------------------------- driving
    async def _drive(
        self,
        session_id: str,
        update: dict[str, Any] | None,
        *,
        resume: dict[str, Any] | None = None,
    ) -> list[Event]:
        config = self._config(session_id)
        if resume is not None:
            graph_input: Any = Command(resume=resume)
        else:
            payload = dict(update or {})
            # A plain message that arrives while a *question* gate is open is an
            # answer to that question, so it must resume the interrupt rather
            # than start a parallel run.
            pending = await self.pending_interrupt(session_id)
            if pending is not None and pending.get("kind") == PendingInterruptKind.QUESTION.value:
                graph_input = Command(resume={"text": payload.get("last_user_input", "")})
            else:
                # Hand the turn's inputs to the graph as *input*, not via
                # ``update_state``: once a thread has run to END, a checkpoint
                # updated with ``update_state`` has no pending tasks and an
                # ``astream(None)`` becomes a silent no-op, so the session
                # would look alive while never running again.
                graph_input = payload

        collected: list[dict[str, Any]] = []
        deltas: list[dict[str, Any]] = []
        interrupt_payload: dict[str, Any] | None = None

        # Streamed text is published live and also collected here, so a caller
        # that only reads the returned list still sees the deltas in order.
        async def _on_delta(data: dict[str, Any]) -> None:
            deltas.append({"type": EventType.TEXT_DELTA.value, "data": dict(data)})
            self._publish_delta(session_id, data)

        sink = DeltaSink(handler=_on_delta)
        self.deps.extras["delta_sink"] = sink
        try:
            async for chunk in self.graph.astream(graph_input, config, stream_mode="updates"):
                if not isinstance(chunk, dict):
                    continue
                if "__interrupt__" in chunk:
                    interrupt_payload = _interrupt_payload(chunk["__interrupt__"])
                    continue
                for _node, node_update in chunk.items():
                    if isinstance(node_update, dict):
                        collected.extend(node_update.get("outbox") or [])
        finally:
            # Never leave a live sink on the shared deps: the next turn (or
            # another session) would publish into this turn's dead handler.
            self.deps.extras.pop("delta_sink", None)

        state = dict((await self.graph.aget_state(config)).values)
        events = self._to_events(
            [*deltas, *collected], session_id=session_id, state=state
        )

        if interrupt_payload is not None:
            events = [
                *events,
                Event(
                    id=len(events) + 1,
                    session_id=session_id,
                    type=EventType.INTERRUPT,
                    phase=self._phase(state),
                    plan_version_id=state.get("current_plan_version_id"),
                    data=interrupt_payload,
                ),
            ]
        self._sync_phase(session_id, state)
        return self._persist_events(events)


def _interrupt_payload(interrupts: Any) -> dict[str, Any]:
    first = interrupts[0] if isinstance(interrupts, (list, tuple)) and interrupts else interrupts
    value = getattr(first, "value", first)
    if isinstance(value, dict):
        return dict(value)
    return {"kind": "unknown", "raw": str(value)}
