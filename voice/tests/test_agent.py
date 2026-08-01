import json
import logging
from typing import ClassVar

import httpx
import pytest

from call_backend import (
    BackendCallClient,
    BackendConfigurationError,
    CallSummary,
    CriteriaEvidence,
    DispatchMetadataError,
    OutboundCallContext,
    criteria_needs_public_fallback,
    parse_dispatch_metadata,
)
from coverage_research import (
    PublicCriteriaResearcher,
    PublicResearchError,
    PublicSource,
    build_public_criteria_query,
    official_domains_for,
)
from outbound_flow import outcome_for_amd_category


def test_dispatch_metadata_allows_only_call_capability() -> None:
    metadata = parse_dispatch_metadata(
        json.dumps({"call_id": "call-123", "token": "capability-token"})
    )

    assert metadata.call_id == "call-123"
    assert metadata.token == "capability-token"

    with pytest.raises(DispatchMetadataError):
        parse_dispatch_metadata(
            json.dumps(
                {
                    "call_id": "call-123",
                    "token": "capability-token",
                    "to_phone_number": "+12125550123",
                }
            )
        )

    with pytest.raises(DispatchMetadataError):
        parse_dispatch_metadata(
            json.dumps({"call_id": "call-123", "context_token": "legacy-token"})
        )


def test_failure_log_fields_exclude_exception_message() -> None:
    from agent import _failure_log_fields

    fields = _failure_log_fields(
        call_id="call-123",
        stage="start-agent-session",
        exc=RuntimeError("provider response contained sensitive details"),
    )

    assert fields == {
        "call_id": "call-123",
        "stage": "start-agent-session",
        "error_type": "RuntimeError",
    }
    assert "sensitive" not in repr(fields)


def test_unexpected_workflow_failure_is_not_reported_as_a_carrier_error() -> None:
    from agent import outcome_for_unexpected_workflow_error

    assert (
        outcome_for_unexpected_workflow_error(
            RuntimeError("provider details must not reach public call status")
        )
        == "agent-error"
    )


@pytest.mark.asyncio
async def test_room_connection_precedes_agent_session_start() -> None:
    from agent import _connect_and_start_session

    timeline: list[object] = []

    class FakeContext:
        room = object()

        async def connect(self) -> None:
            timeline.append("connected")

    class FakeSession:
        async def start(self, **kwargs: object) -> None:
            timeline.append(("started", kwargs))

    await _connect_and_start_session(
        ctx=FakeContext(),  # type: ignore[arg-type]
        session=FakeSession(),  # type: ignore[arg-type]
        agent=object(),  # type: ignore[arg-type]
        room_options="room-options",  # type: ignore[arg-type]
    )

    assert timeline[0] == "connected"
    start_event = timeline[1]
    assert isinstance(start_event, tuple)
    event_name, start_kwargs = start_event
    assert event_name == "started"
    assert isinstance(start_kwargs, dict)
    assert start_kwargs["room_options"] == "room-options"
    assert start_kwargs["record"] == {
        "audio": False,
        "transcript": True,
        "traces": False,
        "logs": False,
    }


def test_context_retains_only_allowed_patient_sections() -> None:
    context = OutboundCallContext.from_payload(
        {
            "call_id": "call-123",
            "to_phone_number": "+12125550123",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "pembrolizumab",
            "patient": {
                "quickview_data": {"name": "Synthetic Patient"},
                "banner_data": {"member_status": "active"},
                "contact_data": {
                    "phone": "+12125550123",
                    "email": "patient@example.test",
                },
            },
            "criteria": [
                {
                    "text": "Prior authorization is required.",
                    "source_label": "Aetna policy",
                    "source_url": "https://www.aetna.com/policy",
                    "effective_date": "2026-01-01",
                }
            ],
        }
    )

    assert context.patient == {
        "quickview_data": {"name": "Synthetic Patient"},
        "banner_data": {"member_status": "active"},
    }
    assert "contact_data" not in repr(context)
    assert context.criteria[0].source_url == "https://www.aetna.com/policy"


