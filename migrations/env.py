from alembic import context
from sqlalchemy.schema import CreateSchema

from tracker.db import Base, database_schema, database_url, make_engine


def offline():
    schema = database_schema()
    context.configure(url=database_url(), target_metadata=Base.metadata, literal_binds=True,
                      version_table_schema=schema)
    with context.begin_transaction():
        if schema:
            context.execute(CreateSchema(schema, if_not_exists=True))
            context.execute(f'SET search_path TO "{schema}"')
        context.run_migrations()


def online():
    with make_engine().connect() as connection:
        schema = database_schema()
        if schema:
            connection.execute(CreateSchema(schema, if_not_exists=True))
            connection.commit()
        context.configure(connection=connection, target_metadata=Base.metadata, version_table_schema=schema)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    offline()
else:
    online()
