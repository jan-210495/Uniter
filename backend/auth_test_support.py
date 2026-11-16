"""Test support: sign an existing ``TestClient`` in (admin by default).

Business-workflow tests predate authentication. Rather than bypassing the auth
gate, they get a genuine server-side session (cookie) plus its CSRF header, so
every request still passes through ``auth.authorize_request`` exactly as a
signed-in browser's would.
"""
from __future__ import annotations

import asyncio
import secrets
from functools import cache

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend import auth
from backend.database import User

TEST_ADMIN_USERNAME = "test-admin"


@cache
def _password_hash() -> str:
    # Hashed once per test run; nobody signs in with this random password.
    return auth.hash_password(secrets.token_urlsafe(16))


async def create_admin_session(
    sessions: async_sessionmaker, *, role: str = "admin", username: str = TEST_ADMIN_USERNAME
) -> tuple[str, str]:
    """Insert (or reuse) an active account and open a session for it."""
    async with sessions() as session:
        user = await session.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                username=username,
                password_hash=_password_hash(),
                role=role,
                must_change_password=False,
            )
            session.add(user)
            await session.flush()
        token = await auth._start_session(session, user)
        await session.commit()
    return token, auth._csrf_for(token)


def sign_in(
    client: TestClient, sessions: async_sessionmaker, *, role: str = "admin",
    username: str = TEST_ADMIN_USERNAME,
) -> TestClient:
    token, csrf = asyncio.run(create_admin_session(sessions, role=role, username=username))
    client.cookies.set(auth.SESSION_COOKIE, token)
    client.headers[auth.CSRF_HEADER] = csrf
    return client
