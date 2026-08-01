"""Outbound-only coverage-criteria agent entrypoint.

The worker deliberately receives no patient or destination data through LiveKit
metadata.  It fetches that context from the local coordinator only after
validating a per-call capability token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import textwrap
from collections.abc import AsyncIterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from livekit import api
from livekit.agents import (
    AMD,
    Agent,
    AgentServer,
    AgentSession,
    FlushSentinel,
    JobContext,
    ModelSettings,
    RunContext,
    TurnHandlingOptions,
    cli,
    function_tool,
    inference,
    llm,
    room_io,
)
from livekit.agents.llm import ToolError
from livekit.plugins import ai_coustics, cartesia, cerebras, deepgram

from call_backend import (
    BackendCallClient,
    BackendCallError,
    BackendConfigurationError,
    CallSummary,
    CriteriaEvidence,
    DispatchMetadata,
    DispatchMetadataError,
    OutboundCallContext,
    criteria_needs_public_fallback,
    parse_dispatch_metadata,
)
from coverage_research import (
    PublicCriteriaResearcher,
    PublicResearchError,
    PublicSource,
)
from outbound_flow import outcome_for_amd_category

load_dotenv(".env.local")

logger = logging.getLogger("coverage-agent")

CEREBRAS_MODEL = "gemma-4-31b"
DEEPGRAM_MODEL = "nova-3"
CARTESIA_MODEL = "sonic-3.5"
CARTESIA_VOICE_ID = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
SIP_PARTICIPANT_PREFIX = "coverage-sip-"
COMPLETION_OUTCOMES = {"completed", "partial", "declined"}


def _failure_log_fields(
    *, call_id: str, stage: str, exc: BaseException
) -> dict[str, str]:
    """Return non-sensitive fields for an unexpected workflow failure."""

    return {
        "call_id": call_id,
        "stage": stage,
        "error_type": type(exc).__name__,
    }


class _AMDTranscriptLogFilter(logging.Filter):
    """Suppress the AMD detector's one transcript-bearing log record.

    The installed LiveKit AMD detector logs its greeting transcript on the
    exact ``livekit.agents`` logger. This filter is attached only while AMD is
    running, so it does not change normal worker logging behavior.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "livekit.agents"
            and (record.msg == "amd prediction" or "transcript" in record.__dict__)
        )


@contextmanager
def _suppress_amd_transcript_logs() -> Iterator[None]:
    """Install the narrow AMD log guard only for an active detection window."""

    amd_logger = logging.getLogger("livekit.agents")
    transcript_filter = _AMDTranscriptLogFilter()
    amd_logger.addFilter(transcript_filter)
    try:
        yield
    finally:
        amd_logger.removeFilter(transcript_filter)


def _amd_category_value(category: Any) -> str:
    return str(getattr(category, "value", category))


def _remove_amd_tag(ctx: JobContext, category: Any) -> None:
    """Remove AMD's raw-greeting tag before the session report is uploaded.

    AMD adds ``lk.amd:*`` metadata containing the greeting transcript. Session
    tags are uploaded only at session end, so removing that tag immediately
    after classification keeps Cloud observability transcript-only. The
    category is sent to the local coordinator as a public call outcome.
    Traces are separately disabled in ``session.start(record=...)``.
    """

    category_value = _amd_category_value(category)
    ctx.tagger.remove(f"lk.amd:{category_value}")


def _remove_amd_transcript_tags(ctx: JobContext) -> None:
    """Remove any raw AMD tags even if the call errors before result handling."""

    for tag in ctx.tagger.tags:
        if tag.startswith("lk.amd:"):
            ctx.tagger.remove(tag)


