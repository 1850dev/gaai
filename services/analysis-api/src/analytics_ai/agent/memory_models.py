"""Scoped memory contracts. User statements and hypotheses are not GA4 facts."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from analytics_ai.domain.models import FrozenModel, utc_now


class MemoryEntry(FrozenModel):
    text: str = Field(min_length=1, max_length=1000)
    message_id: str
    kind: Literal["objective", "decision", "target", "hypothesis", "question"]
    origin: Literal["user_statement", "agent_hypothesis"] = "user_statement"


class MemoryQuote(FrozenModel):
    text: str = Field(min_length=1, max_length=1000)
    kind: Literal["objective", "decision", "target", "question"]


class WorkingMemory(FrozenModel):
    version: int = 1
    entries: tuple[MemoryEntry, ...] = Field(default=(), max_length=40)
    verified_finding_ids: tuple[str, ...] = Field(default=(), max_length=32)
    pending_tasks: tuple[str, ...] = Field(default=(), max_length=16)
    trimmed: bool = False


class WorkingMemoryChange(FrozenModel):
    revision: int = Field(ge=0)
    action: Literal["add", "delete"]
    kind: Literal["objective", "decision", "target", "question"]
    text: str = Field(min_length=1, max_length=1000)


class Agreement(FrozenModel):
    id: str
    revision: int = Field(ge=1)
    text: str = Field(max_length=1000)
    status: Literal["pending", "confirmed", "deleted"] = "pending"
    source_session_id: str | None = None
    source_message_id: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)


class AgreementChange(FrozenModel):
    action: Literal["propose", "confirm", "edit", "delete"]
    id: str = Field(default="", max_length=128)
    revision: int = Field(default=0, ge=0)
    text: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def valid_change(self) -> "AgreementChange":
        if self.action in {"propose", "edit"} and not self.text.strip():
            raise ValueError("agreement_text_required")
        if self.action != "propose" and not self.id:
            raise ValueError("agreement_id_required")
        if self.action in {"confirm", "delete"} and self.text:
            raise ValueError("agreement_confirmation_cannot_change_text")
        return self
