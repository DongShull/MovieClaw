"""Keep watch-import conclusion references beside their original Chinese text."""

import sqlalchemy as sa
from alembic import op

revision = "e1a62d94c8b3"
down_revision = "d9f51c83b70a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ingest_entry", sa.Column("message_ref", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("ingest_entry") as batch:
        batch.drop_column("message_ref")
