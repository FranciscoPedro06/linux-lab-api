"""An API on a real uvicorn server with PostgreSQL, for WebSocket tests."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy import text

from linuxlab.auth import sessions
from linuxlab.auth.models import User
from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.models import EndReason, LabSession
from linuxlab.labs.runtime import LabRuntime
from linuxlab.labs.terminal.relay import TerminalRegistry
from linuxlab.main import create_app
from tests.conftest import app_settings

from .terminal_client import serve


@dataclass
class Account:
    id: uuid.UUID
    token: str


@dataclass
class Api:
    url: str
    app: FastAPI

    @property
    def labs(self) -> Labs:
        labs: Labs = self.app.state.labs
        return labs

    @property
    def terminals(self) -> TerminalRegistry:
        terminals: TerminalRegistry = self.app.state.terminals
        return terminals

    async def account(self, email: str) -> Account:
        user = User(id=uuid.uuid4(), email=email, password_hash="$argon2id$x", display_name="T")
        async with self.app.state.sessionmaker() as db:
            db.add(user)
            await db.flush()
            token = sessions.add_session(db, user.id, sessions.utcnow())
            await db.commit()
        return Account(user.id, token)

    async def lab(self, account: Account) -> LabSession:
        creation = await self.labs.create(account.id)
        assert creation.lab.status == "ready", creation.lab.status
        return creation.lab

    async def status(self, lab: LabSession) -> tuple[str, str | None]:
        current = await self.labs.get(lab.id)
        assert current is not None
        return current.status, current.end_reason

    async def end(self, lab: LabSession, reason: EndReason = EndReason.USER) -> None:
        await self.labs.end(lab.id, reason)

    async def sql(self, statement: str, **params: object) -> None:
        async with self.app.state.engine.begin() as connection:
            await connection.execute(text(statement), params)


@asynccontextmanager
async def running_api(runtime: LabRuntime, **settings: object) -> AsyncIterator[Api]:
    """Serve the API on an emptied database. The reaper does not run."""
    app = create_app(app_settings(**settings), lab_runtime=runtime, start_reaper=False)
    async with serve(app) as url:
        api = Api(url, app)
        await api.sql("TRUNCATE users, auth_sessions, lab_sessions")
        yield api
