"""Keep cloud upload failure references beside their original Chinese text."""

import sqlalchemy as sa
from alembic import op

revision = "c6f14d3a9b72"
down_revision = "b5e03c29f8a1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("library_file_replica", sa.Column("error_ref", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("library_file_replica") as batch:
        batch.drop_column("error_ref")