def test_criteria_fallback_is_required_for_empty_or_untraceable_evidence() -> None:
    complete = CriteriaEvidence(
        text="Prior authorization is required.",
        source_url="https://www.aetna.com/policy",
    )
    incomplete = CriteriaEvidence(
        text="Prior authorization is required.",
        source_label="Aetna policy",
    )

    assert criteria_needs_public_fallback(()) is True
    assert criteria_needs_public_fallback((incomplete,)) is True
    assert criteria_needs_public_fallback((complete,)) is False


@pytest.mark.asyncio
async def test_runtime_seeds_backend_sources_and_prefetches_only_missing_criteria() -> (
    None
):
    from agent import CoverageCallRuntime

    class FakeResearcher:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def search(self, **kwargs: str) -> list[PublicSource]:
            self.calls.append(kwargs)
            return [
                PublicSource(
                    label="Aetna public policy",
                    url="https://www.aetna.com/public-policy",
                    excerpt="Public coverage criteria.",
                )
            ]

    complete_context = OutboundCallContext.from_payload(
        {
            "call_id": "call-123",
            "to_phone_number": "+12125550123",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "pembrolizumab",
            "patient": {},
            "criteria": [
                {
                    "text": "Prior authorization is required.",
                    "source_label": "Aetna policy",
                    "source_url": "https://www.aetna.com/policy",
                }
            ],
        }
    )
    complete_researcher = FakeResearcher()
    complete_runtime = CoverageCallRuntime(
        context=complete_context,
        callbacks=object(),  # type: ignore[arg-type]
        job_context=object(),  # type: ignore[arg-type]
        researcher=complete_researcher,  # type: ignore[arg-type]
    )

    await complete_runtime.ensure_public_fallback()

    assert [source.url for source in complete_runtime.public_sources] == [
        "https://www.aetna.com/policy"
    ]
    assert complete_researcher.calls == []

    incomplete_context = OutboundCallContext.from_payload(
        {
            "call_id": "call-456",
            "to_phone_number": "+12125550123",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "pembrolizumab",
            "patient": {"quickview_data": {"name": "Synthetic Patient"}},
            "criteria": [],
        }
    )
    incomplete_researcher = FakeResearcher()
    incomplete_runtime = CoverageCallRuntime(
        context=incomplete_context,
        callbacks=object(),  # type: ignore[arg-type]
        job_context=object(),  # type: ignore[arg-type]
        researcher=incomplete_researcher,  # type: ignore[arg-type]
    )

    await incomplete_runtime.ensure_public_fallback()

    assert incomplete_researcher.calls == [
        {
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "pembrolizumab",
            "topic": "coverage criteria",
        }
    ]
    assert [source.url for source in incomplete_runtime.public_sources] == [
        "https://www.aetna.com/public-policy"
    ]
    from agent import build_private_turn_context

    assert "Public coverage criteria." in build_private_turn_context(
        incomplete_context,
        incomplete_runtime.public_sources,
    )


@pytest.mark.asyncio
async def test_completion_keeps_the_call_nonterminal_until_the_room_is_deleted() -> (
    None
):
    from agent import CoverageCallRuntime

    timeline: list[object] = []

    class FakeCallbacks:
        async def report_event(self, call_id: str, **kwargs: object) -> None:
            timeline.append(("callback", call_id, kwargs))

    class FakeSpeech:
        async def wait_for_playout(self) -> None:
            timeline.append("farewell-playout")

    class FakeSession:
        def say(self, text: str, **kwargs: object) -> FakeSpeech:
            timeline.append(("say", text, kwargs))
            return FakeSpeech()

    class FakeToolContext:
        session = FakeSession()

        def disallow_interruptions(self) -> None:
            timeline.append("interruptions-disabled")

        async def wait_for_playout(self) -> None:
            timeline.append("prior-playout")

    class FakeRoom:
        name = "call-room"

    class FakeRoomService:
        async def delete_room(self, request: object) -> None:
            timeline.append(("room-deleted", request.room))  # type: ignore[attr-defined]

    class FakeApi:
        room = FakeRoomService()

    class FakeJobContext:
        room = FakeRoom()
        api = FakeApi()

    context = OutboundCallContext.from_payload(
        {
            "call_id": "call-123",
            "to_phone_number": "+12125550123",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "pembrolizumab",
            "patient": {},
            "criteria": [
                {
                    "text": "Prior authorization is required.",
                    "source_label": "Aetna policy",
                    "source_url": "https://www.aetna.com/policy",
                }
            ],
        }
    )
    runtime = CoverageCallRuntime(
        context=context,
        callbacks=FakeCallbacks(),  # type: ignore[arg-type]
        job_context=FakeJobContext(),  # type: ignore[arg-type]
        researcher=None,
    )

    await runtime.complete(
        FakeToolContext(),  # type: ignore[arg-type]
        outcome="completed",
        criteria_summary=["Prior authorization is required."],
        unresolved_questions=[],
    )

    first_callback = timeline[0]
    final_callback = timeline[-1]
    assert first_callback == (
        "callback",
        "call-123",
        {
            "status": "summarizing",
            "summary": CallSummary(
                criteria_summary=["Prior authorization is required."],
                unresolved_questions=[],
                public_sources=[
                    {"label": "Aetna policy", "url": "https://www.aetna.com/policy"}
                ],
            ),
        },
    )
    assert timeline.index(("room-deleted", "call-room")) < timeline.index(
        final_callback
    )
    assert final_callback == (
        "callback",
        "call-123",
        {"status": "completed", "outcome": "completed"},
    )


