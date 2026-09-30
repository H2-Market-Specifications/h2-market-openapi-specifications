from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .._vendor.h2_message_validation import SemanticValidator as _SharedSemanticValidator
from .._vendor.h2_message_validation import semantic_validator_for as _shared_semantic_validator_for
from ..errors import ErrorDetail


@dataclass(frozen=True)
class SemanticValidator:
    """Convert shared semantic violations to mock-backend error details."""

    message_type: str
    message_sub_type: str
    _validator: _SharedSemanticValidator = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_validator",
            _shared_semantic_validator_for(self.message_type, self.message_sub_type),
        )

    def validate(self, payload: Mapping[str, Any]) -> tuple[ErrorDetail, ...]:
        return tuple(ErrorDetail(path=violation.path, message=violation.message) for violation in self._validator.validate(payload))


def semantic_validator_for(message_type: str, message_sub_type: str) -> SemanticValidator:
    return SemanticValidator(message_type, message_sub_type)
