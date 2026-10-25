"""Keep job subject references beside their original Chinese text."""

import sqlalchemy as sa
from alembic import op

revision = "a4d92b18e6f7"
down_revision = "f3c81a07d2e5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("job", sa.Column("subject_ref", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("job") as batch:
        batch.drop_column("subject_ref")