@pytest.mark.asyncio
async def test_infusion_center_completion_tool_uses_neutral_intake_fields() -> None:
    """Facility calls get a matching tool while retaining the callback schema."""

    from livekit.agents.llm import ToolError

    from agent import CoverageAgent

    context = OutboundCallContext.from_payload(
        {
            "call_id": "call-facility-123",
            "to_phone_number": "+12125550123",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "pembrolizumab",
            "recipient": {
                "kind": "infusion-center",
                "name": "Chelsea Infusion Center",
            },
            "patient": {},
            "criteria": [],
        }
    )

    class FakeRuntime:
        def __init__(self) -> None:
            self.context = context
            self.calls: list[dict[str, object]] = []

        async def complete(self, tool_context: object, **kwargs: object) -> None:
            self.calls.append({"tool_context": tool_context, **kwargs})

    runtime = FakeRuntime()
    agent = CoverageAgent(runtime)  # type: ignore[arg-type]
    tool_context = object()

    await agent.complete_infusion_center_call(
        tool_context,  # type: ignore[arg-type]
        outcome="partial",
        intake_summary=["Scheduling could not complete intake during this call."],
        follow_up_questions=["What is the referral intake route?"],
    )

    assert runtime.calls == [
        {
            "tool_context": tool_context,
            "outcome": "partial",
            "criteria_summary": [
                "Scheduling could not complete intake during this call."
            ],
            "unresolved_questions": ["What is the referral intake route?"],
        }
    ]

    with pytest.raises(ToolError, match="complete_infusion_center_call"):
        await agent.complete_coverage_call(
            tool_context,  # type: ignore[arg-type]
            outcome="partial",
            criteria_summary=["No coverage criteria were discussed."],
            unresolved_questions=[],
        )


