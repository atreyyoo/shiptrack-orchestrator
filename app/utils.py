import uuid


def to_uuid(value) -> uuid.UUID:
    """Parse a client-supplied id into a UUID, raising ValueError (caught by
    the API layer as a 400) rather than letting a malformed id reach asyncpg."""
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"'{value}' is not a valid id (expected a UUID)") from exc