def build_coverage_instructions() -> str:
    """Create generic persisted instructions with no call-specific context."""

    return textwrap.dedent(
        """\
        You are Conduit, an AI care-coordination agent. You may speak to a
        payer coverage contact or infusion-center scheduling staff. Private
        call material for each turn identifies the recipient type; use it only
        to conduct this one care-access conversation.

        This is a professional voice call. Speak plainly, briefly, and one
        question at a time. You are not a clinician and must not provide medical
        advice or make guarantees of coverage.

        AMD has already classified the answer before you can speak. Your first
        audible response must disclose that you are an AI assistant, identify
        the appropriate coverage or infusion-center scheduling purpose, and ask
        whether this is the right department. Do not mention this project or a
        hackathon.

        For an infusion-center recipient, act as a care-sourcing coordinator:
        ask for new-patient scheduling or intake, confirm the center is the
        right place to discuss treatment access, and collect only the general
        information needed for staff follow-up. Do not share any patient detail
        until the recipient confirms they are the right person or department.
        Do not disclose a date of birth, insurance identifier, financial detail,
        or full clinical notes over the phone.

        This implementation has no live calendar, center-capacity, document
        delivery, appointment-booking, or CRM tools. Do not invent patient
        availability, an approved authorization, a referral, a document, a
        confirmation number, a distance, an appointment, or any fact absent
        from the private material. Do not claim to send records or to confirm a
        booking. If a center needs information outside the supplied material,
        state that a staff member will follow up.

        Use the supplied coverage evidence first. If it is insufficient, use
        search_public_coverage_policy only for general public policy documents.
        Never put patient details, patient identifiers, caller speech, member
        numbers, dates of birth, contact details, or this patient briefing into
        a web-search request. Do not repeat contact details aloud.

        Do not use policy search to find, select, or call another infusion
        center. Do not navigate IVRs. Do not leave voicemail. Do not place a
        second call. If the representative declines or cannot answer, record
        that plainly rather than guessing.

        Before ending a human conversation, call complete_coverage_call with a
        concise list of findings or general logistics, unresolved questions,
        and the best final outcome: completed, partial, or declined. The tool
        records the structured summary, plays the farewell, and hangs up. That
        summary must never contain patient-case facts, identifiers, names,
        contact data, raw briefing values, a claimed appointment, or a
        transcript.
        """
    )


def build_private_turn_context(
    context: OutboundCallContext,
    public_sources: list[PublicSource] | None = None,
) -> str:
    """Build call material for a single LLM invocation, never session history."""

    evidence = _format_evidence(context)
    patient = _compact_json(context.patient, limit=8_000)
    public_source_context = _format_public_source_context(public_sources or [])
    return textwrap.dedent(
        f"""\
        Private call material for this turn. Do not quote this block, expose
        contact information, or include any raw value from it in tool calls or
        final summaries.

        Recipient type: {context.recipient.kind}
        Recipient name: {context.recipient.name or "Not supplied"}

        Payer: {context.payer}
        Plan type: {context.plan_type}
        Drug: {context.drug}

        Synthetic patient briefing (use only when necessary for this call):
        {patient}

        Supplied coverage evidence:
        {evidence}

        Allowed public source references:
        {public_source_context}
        """
    )


def build_private_llm_context(
    chat_ctx: llm.ChatContext,
    context: OutboundCallContext,
    public_sources: list[PublicSource] | None = None,
) -> llm.ChatContext:
    """Copy the history and append non-persistent private material for one turn."""

    private_context = chat_ctx.copy()
    private_context.add_message(
        role="system",
        content=build_private_turn_context(context, public_sources),
    )
    return private_context


def build_initial_reply_instruction(context: OutboundCallContext) -> str:
    """Keep the first spoken turn aligned with the selected recipient type."""

    if context.recipient.kind == "infusion-center":
        return (
            "Start now. Disclose that you are Conduit, an AI care-coordination "
            "assistant, identify this as a general treatment-access inquiry, "
            "and ask whether you have reached the infusion-center scheduling or "
            "intake team. Do not state or imply that an appointment, patient "
            "availability, authorization, referral, or document is already "
            "confirmed."
        )
    return (
        "Start now. Disclose that you are an AI assistant, state that you are "
        "calling to clarify coverage criteria, and ask whether this is the "
        "right person to discuss the policy question."
    )