@pytest.mark.asyncio
async def test_terminal_callback_follows_room_deletion_and_is_skipped_on_delete_error() -> (
    None
):
    from livekit import api

    from agent import _delete_room_then_report_terminal

    timeline: list[object] = []

    class FakeCallbacks:
        async def report_event(self, call_id: str, **kwargs: object) -> None:
            timeline.append(("callback", call_id, kwargs))

    class FakeRoom:
        name = "call-room"

    class FakeRoomService:
        async def delete_room(self, request: object) -> None:
            timeline.append(("room-deleted", request.room))  # type: ignore[attr-defined]

    class FakeApi:
        room = FakeRoomService()

    class FakeJobContext:
        room = FakeRoom()
        api = FakeApi()

    completed = await _delete_room_then_report_terminal(
        FakeJobContext(),  # type: ignore[arg-type]
        FakeCallbacks(),  # type: ignore[arg-type]
        "call-123",
        status="failed",
        outcome="no-answer",
    )

    assert completed is True
    assert timeline == [
        ("room-deleted", "call-room"),
        ("callback", "call-123", {"status": "failed", "outcome": "no-answer"}),
    ]

    class FailingRoomService:
        async def delete_room(self, request: object) -> None:
            raise api.ServerError("permission_denied", "delete failed", status=403)

    class FailingApi:
        room = FailingRoomService()

    class FailingJobContext:
        room = FakeRoom()
        api = FailingApi()

    timeline.clear()
    completed = await _delete_room_then_report_terminal(
        FailingJobContext(),  # type: ignore[arg-type]
        FakeCallbacks(),  # type: ignore[arg-type]
        "call-123",
        status="failed",
        outcome="carrier-error",
    )

    assert completed is False
    assert timeline == []

    class MissingRoomService:
        async def delete_room(self, request: object) -> None:
            timeline.append(("room-missing", request.room))  # type: ignore[attr-defined]
            raise api.ServerError(api.TwirpErrorCode.NOT_FOUND, "gone", status=404)

    class MissingApi:
        room = MissingRoomService()

    class MissingJobContext:
        room = FakeRoom()
        api = MissingApi()

    timeline.clear()
    completed = await _delete_room_then_report_terminal(
        MissingJobContext(),  # type: ignore[arg-type]
        FakeCallbacks(),  # type: ignore[arg-type]
        "call-123",
        status="completed",
        outcome="voicemail",
    )

    assert completed is True
    assert timeline == [
        ("room-missing", "call-room"),
        ("callback", "call-123", {"status": "completed", "outcome": "voicemail"}),
    ]


@pytest.mark.parametrize(
    ("sip_status_code", "outcome"),
    [
        (408, "no-answer"),
        (480, "unavailable"),
        (486, "unavailable"),
        (503, "carrier-error"),
    ],
)
def test_sip_failures_have_precise_sanitized_outcomes(
    sip_status_code: int,
    outcome: str,
) -> None:
    from livekit import api

    from agent import outcome_for_sip_call_error

    error = api.SipCallError(
        "resource_exhausted",
        "provider details must not leave the worker",
        status=500,
        metadata={"sip_status_code": str(sip_status_code)},
    )

    assert outcome_for_sip_call_error(error) == outcome


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        ("deadline_exceeded", "no-answer"),
        ("internal", "carrier-error"),
    ],
)
def test_sip_server_failures_keep_no_answer_distinct(
    code: str,
    outcome: str,
) -> None:
    from livekit import api

    from agent import outcome_for_sip_server_error

    error = api.ServerError(
        code,
        "provider details must not leave the worker",
        status=500,
    )

    assert outcome_for_sip_server_error(error) == outcome


def test_amd_log_filter_blocks_only_transcript_bearing_amd_records() -> None:
    from agent import _AMDTranscriptLogFilter, _suppress_amd_transcript_logs

    transcript_record = logging.makeLogRecord(
        {
            "name": "livekit.agents",
            "msg": "amd prediction",
            "transcript": "private voicemail greeting",
        }
    )
    other_record = logging.makeLogRecord(
        {
            "name": "livekit.agents",
            "msg": "safe agent event",
        }
    )
    unrelated_record = logging.makeLogRecord(
        {
            "name": "other.logger",
            "msg": "safe agent event",
            "transcript": "not managed by AMD",
        }
    )

    transcript_filter = _AMDTranscriptLogFilter()
    assert transcript_filter.filter(transcript_record) is False
    assert transcript_filter.filter(other_record) is True
    assert transcript_filter.filter(unrelated_record) is True

    amd_logger = logging.getLogger("livekit.agents")
    before = list(amd_logger.filters)
    with _suppress_amd_transcript_logs():
        assert any(
            isinstance(log_filter, _AMDTranscriptLogFilter)
            for log_filter in amd_logger.filters
        )
    assert amd_logger.filters == before


def test_amd_observability_tag_removes_raw_metadata_without_replacement() -> None:
    from agent import _remove_amd_tag

    operations: list[tuple[object, ...]] = []

    class FakeTagger:
        def remove(self, tag: str) -> None:
            operations.append(("remove", tag))

    class FakeJobContext:
        tagger = FakeTagger()

    _remove_amd_tag(FakeJobContext(), "human")  # type: ignore[arg-type]

    assert operations == [("remove", "lk.amd:human")]


