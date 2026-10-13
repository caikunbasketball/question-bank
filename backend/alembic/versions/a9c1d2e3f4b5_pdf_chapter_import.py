"""pdf_chapter_import: 书籍 / 章节 / 小节骨架 + 章节批量导入任务表

Revision ID: a9c1d2e3f4b5
Revises: e5f6a7b8c9d0

新增四张表::

    source_books              一本书(PDF 章节导入的载体)
    book_chapters             章
    book_sections             小节
    pdf_chapter_import_tasks  一次批量导入提交
    pdf_chapter_import_items  每章一条子任务(进度 / 重试 / 断点续传 / 查重)

并给 ``questions`` 追加 book_id / chapter_id / section_id 三个可空外键。

全部列可空、不加非空约束,是为了让既有题目行与手工建题路径零改动地继续工作。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9c1d2e3f4b5'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TASK_STATUS = sa.Enum(
    'pending', 'processing', 'completed', 'failed', 'partial', 'cancelled',
    name='chapterimporttaskstatus',
)
ITEM_STATUS = sa.Enum(
    'pending', 'processing', 'completed', 'failed', 'skipped', 'cancelled',
    name='chapterimportitemstatus',
)


def upgrade() -> None:
    op.create_table(
        'source_books',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('subject_id', sa.Integer(), nullable=True),
        sa.Column('filename', sa.String(length=255), nullable=True),
        sa.Column('content_sha256', sa.String(length=64), nullable=True),
        sa.Column('page_count', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['user.id'], ),
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_source_books_id'), 'source_books', ['id'], unique=False)
    op.create_index(op.f('ix_source_books_subject_id'), 'source_books', ['subject_id'], unique=False)
    op.create_index(op.f('ix_source_books_content_sha256'), 'source_books', ['content_sha256'], unique=False)
    op.create_index(
        'ix_source_books_subject_title', 'source_books', ['subject_id', 'title'], unique=False
    )

    op.create_table(
        'book_chapters',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('book_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=False),
        sa.Column('start_page', sa.Integer(), nullable=False),
        sa.Column('end_page', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['book_id'], ['source_books.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_book_chapters_id'), 'book_chapters', ['id'], unique=False)
    op.create_index(op.f('ix_book_chapters_book_id'), 'book_chapters', ['book_id'], unique=False)
    op.create_index(
        'ix_book_chapters_book_order', 'book_chapters', ['book_id', 'order_index'], unique=False
    )

    op.create_table(
        'book_sections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('book_id', sa.Integer(), nullable=False),
        sa.Column('chapter_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=False),
        sa.Column('start_page', sa.Integer(), nullable=False),
        sa.Column('end_page', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['book_id'], ['source_books.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['chapter_id'], ['book_chapters.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_book_sections_id'), 'book_sections', ['id'], unique=False)
    op.create_index(op.f('ix_book_sections_book_id'), 'book_sections', ['book_id'], unique=False)
    op.create_index(op.f('ix_book_sections_chapter_id'), 'book_sections', ['chapter_id'], unique=False)
    op.create_index(
        'ix_book_sections_chapter_order', 'book_sections', ['chapter_id', 'order_index'], unique=False
    )

    op.create_table(
        'pdf_chapter_import_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('book_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('subject_id', sa.Integer(), nullable=True),
        sa.Column('mode', sa.String(length=20), nullable=True),
        sa.Column('source_path', sa.String(length=512), nullable=False),
        sa.Column('source_filename', sa.String(length=255), nullable=True),
        sa.Column('status', TASK_STATUS, nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('total_items', sa.Integer(), nullable=False),
        sa.Column('completed_items', sa.Integer(), nullable=False),
        sa.Column('failed_items', sa.Integer(), nullable=False),
        sa.Column('skipped_items', sa.Integer(), nullable=False),
        sa.Column('imported_questions', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['book_id'], ['source_books.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_pdf_chapter_import_tasks_id'), 'pdf_chapter_import_tasks', ['id'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_tasks_book_id'), 'pdf_chapter_import_tasks', ['book_id'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_tasks_user_id'), 'pdf_chapter_import_tasks', ['user_id'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_tasks_status'), 'pdf_chapter_import_tasks', ['status'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_tasks_created_at'), 'pdf_chapter_import_tasks', ['created_at'], unique=False)

    op.create_table(
        'pdf_chapter_import_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('chapter_id', sa.Integer(), nullable=True),
        sa.Column('section_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('start_page', sa.Integer(), nullable=False),
        sa.Column('end_page', sa.Integer(), nullable=False),
        sa.Column('order_index', sa.Integer(), nullable=False),
        sa.Column('status', ITEM_STATUS, nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('questions_count', sa.Integer(), nullable=False),
        sa.Column('content_sha256', sa.String(length=64), nullable=True),
        sa.Column('import_task_id', sa.Integer(), nullable=True),
        sa.Column('ocr_markdown', sa.Text(), nullable=True),
        sa.Column('ocr_meta', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['chapter_id'], ['book_chapters.id'], ),
        sa.ForeignKeyConstraint(['import_task_id'], ['import_tasks.id'], ),
        sa.ForeignKeyConstraint(['section_id'], ['book_sections.id'], ),
        sa.ForeignKeyConstraint(['task_id'], ['pdf_chapter_import_tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_pdf_chapter_import_items_id'), 'pdf_chapter_import_items', ['id'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_items_task_id'), 'pdf_chapter_import_items', ['task_id'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_items_chapter_id'), 'pdf_chapter_import_items', ['chapter_id'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_items_section_id'), 'pdf_chapter_import_items', ['section_id'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_items_status'), 'pdf_chapter_import_items', ['status'], unique=False)
    op.create_index(op.f('ix_pdf_chapter_import_items_content_sha256'), 'pdf_chapter_import_items', ['content_sha256'], unique=False)
    op.create_index(
        'ix_pdf_chapter_items_task_order',
        'pdf_chapter_import_items', ['task_id', 'order_index'], unique=False,
    )
    op.create_index(
        'ix_pdf_chapter_items_chapter',
        'pdf_chapter_import_items', ['chapter_id', 'section_id'], unique=False,
    )

    # questions: 追加书籍 / 章节 / 小节归属。batch_alter_table 保证 SQLite 也能加列。
    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('book_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('chapter_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('section_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('classification_review', sa.Text(), nullable=True))
        batch_op.create_index(batch_op.f('ix_questions_book_id'), ['book_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_questions_chapter_id'), ['chapter_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_questions_section_id'), ['section_id'], unique=False)
        batch_op.create_foreign_key('fk_questions_source_books', 'source_books', ['book_id'], ['id'])
        batch_op.create_foreign_key('fk_questions_book_chapters', 'book_chapters', ['chapter_id'], ['id'])
        batch_op.create_foreign_key('fk_questions_book_sections', 'book_sections', ['section_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('questions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_questions_book_sections', type_='foreignkey')
        batch_op.drop_constraint('fk_questions_book_chapters', type_='foreignkey')
        batch_op.drop_constraint('fk_questions_source_books', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_questions_section_id'))
        batch_op.drop_index(batch_op.f('ix_questions_chapter_id'))
        batch_op.drop_index(batch_op.f('ix_questions_book_id'))
        batch_op.drop_column('section_id')
        batch_op.drop_column('chapter_id')
        batch_op.drop_column('book_id')
        batch_op.drop_column('classification_review')

    op.drop_index('ix_pdf_chapter_items_chapter', table_name='pdf_chapter_import_items')
    op.drop_index('ix_pdf_chapter_items_task_order', table_name='pdf_chapter_import_items')
    op.drop_index(op.f('ix_pdf_chapter_import_items_content_sha256'), table_name='pdf_chapter_import_items')
    op.drop_index(op.f('ix_pdf_chapter_import_items_status'), table_name='pdf_chapter_import_items')
    op.drop_index(op.f('ix_pdf_chapter_import_items_section_id'), table_name='pdf_chapter_import_items')
    op.drop_index(op.f('ix_pdf_chapter_import_items_chapter_id'), table_name='pdf_chapter_import_items')
    op.drop_index(op.f('ix_pdf_chapter_import_items_task_id'), table_name='pdf_chapter_import_items')
    op.drop_index(op.f('ix_pdf_chapter_import_items_id'), table_name='pdf_chapter_import_items')
    op.drop_table('pdf_chapter_import_items')

    op.drop_index(op.f('ix_pdf_chapter_import_tasks_created_at'), table_name='pdf_chapter_import_tasks')
    op.drop_index(op.f('ix_pdf_chapter_import_tasks_status'), table_name='pdf_chapter_import_tasks')
    op.drop_index(op.f('ix_pdf_chapter_import_tasks_user_id'), table_name='pdf_chapter_import_tasks')
    op.drop_index(op.f('ix_pdf_chapter_import_tasks_book_id'), table_name='pdf_chapter_import_tasks')
    op.drop_index(op.f('ix_pdf_chapter_import_tasks_id'), table_name='pdf_chapter_import_tasks')
    op.drop_table('pdf_chapter_import_tasks')

    op.drop_index(op.f('ix_book_sections_id'), table_name='book_sections')
    op.drop_index(op.f('ix_book_sections_book_id'), table_name='book_sections')
    op.drop_index(op.f('ix_book_sections_chapter_id'), table_name='book_sections')
    op.drop_index('ix_book_sections_chapter_order', table_name='book_sections')
    op.drop_table('book_sections')

    op.drop_index(op.f('ix_book_chapters_id'), table_name='book_chapters')
    op.drop_index(op.f('ix_book_chapters_book_id'), table_name='book_chapters')
    op.drop_index('ix_book_chapters_book_order', table_name='book_chapters')
    op.drop_table('book_chapters')

    op.drop_index(op.f('ix_source_books_id'), table_name='source_books')
    op.drop_index(op.f('ix_source_books_subject_id'), table_name='source_books')
    op.drop_index(op.f('ix_source_books_content_sha256'), table_name='source_books')
    op.drop_index('ix_source_books_subject_title', table_name='source_books')
    op.drop_table('source_books')