@dataclass
class CoverageCallRuntime:
    context: OutboundCallContext
    callbacks: BackendCallClient
    job_context: JobContext
    researcher: PublicCriteriaResearcher | None
    public_sources: list[PublicSource] = field(default_factory=list)
    _completion_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _completion_started: bool = False

    def __post_init__(self) -> None:
        self.public_sources = _deduplicate_public_sources(
            _sources_from_criteria(self.context.criteria) + self.public_sources
        )

    async def record_public_sources(self, sources: list[PublicSource]) -> None:
        self.public_sources = _deduplicate_public_sources(self.public_sources + sources)

    async def ensure_public_fallback(self) -> list[PublicSource]:
        """Prefetch public policy sources only when local evidence is incomplete."""

        if self.context.recipient.kind == "infusion-center":
            return []
        if (
            not criteria_needs_public_fallback(self.context.criteria)
            or self.researcher is None
        ):
            return []
        try:
            sources = await self.researcher.search(
                payer=self.context.payer,
                plan_type=self.context.plan_type,
                drug=self.context.drug,
                topic="coverage criteria",
            )
        except PublicResearchError:
            # Public research is a fallback, never a reason to abandon a call.
            return []
        await self.record_public_sources(sources)
        return sources

    async def complete(
        self,
        tool_context: RunContext,
        *,
        outcome: str,
        criteria_summary: list[str],
        unresolved_questions: list[str],
    ) -> None:
        if outcome not in COMPLETION_OUTCOMES:
            raise ToolError(
                "The final outcome must be completed, partial, or declined."
            )
        if not criteria_summary:
            raise ToolError(
                "Provide at least one concise coverage finding before ending."
            )

        async with self._completion_lock:
            if self._completion_started:
                return
            self._completion_started = True

        summary = CallSummary(
            criteria_summary=_bounded_strings(criteria_summary),
            unresolved_questions=_bounded_strings(unresolved_questions),
            public_sources=[
                source.to_callback_payload() for source in self.public_sources
            ],
        )
        try:
            await self.callbacks.report_event(
                self.context.call_id,
                status="summarizing",
                summary=summary,
            )
        except BackendCallError as exc:
            self._completion_started = False
            raise ToolError(
                "I could not save the call result. Please continue the call."
            ) from exc

        # The backend retains this structured summary while the call remains
        # nonterminal. Do not release its one-call gate until the final speech
        # has played and the SIP room has been deleted.
        tool_context.disallow_interruptions()
        try:
            await tool_context.wait_for_playout()
            farewell = tool_context.session.say(
                "Thank you for your time. Goodbye.", allow_interruptions=False
            )
            await farewell.wait_for_playout()
            await _delete_outbound_room(self.job_context)
        except Exception:
            # Keep the backend nonterminal if the SIP room cannot be confirmed
            # closed. The five-minute backend watchdog remains the safe cleanup
            # path rather than admitting a potentially concurrent call.
            logger.warning(
                "unable to close completed outbound room",
                extra={"call_id": self.context.call_id},
            )
            return

        try:
            await self.callbacks.report_event(
                self.context.call_id,
                status="completed",
                outcome=outcome,
            )
        except BackendCallError:
            # The room is already closed; withholding a terminal callback is
            # safer than releasing the one-call gate before cleanup succeeds.
            logger.warning(
                "unable to report completed outbound call",
                extra={"call_id": self.context.call_id},
            )


class CoverageAgent(Agent):
    """The conversational portion starts only after AMD has completed."""

    def __init__(self, runtime: CoverageCallRuntime) -> None:
        self._runtime = runtime
        # Agent instructions become an AgentConfigUpdate in session history, so
        # they must remain generic. Private materials are injected per LLM call.
        super().__init__(instructions=build_coverage_instructions())

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str | FlushSentinel]:
        private_chat_ctx = build_private_llm_context(
            chat_ctx,
            self._runtime.context,
            self._runtime.public_sources,
        )
        # Passing a copied context to the default node makes the private system
        # message visible to this LLM call only. It is never added to self.chat_ctx
        # or session.history, so transcript-only observability cannot upload it.
        async for chunk in Agent.default.llm_node(
            self, private_chat_ctx, tools, model_settings
        ):
            yield chunk

    @function_tool()
    async def search_public_coverage_policy(
        self,
        context: RunContext,
        topic: str,
    ) -> dict[str, Any]:
        """Find up to three public payer/CMS policy sources.

        Use only when supplied evidence cannot answer a general coverage-policy
        question. Never include any patient, member, contact, or caller details
        in the topic; the tool deliberately ignores topic text in its query.

        Args:
            topic: A generic policy topic such as "medical policy" or "prior
                authorization requirements". Patient-specific wording is not
                allowed.
        """

        del context
        if self._runtime.researcher is None:
            raise ToolError("Public policy search is not configured for this call.")
        try:
            sources = await self._runtime.researcher.search(
                payer=self._runtime.context.payer,
                plan_type=self._runtime.context.plan_type,
                drug=self._runtime.context.drug,
                topic=topic,
            )
        except PublicResearchError as exc:
            raise ToolError(
                "Public policy search is unavailable. Use the supplied evidence."
            ) from exc

        await self._runtime.record_public_sources(sources)
        return {
            "sources": [
                {"label": source.label, "url": source.url, "excerpt": source.excerpt}
                for source in sources
            ]
        }

    @function_tool()
    async def complete_coverage_call(
        self,
        context: RunContext,
        outcome: str,
        criteria_summary: list[str],
        unresolved_questions: list[str],
    ) -> None:
        """Record a final human-call result, play a brief goodbye, and hang up.

        Call this only after the coverage discussion is complete. It must never
        be used for voicemail, IVR, or an unavailable mailbox because the
        outbound lifecycle handles those outcomes without speaking.

        Args:
            outcome: One of completed, partial, or declined.
            criteria_summary: Concise findings from supplied evidence, the
                representative, or allowed public policy sources. Include policy
                facts only—never patient-case facts, identifiers, names,
                contact details, or raw briefing values.
            unresolved_questions: Policy follow-up items only; never include
                patient-case facts, identifiers, names, contact details, or raw
                briefing values. Use an empty list when none remain.
        """

        await self._runtime.complete(
            context,
            outcome=outcome.lower().strip(),
            criteria_summary=criteria_summary,
            unresolved_questions=unresolved_questions,
        )


