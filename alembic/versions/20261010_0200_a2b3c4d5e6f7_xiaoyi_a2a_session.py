"""xiaoyi_a2a_session: platform sessionId -> agent_session mapping.

One row per Xiaoyi cloud A2A platform sessionId (docs/research/
xiaoyi-cloud-a2a.md). Routing only — conversation content lives in
the JSONL transcript and agent_session index, never here.
"""

import sqlalchemy as sa
from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "087d01dfcecb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xiaoyi_a2a_session",
        sa.Column("platform_session_id", sa.String(), primary_key=True),
        sa.Column("agent_session_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_xiaoyi_a2a_session_agent_session_id",
        "xiaoyi_a2a_session",
        ["agent_session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_xiaoyi_a2a_session_agent_session_id", table_name="xiaoyi_a2a_session"
    )
    op.drop_table("xiaoyi_a2a_session")
