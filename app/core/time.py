from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return naive UTC for the existing database DateTime columns."""

    return datetime.now(UTC).replace(tzinfo=None)