def create_voice_pipeline() -> tuple[AgentSession, Any, Any]:
    """Build the required direct-provider voice stack exactly once per call."""

    llm = cerebras.LLM(model=CEREBRAS_MODEL)
    stt = deepgram.STT(model=DEEPGRAM_MODEL, language="en-US")
    tts = cartesia.TTS(model=CARTESIA_MODEL, voice=CARTESIA_VOICE_ID)
    session = AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        turn_handling=TurnHandlingOptions(turn_detection=inference.TurnDetector()),
        preemptive_generation=True,
    )
    return session, llm, stt


async def _connect_and_start_session(
    *,
    ctx: JobContext,
    session: AgentSession,
    agent: Agent,
    room_options: room_io.RoomOptions,
) -> None:
    """Join the job room before RoomIO initializes its audio participants."""

    await ctx.connect()
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_options,
        # LiveKit Cloud observability keeps the transcript only. No audio,
        # traces, or runtime logs are uploaded for this session.
        record={"audio": False, "transcript": True, "traces": False, "logs": False},
    )


server = AgentServer()


@server.rtc_session(agent_name="voice")
async def my_agent(ctx: JobContext) -> None:
    """Fetch call context, dial through a stored trunk, then run AMD first."""

    metadata = _read_dispatch_metadata(ctx)
    if metadata is None:
        await _delete_room_quietly(ctx)
        return

    # Logs are keyed only by opaque server-generated call IDs. Do not add room,
    # destination, patient, search-query, or raw context values here.
    ctx.log_context_fields = {"call_id": metadata.call_id}
    try:
        callbacks = BackendCallClient(
            base_url=os.getenv("CALL_BACKEND_URL", "http://127.0.0.1:8000"),
            token=metadata.token,
        )
    except BackendConfigurationError:
        logger.warning(
            "invalid backend callback configuration",
            extra={"call_id": metadata.call_id},
        )
        await _delete_room_quietly(ctx, metadata.call_id)
        return
    try:
        call_context = await callbacks.fetch_context(metadata.call_id)
    except BackendCallError:
        logger.warning("call context unavailable", extra={"call_id": metadata.call_id})
        await _delete_room_then_report_terminal(
            ctx,
            callbacks,
            metadata.call_id,
            status="failed",
            outcome="context-error",
        )
        return

    trunk_id = os.getenv("SIP_OUTBOUND_TRUNK_ID")
    if not trunk_id:
        logger.warning(
            "outbound trunk is not configured", extra={"call_id": metadata.call_id}
        )
        await _delete_room_then_report_terminal(
            ctx,
            callbacks,
            metadata.call_id,
            status="failed",
            outcome="agent-error",
        )
        return

    tavily_key = os.getenv("TAVILY_API_KEY")
    runtime = CoverageCallRuntime(
        context=call_context,
        callbacks=callbacks,
        job_context=ctx,
        researcher=PublicCriteriaResearcher(api_key=tavily_key) if tavily_key else None,
    )
    participant_identity = f"{SIP_PARTICIPANT_PREFIX}{metadata.call_id}"

    stage = "create-voice-pipeline"
    try:
        session, llm, stt = create_voice_pipeline()
        agent = CoverageAgent(runtime)
        stage = "connect-and-start-agent-session"
        await _connect_and_start_session(
            ctx=ctx,
            session=session,
            agent=agent,
            room_options=room_io.RoomOptions(
                participant_identity=participant_identity,
                delete_room_on_close=True,
                audio_input=room_io.AudioInputOptions(
                    noise_cancellation=ai_coustics.audio_enhancement(
                        model=ai_coustics.EnhancerModel.QUAIL_VF_S
                    ),
                ),
            ),
        )
        stage = "report-dialing"
        await callbacks.report_event(metadata.call_id, status="dialing")

        # AMD must begin before the participant is created. It pauses agent
        # speech, so CoverageAgent cannot speak over a greeting or voicemail.
        stage = "run-amd-and-dial"
        with _suppress_amd_transcript_logs():
            async with AMD(
                session,
                llm=llm,
                stt=stt,
                participant_identity=participant_identity,
                ivr_detection=False,
                wait_until_finished=True,
                suppress_compatibility_warning=False,
            ) as detector:
                try:
                    await ctx.api.sip.create_sip_participant(
                        api.CreateSIPParticipantRequest(
                            sip_trunk_id=trunk_id,
                            sip_call_to=call_context.to_phone_number,
                            room_name=ctx.room.name,
                            participant_identity=participant_identity,
                            participant_name="Coverage criteria assistant",
                            wait_until_answered=True,
                        )
                    )
                    await ctx.wait_for_participant(identity=participant_identity)
                    await callbacks.report_event(metadata.call_id, status="screening")
                    result = await detector.execute()
                    _remove_amd_tag(ctx, result.category)
                finally:
                    _remove_amd_transcript_tags(ctx)

        terminal_outcome = outcome_for_amd_category(
            _amd_category_value(result.category)
        )
        if terminal_outcome is not None:
            logger.info("terminal AMD outcome", extra={"call_id": metadata.call_id})
            await _delete_room_then_report_terminal(
                ctx,
                callbacks,
                metadata.call_id,
                status="completed",
                outcome=terminal_outcome,
            )
            return

        # Human and uncertain classifications proceed. This is the first place
        # the agent is allowed to generate speech.
        stage = "activate-conversation"
        await callbacks.report_event(metadata.call_id, status="active")
        await runtime.ensure_public_fallback()
        session.generate_reply(
            instructions=build_initial_reply_instruction(call_context),
        )
    except api.SipCallError as exc:
        # SIP diagnostics can contain provider details. Convert them to a safe
        # categorical outcome before the backend receives anything.
        logger.warning("outbound SIP call failed", extra={"call_id": metadata.call_id})
        await _delete_room_then_report_terminal(
            ctx,
            callbacks,
            metadata.call_id,
            status="failed",
            outcome=outcome_for_sip_call_error(exc),
        )
    except api.ServerError as exc:
        # LiveKit uses deadline_exceeded when wait_until_answered reaches its
        # answer window without a SIP status response. It carries no provider
        # details into the public status.
        logger.warning(
            "outbound SIP request failed", extra={"call_id": metadata.call_id}
        )
        await _delete_room_then_report_terminal(
            ctx,
            callbacks,
            metadata.call_id,
            status="failed",
            outcome=outcome_for_sip_server_error(exc),
        )
    except Exception as exc:
        # Provider exceptions can include a SIP destination. Keep worker logs
        # keyed only by opaque server-generated fields.
        logger.warning(
            "outbound call failed",
            extra=_failure_log_fields(
                call_id=metadata.call_id,
                stage=stage,
                exc=exc,
            ),
        )
        await _delete_room_then_report_terminal(
            ctx,
            callbacks,
            metadata.call_id,
            status="failed",
            outcome=outcome_for_unexpected_workflow_error(exc),
        )


