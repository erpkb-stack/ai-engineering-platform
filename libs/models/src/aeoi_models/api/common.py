from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Page[T](BaseModel):
    """Cursor page. `next_cursor` is opaque; pass it back as ?cursor= to continue."""

    model_config = ConfigDict(frozen=True)

    items: list[T]
    next_cursor: str | None = Field(default=None, description="null = last page")
