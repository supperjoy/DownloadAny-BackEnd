from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError


def _execute_if_missing(
    engine: Engine,
    *,
    is_missing: Callable[[], bool],
    statement: str,
) -> None:
    if not is_missing():
        return
    try:
        with engine.begin() as connection:
            connection.execute(text(statement))
    except DBAPIError:
        # Another worker may have completed the same startup migration after our
        # initial inspection. Only suppress the duplicate-operation race.
        if is_missing():
            raise


def migrate_feedback_contact(engine: Engine) -> None:
    """Bring feedback tables created before contact/IP tracking up to date."""

    inspector = inspect(engine)
    if not inspector.has_table("feedbacks"):
        return

    def column_is_missing(name: str) -> bool:
        return name not in {
            column["name"] for column in inspect(engine).get_columns("feedbacks")
        }

    _execute_if_missing(
        engine,
        is_missing=lambda: column_is_missing("email"),
        statement="ALTER TABLE feedbacks ADD COLUMN email VARCHAR(320) NULL",
    )
    _execute_if_missing(
        engine,
        is_missing=lambda: column_is_missing("ip_address"),
        statement=(
            "ALTER TABLE feedbacks "
            "ADD COLUMN ip_address VARCHAR(64) NOT NULL DEFAULT ''"
        ),
    )

    def index_is_missing() -> bool:
        return "ix_feedbacks_ip_address" not in {
            index["name"] for index in inspect(engine).get_indexes("feedbacks")
        }

    _execute_if_missing(
        engine,
        is_missing=index_is_missing,
        statement="CREATE INDEX ix_feedbacks_ip_address ON feedbacks (ip_address)",
    )
