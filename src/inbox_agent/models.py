"""Core data models for the InboxPilot mail-triage pipeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MailSource(StrEnum):
    """Supported origins of an email message."""

    MOCK = "mock"
    MICROSOFT_GRAPH = "microsoft_graph"


class BodyType(StrEnum):
    """Supported body representations."""

    TEXT = "text"
    HTML = "html"


class Importance(StrEnum):
    """Importance supplied by the mail provider or sender."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class Priority(StrEnum):
    """InboxPilot priority levels, from highest to lowest."""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"


class MessageCategory(StrEnum):
    """Stable category taxonomy shared by structured LLM output."""

    ACADEMIC_DEADLINE = "academic_deadline"
    ADMINISTRATIVE_DEADLINE = "administrative_deadline"
    COURSE_REGISTRATION = "course_registration"
    COURSE_CHANGE = "course_change"
    COURSE_MATERIAL = "course_material"
    EXAM_CHANGE = "exam_change"
    SCHOLARSHIP_DEADLINE = "scholarship_deadline"
    PAYMENT_DEADLINE = "payment_deadline"
    SECURITY_ALERT = "security_alert"
    LIBRARY_REMINDER = "library_reminder"
    EVENT_REGISTRATION = "event_registration"
    CAREER_EVENT = "career_event"
    ACADEMIC_CALENDAR = "academic_calendar"
    CAMPUS_ACTIVITY = "campus_activity"
    COURTESY_MESSAGE = "courtesy_message"
    NEWSLETTER = "newsletter"
    PROMOTION = "promotion"
    INCOMPLETE_MESSAGE = "incomplete_message"
    GENERAL_NOTICE = "general_notice"


class DecisionSource(StrEnum):
    """Component that produced the final triage decision."""

    RULE = "rule"
    LLM = "llm"
    HYBRID = "hybrid"


class DeadlineKind(StrEnum):
    """How a structured analyzer determined a deadline."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class FrozenModel(BaseModel):
    """Strict immutable base for pipeline values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


def _validate_email_address(value: str) -> str:
    """Apply intentionally small, provider-neutral email checks."""

    if len(value) > 320:
        raise ValueError("email address must not exceed 320 characters")
    if any(character.isspace() for character in value):
        raise ValueError("email address must not contain whitespace")
    if value.count("@") != 1:
        raise ValueError("email address must contain exactly one @ character")

    local_part, domain = value.split("@", maxsplit=1)
    if not local_part or not domain:
        raise ValueError("email address must contain local and domain parts")
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise ValueError("email address contains an invalid domain")
    return value


def _require_aware_datetime(value: datetime | None, field_name: str) -> datetime | None:
    """Reject datetimes that do not include a usable UTC offset."""

    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must include timezone information")
    return value


class EmailAddress(FrozenModel):
    """A display name and SMTP-style email address."""

    name: str = Field(default="", max_length=320)
    address: str = Field(min_length=3, max_length=320)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        """Reject clearly malformed addresses without provider-specific assumptions."""

        return _validate_email_address(value)


class MessageBody(FrozenModel):
    """An email body and its representation."""

    content_type: BodyType
    content: str = Field(default="", max_length=1_000_000)


class AttachmentMetadata(FrozenModel):
    """Safe attachment metadata; binary content is deliberately excluded."""

    name: str = Field(min_length=1, max_length=512)
    content_type: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, ge=0)
    is_inline: bool = False


