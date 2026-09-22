"""移除把多轮对话建模为单条 Trace 续接关系的字段。

Revision ID: 20260922_07
Revises: 20260921_06
Create Date: 2026-09-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_07"
down_revision: str | None = "20260921_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """删除线性续接字段，并清理历史响应中的旧路由属性。"""

    op.execute(
        sa.text(
            """
            UPDATE agent_traces
            SET output_data = jsonb_set(
                output_data,
                '{route}',
                (output_data -> 'route')
                    - 'is_follow_up'
                    - 'continued_from_trace_id',
                false
            )
            WHERE jsonb_typeof(output_data -> 'route') = 'object'
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE trace_spans
            SET output_data = output_data
                    - 'is_follow_up'
                    - 'continued_from_trace_id'
            WHERE output_data ?| ARRAY[
                'is_follow_up',
                'continued_from_trace_id'
            ]
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE trace_spans
            SET attributes = attributes - 'continued_from_trace_id'
            WHERE attributes ? 'continued_from_trace_id'
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
    op.drop_column("agent_traces", "continued_from_trace_id")


def downgrade() -> None:
    """恢复可为空的旧字段；已删除的关系数据不会重新构造。"""

    op.add_column(
        "agent_traces",
        sa.Column("continued_from_trace_id", sa.Uuid(), nullable=True),
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