def test_amd_tag_cleanup_removes_raw_tags_when_detection_does_not_complete() -> None:
    from agent import _remove_amd_transcript_tags

    removed: list[str] = []

    class FakeTagger:
        tags: ClassVar[set[str]] = {"lk.amd:human", "lk.amd:machine-vm", "safe:tag"}

        def remove(self, tag: str) -> None:
            removed.append(tag)

    class FakeJobContext:
        tagger = FakeTagger()

    _remove_amd_transcript_tags(FakeJobContext())  # type: ignore[arg-type]

    assert set(removed) == {"lk.amd:human", "lk.amd:machine-vm"}


def test_coverage_specialist_instructions_require_disclosure_without_contact_data() -> (
    None
):
    from livekit.agents import llm

    from agent import (
        build_coverage_instructions,
        build_private_llm_context,
    )

    context = OutboundCallContext.from_payload(
        {
            "call_id": "call-123",
            "to_phone_number": "+12125550123",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "pembrolizumab",
            "patient": {
                "quickview_data": {"synthetic": True},
                "banner_data": {"coverage": "active"},
                "contact_data": {"email": "patient@example.test"},
            },
            "criteria": [],
        }
    )

    instructions = build_coverage_instructions()
    persisted_history = llm.ChatContext()
    persisted_history.add_message(role="system", content=instructions)
    private_turn_context = build_private_llm_context(persisted_history, context)
    persisted_text = "\n".join(item.text_content for item in persisted_history.items)
    private_text = "\n".join(item.text_content for item in private_turn_context.items)

    assert "AI assistant" in instructions
    assert "Do not navigate IVRs" in instructions
    assert "synthetic" not in persisted_text
    assert "patient@example.test" not in persisted_text
    assert "contact_data" not in persisted_text
    assert "synthetic" in private_text
    assert "patient@example.test" not in private_text
    assert "contact_data" not in private_text


def test_infusion_center_call_context_uses_conduit_prompt_without_inventing_tools() -> (
    None
):
    from livekit.agents import llm

    from agent import (
        build_coverage_instructions,
        build_initial_reply_instruction,
        build_private_llm_context,
    )

    context = OutboundCallContext.from_payload(
        {
            "call_id": "call-789",
            "to_phone_number": "+12125550123",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "ustekinumab",
            "recipient": {
                "kind": "infusion-center",
                "name": "Chelsea Infusion Center",
            },
            "patient": {
                "quickview_data": {"synthetic": True},
                "banner_data": {"coverage": "active"},
            },
            "criteria": [],
        }
    )

    persisted_history = llm.ChatContext()
    persisted_history.add_message(role="system", content=build_coverage_instructions())
    private_context = build_private_llm_context(persisted_history, context)
    instructions = build_coverage_instructions()
    private_text = "\n".join(item.text_content for item in private_context.items)

    assert "Conduit" in instructions
    assert "infusion-center scheduling" in instructions
    assert "complete_infusion_center_call" in instructions
    assert "Do not leave voicemail" in instructions
    assert "do not invent" in instructions.lower()
    assert "staff member will follow up" not in instructions
    assert "promise a staff follow-up" in instructions
    assert "Chelsea Infusion Center" not in instructions
    assert "Recipient type: infusion-center" in private_text
    assert "Recipient name: Chelsea Infusion Center" in private_text
    opening = build_initial_reply_instruction(context)
    assert "Conduit" in opening
    assert "scheduling or intake" in opening
    assert "appointment" in opening

    payer_opening = build_initial_reply_instruction(
        OutboundCallContext.from_payload(
            {
                "call_id": "call-790",
                "to_phone_number": "+12125550123",
                "payer": "aetna",
                "plan_type": "commercial",
                "drug": "ustekinumab",
                "patient": {},
                "criteria": [],
            }
        )
    )
    assert "coverage criteria" in payer_opening
    assert "Conduit" not in payer_opening


