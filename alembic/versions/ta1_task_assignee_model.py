"""task_assignee mapped as the TaskAssignee model: schema check only

Revision ID: ta1_task_assignee_model
Revises: pc1_provisioning_claim_token
Create Date: 2026-10-07

refactor/task-assignee. The bare `task_assignee` Table became the
`TaskAssignee` model with the same columns, primary key and
ck_task_assignee_role, so there is nothing to change in the database. This
revision only checks that the live table has the shape the model maps, so a
database that drifted fails here instead of at runtime, and it keeps the
model change on the migrate path (the CI guard and the prod migrate workflow
key on alembic/versions/**).

Hand-written, house style: lock_timeout, post-upgrade assert.
"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy.sql import text


revision: str = "ta1_task_assignee_model"
down_revision: Union[str, Sequence[str], None] = "pc1_provisioning_claim_token"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(text("SET lock_timeout = '5s'"))

    columns = {
        row[0]
        for row in connection.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'task_assignee'"
        ))
    }
    assert {"task_id", "user_id", "role"} <= columns, f"task_assignee columns: {sorted(columns)}"
    assert connection.execute(text(
        "SELECT 1 FROM pg_constraint WHERE conname = 'ck_task_assignee_role'"
    )).scalar() == 1, "ck_task_assignee_role is missing"


def downgrade() -> None:
    # Nothing was changed on upgrade.
    pass
