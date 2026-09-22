"""明确 Trace 之间的续接关系命名。

修订版本：20260921_06
前置版本：20260918_05
创建时间：2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_06"
down_revision: str | Sequence[str] | None = "20260918_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """将容易误解为层级关系的字段改为续接关系。"""

    op.drop_index("ix_agent_traces_parent_trace_id", table_name="agent_traces")
    op.drop_constraint(
        "fk_agent_traces_parent_trace_id_agent_traces",
        "agent_traces",
        type_="foreignkey",
    )
    op.alter_column(
        "agent_traces",
        "parent_trace_id",
        new_column_name="continued_from_trace_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
    )
    op.create_foreign_key(
        "fk_agent_traces_continued_from_trace_id_agent_traces",
        "agent_traces",
        "agent_traces",
        ["continued_from_trace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_agent_traces_continued_from_trace_id",
        "agent_traces",
        ["continued_from_trace_id"],
    )
    op.execute(
        sa.text(
            """
            UPDATE agent_traces
            SET output_data = jsonb_set(
                output_data #- '{route,referenced_trace_id}',
                '{route,continued_from_trace_id}',
                output_data #> '{route,referenced_trace_id}',
                true
            )
            WHERE output_data #> '{route,referenced_trace_id}' IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE trace_spans
            SET output_data = jsonb_set(
                output_data - 'referenced_trace_id',
                '{continued_from_trace_id}',
                output_data -> 'referenced_trace_id',
                true
            )
            WHERE output_data -> 'referenced_trace_id' IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE trace_spans
            SET attributes = jsonb_set(
                    attributes,
                    '{continued_from_trace_id}',
                    artifact_refs -> 0 -> 'trace_id',
                    true
                ),
                artifact_refs = '[]'::jsonb
            WHERE jsonb_array_length(artifact_refs) = 1
              AND artifact_refs -> 0 ->> 'kind' IN (
                  'analysis_trace',
                  'conversation_trace'
              )
            """
        )
    )


def downgrade() -> None:
    """恢复旧的字段名称。"""

    op.execute(
        sa.text(
            """
            UPDATE agent_traces
            SET output_data = jsonb_set(
                output_data #- '{route,continued_from_trace_id}',
                '{route,referenced_trace_id}',
                output_data #> '{route,continued_from_trace_id}',
                true
            )
            WHERE output_data #> '{route,continued_from_trace_id}' IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE trace_spans
            SET output_data = jsonb_set(
                output_data - 'continued_from_trace_id',
                '{referenced_trace_id}',
                output_data -> 'continued_from_trace_id',
                true
            )
            WHERE output_data -> 'continued_from_trace_id' IS NOT NULL
            """
        )
    )

    op.drop_index(
        "ix_agent_traces_continued_from_trace_id",
        table_name="agent_traces",
    )
    op.drop_constraint(
        "fk_agent_traces_continued_from_trace_id_agent_traces",
        "agent_traces",
        type_="foreignkey",
    )
    op.alter_column(
        "agent_traces",
        "continued_from_trace_id",
        new_column_name="parent_trace_id",
        existing_type=sa.Uuid(),
        existing_nullable=True,
    )
    op.create_foreign_key(
        "fk_agent_traces_parent_trace_id_agent_traces",
        "agent_traces",
        "agent_traces",
        ["parent_trace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_agent_traces_parent_trace_id",
        "agent_traces",
        ["parent_trace_id"],
    )