@pytest.mark.asyncio
async def test_infusion_center_call_does_not_prefetch_payer_policy_research() -> None:
    from agent import CoverageCallRuntime

    class FakeResearcher:
        def __init__(self) -> None:
            self.calls: list[dict[str, str]] = []

        async def search(self, **kwargs: str) -> list[PublicSource]:
            self.calls.append(kwargs)
            return []

    context = OutboundCallContext.from_payload(
        {
            "call_id": "call-901",
            "to_phone_number": "+12125550123",
            "payer": "aetna",
            "plan_type": "commercial",
            "drug": "ustekinumab",
            "recipient": {
                "kind": "infusion-center",
                "name": "Chelsea Infusion Center",
            },
            "patient": {},
            "criteria": [],
        }
    )
    researcher = FakeResearcher()
    runtime = CoverageCallRuntime(
        context=context,
        callbacks=object(),  # type: ignore[arg-type]
        job_context=object(),  # type: ignore[arg-type]
        researcher=researcher,  # type: ignore[arg-type]
    )

    assert await runtime.ensure_public_fallback() == []
    assert researcher.calls == []


def test_pipeline_uses_the_configured_direct_provider_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent

    calls: dict[str, dict[str, object]] = {}

    class FakeSession:
        def __init__(self, **kwargs: object) -> None:
            calls["session"] = kwargs

    def fake_llm(**kwargs: object) -> object:
        calls["llm"] = kwargs
        return object()

    def fake_stt(**kwargs: object) -> object:
        calls["stt"] = kwargs
        return object()

    def fake_tts(**kwargs: object) -> object:
        calls["tts"] = kwargs
        return object()

    monkeypatch.setattr(agent.cerebras, "LLM", fake_llm)
    monkeypatch.setattr(agent.deepgram, "STT", fake_stt)
    monkeypatch.setattr(agent.cartesia, "TTS", fake_tts)
    monkeypatch.setattr(agent, "AgentSession", FakeSession)

    session, _, _ = agent.create_voice_pipeline()

    assert isinstance(session, FakeSession)
    assert calls["llm"] == {"model": "gemma-4-31b"}
    assert calls["stt"] == {"model": "nova-3", "language": "en-US"}
    assert calls["tts"] == {
        "model": "sonic-3.5",
        "voice": "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
    }
    assert calls["session"]["preemptive_generation"] is True


def test_public_query_excludes_untrusted_tool_text() -> None:
    query = build_public_criteria_query(
        payer="aetna",
        plan_type="commercial",
        drug="pembrolizumab",
        topic="Jane Doe, member 123456789, needs an exception",
    )

    assert query == "Aetna commercial pembrolizumab coverage criteria"
    assert "Jane" not in query
    assert "123456789" not in query
    assert official_domains_for("aetna") == ("aetna.com", "cms.gov")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:8000",
        "http://localhost:8000",
        "http://10.0.0.4:8000",
        "http://example.test:8000",
        "http://127.0.0.1:8000/internal",
    ],
)
def test_backend_client_rejects_non_loopback_or_non_http_urls(base_url: str) -> None:
    with pytest.raises(BackendConfigurationError):
        BackendCallClient(base_url=base_url, token="capability-token")


def test_backend_client_accepts_a_loopback_http_url() -> None:
    BackendCallClient(base_url="http://127.0.0.1:8000", token="capability-token")