def _read_dispatch_metadata(ctx: JobContext) -> DispatchMetadata | None:
    try:
        return parse_dispatch_metadata(ctx.job.metadata)
    except DispatchMetadataError:
        # Metadata is treated as untrusted input. We cannot safely make a
        # capability-protected callback when its capability is malformed.
        return None


def outcome_for_sip_call_error(error: api.SipCallError) -> str:
    """Map a LiveKit SIP response to the public outcome vocabulary only."""

    if error.sip_status_code == 408:
        return "no-answer"
    if error.sip_status_code in {480, 486}:
        return "unavailable"
    return "carrier-error"


def outcome_for_sip_server_error(error: api.ServerError) -> str:
    """Map a non-SIP LiveKit dial failure to a public terminal outcome."""

    if error.code == api.ServerErrorCode.DEADLINE_EXCEEDED:
        return "no-answer"
    return "carrier-error"


def outcome_for_unexpected_workflow_error(_error: BaseException) -> str:
    """Keep model and agent failures distinct from explicit SIP failures."""

    return "agent-error"


async def _delete_room_then_report_terminal(
    ctx: JobContext,
    callbacks: BackendCallClient,
    call_id: str,
    *,
    status: str,
    outcome: str,
) -> bool:
    """Release the backend gate only after the call's room has been closed."""

    try:
        await _delete_outbound_room(ctx)
    except Exception:
        # Preserve the active backend record if room deletion cannot be
        # confirmed. The coordinator's timeout remains the safe fail-closed
        # cleanup route and prevents a second concurrent SIP leg.
        logger.warning("unable to delete outbound room", extra={"call_id": call_id})
        return False

    try:
        await callbacks.report_event(call_id, status=status, outcome=outcome)
    except BackendCallError:
        # The room is closed, but a failed callback must not be followed by a
        # synthetic terminal status that could race a live leg elsewhere.
        logger.warning(
            "unable to report terminal call status", extra={"call_id": call_id}
        )
        return False
    return True


