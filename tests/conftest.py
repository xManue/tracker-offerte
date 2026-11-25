import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from tracker.db import Base


@pytest.fixture
def sessions(tmp_path):
    postgres = os.getenv("TRACKER_TEST_POSTGRES_URL")
    if postgres:
        admin = create_engine(postgres)
        schema = "tracker_test_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA {schema}"))
        engine = create_engine(postgres, connect_args={"options": f"-c search_path={schema}"})
    else:
        engine = create_engine(f"sqlite:///{(tmp_path / 'tracker.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()
    if postgres:
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()
