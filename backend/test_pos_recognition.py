import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base, enable_sqlite_foreign_keys, get_db
from backend.auth_test_support import sign_in
from backend.main import app
from backend.seed import seed_data


@pytest.fixture
def client(tmp_path) -> Iterator[TestClient]:
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(tmp_path / "recognition.db"))
    )
    event.listen(engine.sync_engine, "connect", enable_sqlite_foreign_keys)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def setup() -> None:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            await seed_data(session)

    async def override_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    asyncio.run(setup())
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as test_client:
            sign_in(test_client, sessions)
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)
        asyncio.run(engine.dispose())


def test_pos_page_has_auto_operator_and_named_recognition(client: TestClient) -> None:
    english = client.get("/pos")
    arabic = client.get("/pos", params={"lang": "ar"})
    assert english.status_code == arabic.status_code == 200
    for rendered in (english.text, arabic.text):
        # Auto-operator prefix map, applied while typing.
        assert "'093': 'syriatel'" in rendered
        assert "'098': 'syriatel'" in rendered
        assert "'099': 'syriatel'" in rendered
        assert "'094': 'mtn'" in rendered
        assert "'095': 'mtn'" in rendered
        assert "'096': 'mtn'" in rendered
        # A manual operator choice is never overridden by later typing.
        assert "operatorTouched" in rendered
        # A sale name is a snapshot, never an identity lookup.
        assert "/api/customers/lookup-by-name" not in rendered
        assert "resolveNamedCustomer" not in rendered
        assert "{ input: nameInput, anchor: nameAnchor }" not in rendered
        assert "fetch('/api/customers'" not in rendered
        # A blank name is a one-time sale with no customer record.
        assert "messages.oneTimeSale" in rendered
        # Feedback live region below the form.
        assert 'id="register-status"' in rendered
        assert 'role="status"' in rendered
    assert "The name will be saved for this sale only." in english.text
    assert "Sale without a name." in english.text
    # Arabic script messages are emitted by tojson as escaped unicode.
    import json

    for message in (
        "سيُحفظ الاسم لهذا البيع فقط.",
        "بيع بلا اسم.",
    ):
        assert json.dumps(message, ensure_ascii=True)[1:-1] in arabic.text
