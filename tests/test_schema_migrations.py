from sqlalchemy import create_engine, inspect, text

from downloadany_backend.schema_migrations import migrate_feedback_contact


def test_migrate_feedback_contact_upgrades_old_table():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE feedbacks (
                    id INTEGER PRIMARY KEY,
                    kind VARCHAR(16) NOT NULL,
                    text TEXT NOT NULL,
                    app_version VARCHAR(32) NOT NULL DEFAULT '',
                    platform VARCHAR(64) NOT NULL DEFAULT '',
                    device_id VARCHAR(64) NOT NULL DEFAULT '',
                    created_at DATETIME NOT NULL
                )
                """
            )
        )

    migrate_feedback_contact(engine)
    migrate_feedback_contact(engine)

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("feedbacks")}
    indexes = {index["name"] for index in inspector.get_indexes("feedbacks")}
    assert {"email", "ip_address"} <= columns
    assert "ix_feedbacks_ip_address" in indexes