@pytest.mark.asyncio
async def test_tavily_search_is_limited_to_official_domains_and_three_sources() -> None:
    class FakeTavilyClient:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] | None = None
            self.closed = False

        async def search(self, **kwargs: object) -> dict[str, object]:
            self.kwargs = kwargs
            return {
                "results": [
                    {
                        "title": "Policy one",
                        "url": "https://www.aetna.com/policy-one",
                        "content": "First official source.",
                    },
                    {
                        "title": "Policy two",
                        "url": "https://cms.gov/policy-two",
                        "content": "Second official source.",
                    },
                    {
                        "title": "Untrusted",
                        "url": "https://example.com/not-allowed",
                        "content": "This must not be returned.",
                    },
                    {
                        "title": "Wrong scheme",
                        "url": "ftp://aetna.com/not-allowed",
                        "content": "This must not be returned.",
                    },
                    {
                        "title": "Policy three",
                        "url": "https://aetna.com/policy-three",
                        "content": "Third official source.",
                    },
                    {
                        "title": "Policy four",
                        "url": "https://aetna.com/policy-four",
                        "content": "This exceeds the result cap.",
                    },
                ]
            }

        async def close(self) -> None:
            self.closed = True

    fake = FakeTavilyClient()
    researcher = PublicCriteriaResearcher(
        api_key="test-key",
        client_factory=lambda _: fake,
    )

    sources = await researcher.search(
        payer="aetna",
        plan_type="commercial",
        drug="pembrolizumab",
        topic="member Alice Smith asks whether this is covered",
    )

    assert fake.kwargs == {
        "query": "Aetna commercial pembrolizumab coverage criteria",
        "search_depth": "basic",
        "max_results": 3,
        "include_domains": ["aetna.com", "cms.gov"],
        "timeout": 4.0,
    }
    assert [source.url for source in sources] == [
        "https://www.aetna.com/policy-one",
        "https://cms.gov/policy-two",
        "https://aetna.com/policy-three",
    ]
    assert fake.closed is True


@pytest.mark.asyncio
async def test_tavily_rejects_a_non_mapping_response() -> None:
    class FakeTavilyClient:
        async def search(self, **kwargs: object) -> list[object]:
            return []

        async def close(self) -> None:
            return None

    researcher = PublicCriteriaResearcher(
        api_key="test-key",
        client_factory=lambda _: FakeTavilyClient(),
    )

    with pytest.raises(PublicResearchError, match="invalid response"):
        await researcher.search(
            payer="aetna",
            plan_type="commercial",
            drug="pembrolizumab",
            topic="coverage criteria",
        )


@pytest.mark.asyncio
async def test_backend_client_uses_capability_header_and_never_sends_transcript() -> (
    None
):
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "call_id": "call-123",
                    "to_phone_number": "+12125550123",
                    "payer": "aetna",
                    "plan_type": "commercial",
                    "drug": "pembrolizumab",
                    "patient": {},
                    "criteria": [],
                },
            )
        return httpx.Response(204)

    client = BackendCallClient(
        base_url="http://127.0.0.1:8000",
        token="capability-token",
        transport=httpx.MockTransport(handler),
    )

    context = await client.fetch_context("call-123")
    await client.report_event(
        "call-123",
        status="completed",
        outcome="completed",
        summary=CallSummary(
            criteria_summary=["Criteria were confirmed."],
            unresolved_questions=["None"],
            public_sources=[{"label": "Aetna", "url": "https://aetna.com/policy"}],
        ),
    )

    assert context.call_id == "call-123"
    assert [request.url.path for request in captured] == [
        "/internal/calls/call-123/context",
        "/internal/calls/call-123/events",
    ]
    assert all(
        request.headers["X-Call-Context-Token"] == "capability-token"
        for request in captured
    )
    event_body = json.loads(captured[1].content)
    assert event_body == {
        "status": "completed",
        "outcome": "completed",
        "summary": {
            "criteria_summary": ["Criteria were confirmed."],
            "unresolved_questions": ["None"],
            "public_sources": [{"label": "Aetna", "url": "https://aetna.com/policy"}],
        },
    }
    assert "transcript" not in event_body


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["context-error", "agent-error", "carrier-error"])
async def test_failure_events_preserve_hyphenated_outcome_wire_values(
    outcome: str,
) -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    client = BackendCallClient(
        base_url="http://127.0.0.1:8000",
        token="capability-token",
        transport=httpx.MockTransport(handler),
    )

    await client.report_event(
        "call-123",
        status="failed",
        outcome=outcome,
        error="sanitized failure",
    )

    assert json.loads(captured[0].content) == {
        "status": "failed",
        "outcome": outcome,
        "error": "sanitized failure",
    }


@pytest.mark.parametrize(
    ("category", "outcome"),
    [
        ("human", None),
        ("uncertain", None),
        ("machine-vm", "voicemail"),
        ("machine-unavailable", "unavailable"),
        ("machine-ivr", "ivr"),
    ],
)
def test_amd_classification_only_continues_for_human_or_uncertain(
    category: str, outcome: str | None
) -> None:
    assert outcome_for_amd_category(category) == outcome
