from enum import Enum as PyEnum

from sqlalchemy import JSON, Enum
from sqlalchemy.dialects.postgresql import JSONB

JSON_DATA = JSON().with_variant(JSONB(), "postgresql")


def enum_type(enum_cls: type[PyEnum], *, name: str) -> Enum:
    """Portable string enum, intentionally avoiding PostgreSQL-native enums."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
    )
