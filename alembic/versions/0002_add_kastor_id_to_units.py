"""Add kastor_id to UnitModel and adjust alicuota constraints

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 14:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop existing check constraint
    op.drop_constraint('chk_units_alicuota_positive', 'units', type_='check')
    
    # 2. Add kastor_id column
    op.add_column('units', sa.Column('kastor_id', sa.Integer(), nullable=True))
    op.create_unique_constraint(None, 'units', ['kastor_id'])
    op.create_index(op.f('ix_units_kastor_id'), 'units', ['kastor_id'], unique=True)
    
    # 3. Make alicuota nullable
    op.alter_column('units', 'alicuota', existing_type=sa.Numeric(precision=8, scale=5), nullable=True)
    
    # 4. Re-add check constraint
    op.create_check_constraint('chk_units_alicuota_positive', 'units', 'alicuota IS NULL OR alicuota > 0')


def downgrade() -> None:
    # 1. Drop check constraint
    op.drop_constraint('chk_units_alicuota_positive', 'units', type_='check')
    
    # 2. Revert alicuota nullable to NOT NULL
    op.execute("UPDATE units SET alicuota = 0.0 WHERE alicuota IS NULL")
    op.alter_column('units', 'alicuota', existing_type=sa.Numeric(precision=8, scale=5), nullable=False)
    
    # 3. Drop kastor_id
    op.drop_index(op.f('ix_units_kastor_id'), table_name='units')
    op.drop_constraint('uq_units_kastor_id', 'units', type_='unique')
    op.drop_column('units', 'kastor_id')
    
    # 4. Re-add original check constraint
    op.create_check_constraint('chk_units_alicuota_positive', 'units', 'alicuota > 0')