class EmailMessage(FrozenModel):
    """Provider-neutral representation of a received email."""

    source: MailSource
    source_id: str = Field(min_length=1, max_length=512)
    internet_message_id: str | None = Field(default=None, max_length=998)

    subject: str = Field(default="", max_length=2_000)
    from_address: EmailAddress
    sender: EmailAddress | None = None
    reply_to: tuple[EmailAddress, ...] = ()
    to_recipients: tuple[EmailAddress, ...] = ()
    cc_recipients: tuple[EmailAddress, ...] = ()

    received_at: datetime
    sent_at: datetime | None = None

    body: MessageBody
    body_preview: str = Field(default="", max_length=5_000)

    importance: Importance = Importance.NORMAL
    inference_classification: Literal["focused", "other"] | None = None
    categories: tuple[str, ...] = Field(default=(), max_length=100)
    change_key: str | None = Field(default=None, min_length=1, max_length=512)

    has_attachments: bool = False
    attachments: tuple[AttachmentMetadata, ...] = ()

    @field_validator("received_at", "sent_at")
    @classmethod
    def validate_message_datetime(cls, value: datetime | None) -> datetime | None:
        """Require timezone-aware provider timestamps."""

        return _require_aware_datetime(value, "message datetime")

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Preserve provider categories while rejecting empty or ambiguous names."""

        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("message categories must not be empty")
        if any(len(value) > 255 for value in normalized):
            raise ValueError("message categories must not exceed 255 characters")
        casefolded = [value.casefold() for value in normalized]
        if len(casefolded) != len(set(casefolded)):
            raise ValueError("message categories must be unique ignoring case")
        return normalized

    @property
    def effective_sender(self) -> EmailAddress:
        """Return the transport sender, falling back to the visible author."""

        return self.sender or self.from_address

    @property
    def effective_has_attachments(self) -> bool:
        """Account for providers that return metadata separately from the flag."""

        return self.has_attachments or bool(self.attachments)


class MessageDataset(FrozenModel):
    """A versioned collection of sample or imported messages."""

    schema_version: Literal["1.0"] = "1.0"
    messages: tuple[EmailMessage, ...]

    @model_validator(mode="after")
    def validate_unique_source_ids(self) -> Self:
        """Prevent duplicated provider identities in one dataset."""

        identities = [(message.source, message.source_id) for message in self.messages]
        if len(identities) != len(set(identities)):
            raise ValueError("dataset contains duplicate source and source_id pairs")
        return self


class NormalizedMessage(FrozenModel):
    """Deterministic, cleaned representation used for feature extraction."""

    source: MailSource
    source_id: str = Field(min_length=1, max_length=512)

    subject: str = Field(default="", max_length=2_000)
    from_name: str = Field(default="", max_length=320)
    from_address: str = Field(min_length=3, max_length=320)
    from_domain: str = Field(min_length=1, max_length=255)
    sender_address: str | None = Field(default=None, max_length=320)
    reply_to_addresses: tuple[str, ...] = ()
    to_addresses: tuple[str, ...] = ()
    cc_addresses: tuple[str, ...] = ()

    received_at: datetime
    sent_at: datetime | None = None
    body_text: str = Field(default="", max_length=1_000_000)
    body_preview: str = Field(default="", max_length=5_000)

    importance: Importance = Importance.NORMAL
    has_attachments: bool = False

    @field_validator("from_address")
    @classmethod
    def validate_from_address(cls, value: str) -> str:
        """Validate the normalized author address."""

        return _validate_email_address(value)

    @field_validator("sender_address")
    @classmethod
    def validate_sender_address(cls, value: str | None) -> str | None:
        """Validate the optional normalized transport sender."""

        return _validate_email_address(value) if value is not None else None

    @field_validator("reply_to_addresses", "to_addresses", "cc_addresses")
    @classmethod
    def validate_address_collection(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Validate every address produced by the normalizer."""

        return tuple(_validate_email_address(value) for value in values)

    @field_validator("received_at", "sent_at")
    @classmethod
    def validate_normalized_datetime(cls, value: datetime | None) -> datetime | None:
        """Keep normalized timestamps timezone-aware."""

        return _require_aware_datetime(value, "normalized message datetime")

    @property
    def recipient_count(self) -> int:
        """Return the known To and Cc recipient count."""

        return len(self.to_addresses) + len(self.cc_addresses)


class UserContext(FrozenModel):
    """User-specific information required to calculate triage features."""

    mailbox_addresses: frozenset[str] = Field(min_length=1)
    trusted_senders: frozenset[str] = frozenset()
    trusted_domains: frozenset[str] = frozenset()
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=255)

    @field_validator("mailbox_addresses", "trusted_senders")
    @classmethod
    def normalize_email_set(cls, values: frozenset[str]) -> frozenset[str]:
        """Validate and normalize configured email addresses."""

        return frozenset(_validate_email_address(value.lower()) for value in values)

    @field_validator("trusted_domains")
    @classmethod
    def normalize_domain_set(cls, values: frozenset[str]) -> frozenset[str]:
        """Normalize domains and reject values that look like full addresses."""

        normalized: set[str] = set()
        for value in values:
            domain = value.lower().removeprefix("@").strip()
            if not domain or "@" in domain or any(character.isspace() for character in domain):
                raise ValueError("trusted domain must be a non-empty domain name")
            normalized.add(domain)
        return frozenset(normalized)


class MessageFeatures(FrozenModel):
    """Policy-dependent signals extracted from a normalized message."""

    sender_is_trusted: bool = False
    sender_domain_is_trusted: bool = False
    directly_addressed: bool = False
    recipient_count: int = Field(default=0, ge=0)
    looks_like_bulk_mail: bool = False

    urgent_keywords: tuple[str, ...] = ()
    security_keywords: tuple[str, ...] = ()
    bulk_keywords: tuple[str, ...] = ()
    action_keywords: tuple[str, ...] = ()
    opportunity_keywords: tuple[str, ...] = ()
    no_action_keywords: tuple[str, ...] = ()
    contains_unsubscribe: bool = False
    contains_deadline_language: bool = False
    detected_dates: tuple[datetime, ...] = ()

    has_attachments: bool = False
    provider_marked_high_importance: bool = False
    provider_marked_low_importance: bool = False
    external_sender: bool = False
    empty_subject: bool = False
    sender_mismatch: bool = False

    @field_validator("detected_dates")
    @classmethod
    def validate_detected_dates(cls, values: tuple[datetime, ...]) -> tuple[datetime, ...]:
        """Require extracted deadlines to retain their timezone context."""

        for value in values:
            _require_aware_datetime(value, "detected date")
        return values


