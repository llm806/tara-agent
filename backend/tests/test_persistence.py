from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from tara_agent.config import Settings
from tara_agent.persistence import Base, Database, DatabaseConfigurationError


def test_persistence_metadata_contains_auth_and_trace_tables() -> None:
    assert set(Base.metadata.tables) == {
        "agent_traces",
        "auth_sessions",
        "chat_messages",
        "chat_sessions",
        "trace_spans",
        "users",
    }


def test_session_sequences_are_unique_and_trace_values_are_checked() -> None:
    messages = Base.metadata.tables["chat_messages"]
    traces = Base.metadata.tables["agent_traces"]
    spans = Base.metadata.tables["trace_spans"]

    message_uniques = {
        constraint.name
        for constraint in messages.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    span_checks = {
        constraint.name
        for constraint in spans.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert "uq_chat_messages_session_sequence" in message_uniques
    assert "continued_from_trace_id" not in traces.columns
    assert "parent_trace_id" not in traces.columns
    assert "ck_trace_spans_duration_nonnegative" in span_checks
    assert "ck_trace_spans_total_tokens_nonnegative" in span_checks


def test_owned_records_are_deleted_with_their_parent() -> None:
    sessions = Base.metadata.tables["chat_sessions"]
    traces = Base.metadata.tables["agent_traces"]
    spans = Base.metadata.tables["trace_spans"]

    trace_session_fk = next(
        constraint
        for constraint in traces.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and list(constraint.columns)[0].name == "session_id"
    )
    span_trace_fk = next(
        constraint
        for constraint in spans.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and list(constraint.columns)[0].name == "trace_id"
    )
    session_user_fk = next(
        constraint
        for constraint in sessions.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        and list(constraint.columns)[0].name == "user_id"
    )

    assert session_user_fk.ondelete == "CASCADE"
    assert trace_session_fk.ondelete == "CASCADE"
    assert span_trace_fk.ondelete == "CASCADE"


def test_database_url_accepts_standard_postgresql_scheme() -> None:
    settings = Settings(database_url="postgresql://user:pass@localhost/database", _env_file=None)

    assert settings.get_database_url() == (
        "postgresql+asyncpg://user:pass@localhost/database"
    )


def test_database_requires_an_explicit_url() -> None:
    settings = Settings(database_url=None, _env_file=None)

    try:
        Database(settings)
    except DatabaseConfigurationError as error:
        assert "DATABASE_URL" in str(error)
    else:
        raise AssertionError("缺少数据库地址时应拒绝创建连接池")