async def _delete_outbound_room(ctx: JobContext) -> None:
    """Delete through Room Service and surface failures before terminal status.

    ``JobContext.delete_room`` intentionally swallows Room Service failures.
    Terminal callbacks instead need an affirmative deletion result so their
    backend one-call gate cannot open while a SIP participant may still exist.
    A missing room is safe because no active SIP leg remains in it.
    """

    try:
        await ctx.api.room.delete_room(api.DeleteRoomRequest(room=ctx.room.name))
    except api.ServerError as exc:
        if exc.code != api.TwirpErrorCode.NOT_FOUND:
            raise


async def _delete_room_quietly(ctx: JobContext, call_id: str | None = None) -> None:
    try:
        await _delete_outbound_room(ctx)
    except Exception:
        if call_id is not None:
            logger.warning("unable to delete outbound room", extra={"call_id": call_id})


def _format_evidence(context: OutboundCallContext) -> str:
    if not context.criteria:
        return "No local payer criteria were available. Use public policy search only if needed."
    rendered: list[str] = []
    for item in context.criteria[:20]:
        source = item.source_label or item.source_url or "local payer criteria"
        rendered.append(f"- {item.text} (Source: {source})")
    return "\n".join(rendered)


def _compact_json(value: Any, *, limit: int) -> str:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    return rendered[:limit]


def _bounded_strings(values: list[str], *, max_items: int = 20) -> list[str]:
    return [value.strip()[:1_000] for value in values if value.strip()][:max_items]


def _sources_from_criteria(
    criteria: tuple[CriteriaEvidence, ...],
) -> list[PublicSource]:
    sources: list[PublicSource] = []
    for evidence in criteria:
        source_url = evidence.source_url
        if not source_url or not _is_http_source_url(source_url):
            continue
        sources.append(
            PublicSource(
                label=evidence.source_label or source_url,
                url=source_url,
                excerpt="",
            )
        )
    return sources


def _deduplicate_public_sources(sources: list[PublicSource]) -> list[PublicSource]:
    deduplicated: list[PublicSource] = []
    seen_urls: set[str] = set()
    for source in sources:
        if source.url in seen_urls:
            continue
        seen_urls.add(source.url)
        deduplicated.append(source)
    return deduplicated


def _format_public_source_context(sources: list[PublicSource]) -> str:
    if not sources:
        return "No public source references are available yet."
    rendered: list[str] = []
    for source in sources[:6]:
        line = f"- {source.label}: {source.url}"
        if excerpt := source.excerpt.strip():
            line += f"\n  Public excerpt: {excerpt[:1_200]}"
        rendered.append(line)
    return "\n".join(rendered)


def _is_http_source_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname is not None


if __name__ == "__main__":
    cli.run_app(server)