class ScoreReason(FrozenModel):
    """One explainable contribution to a rule score."""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    description: str = Field(min_length=1, max_length=500)
    score_change: int = Field(ge=-100, le=100)
    matched_value: str | None = Field(default=None, max_length=320)


class RuleEvaluation(FrozenModel):
    """Auditable output of the deterministic rule engine."""

    base_score: int = Field(ge=0, le=100)
    final_score: int = Field(ge=0, le=100)
    suggested_priority: Priority
    reasons: tuple[ScoreReason, ...] = ()
    requires_review: bool = False

    @model_validator(mode="after")
    def validate_score_arithmetic(self) -> Self:
        """Ensure the published score matches its explainable contributions."""

        calculated_score = self.base_score + sum(reason.score_change for reason in self.reasons)
        clamped_score = max(0, min(100, calculated_score))
        if self.final_score != clamped_score:
            raise ValueError("final_score does not match base_score and score reasons")
        return self


class ExtractedDeadline(FrozenModel):
    """A timezone-aware deadline with evidence and extraction confidence."""

    value: datetime
    kind: DeadlineKind
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = Field(min_length=1, max_length=1_000)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: datetime) -> datetime:
        """Require an absolute instant even when the source text was relative."""

        validated = _require_aware_datetime(value, "extracted deadline")
        assert validated is not None
        return validated


class ActionItem(FrozenModel):
    """One structured task extracted from an email."""

    description: str = Field(min_length=1, max_length=1_000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str | None = Field(min_length=1, max_length=1_000)
    deadline: ExtractedDeadline | None


class LLMMessageAnalysis(FrozenModel):
    """Provider-neutral structured content returned by an LLM analyzer."""

    priority: Priority
    category: MessageCategory
    summary: str = Field(min_length=1, max_length=1_000)
    action_items: tuple[ActionItem, ...] = Field(max_length=50)
    deadline: ExtractedDeadline | None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(min_length=1, max_length=1_000)
    requires_review: bool

    @model_validator(mode="after")
    def validate_unique_action_items(self) -> Self:
        """Reject repeated task descriptions in one structured response."""

        descriptions = [item.description.casefold() for item in self.action_items]
        if len(descriptions) != len(set(descriptions)):
            raise ValueError("LLM analysis contains duplicate action item descriptions")
        return self


class LLMTokenUsage(FrozenModel):
    """Provider-neutral token accounting for one LLM request."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_cached_tokens(self) -> Self:
        """Cached input tokens must be a subset of all input tokens."""

        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens must not exceed input_tokens")
        return self

    @property
    def total_tokens(self) -> int:
        """Return total billed input and generated output tokens."""

        return self.input_tokens + self.output_tokens


class LLMAnalysisResult(FrozenModel):
    """Traceable runtime envelope around one structured LLM analysis."""

    schema_version: Literal["1.0"] = "1.0"
    message_id: str = Field(min_length=1, max_length=512)
    analysis: LLMMessageAnalysis

    provider: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    model_name: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
        max_length=200,
    )
    prompt_version: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        max_length=100,
    )
    analyzed_at: datetime
    duration_ms: int = Field(ge=0)
    usage: LLMTokenUsage | None = None
    request_id: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("analyzed_at")
    @classmethod
    def validate_analyzed_at(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamps for audit and evaluation."""

        validated = _require_aware_datetime(value, "LLM analysis timestamp")
        assert validated is not None
        return validated


class TriageResult(FrozenModel):
    """Stable public result consumed by the CLI, storage, and future UI."""

    message_id: str = Field(min_length=1, max_length=512)
    priority: Priority
    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)

    category: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    summary: str = Field(min_length=1, max_length=1_000)
    action_items: tuple[ActionItem, ...] = Field(default=(), max_length=50)
    deadline: datetime | None = None

    reasons: tuple[ScoreReason, ...] = ()
    requires_review: bool = False
    decision_source: DecisionSource = DecisionSource.RULE

    evaluated_at: datetime
    policy_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=100)

    @field_validator("deadline", "evaluated_at")
    @classmethod
    def validate_result_datetime(cls, value: datetime | None) -> datetime | None:
        """Require traceable, timezone-aware result timestamps."""

        return _require_aware_datetime(value, "triage result datetime")
