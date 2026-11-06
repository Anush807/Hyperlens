"""Shared field types."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

# Pydantic's EmailStr rejects reserved TLDs, and the spec's own seed account is
# admin@hvops.local - a .local address, which is exactly right for a service
# that never sends mail. This is a syntax check, not deliverability.
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    ),
]
