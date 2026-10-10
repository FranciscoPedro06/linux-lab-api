"""An API on a real uvicorn server with PostgreSQL, for WebSocket tests."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import text

from linuxlab.auth import sessions
from linuxlab.auth.models import User
from linuxlab.content.loader import load_content
from linuxlab.content.sync import sync_content
from linuxlab.labs.lifecycle import Labs
from linuxlab.labs.models import EndReason, LabSession
from linuxlab.labs.runtime import LabRuntime
from linuxlab.labs.terminal.relay import TerminalRegistry
from linuxlab.main import create_app
from tests.conftest import app_settings
from tests.content.support import FIXTURE

from .terminal_client import serve

# A published mission of the synthetic content in tests/fixtures/content.
MISSION = "sample-file"


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

    async def lab(self, account: Account, mission: str = MISSION) -> LabSession:
        creation = await self.labs.create(account.id, mission)
        assert creation.lab.status == "ready", creation.lab.status
        return creation.lab

    async def status(self, lab: LabSession) -> tuple[str, str | None]:
        current = await self.labs.get(lab.id)
        assert current is not None
        return current.status, current.end_reason

    async def end(self, lab: LabSession, reason: EndReason = EndReason.USER) -> None:
        await self.labs.end(lab.id, reason)

    async def sync(self, root: Path) -> None:
        """Sync the content in `root`, as `linuxlab content sync` would."""
        await sync_content(self.app.state.sessionmaker, load_content(root))

    async def sql(self, statement: str, **params: object) -> list[Any]:
        async with self.app.state.engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []


@asynccontextmanager
async def running_api(runtime: LabRuntime, **settings: object) -> AsyncIterator[Api]:
    """Serve the API on an emptied database with the synthetic missions published.
    The reaper does not run."""
    app = create_app(app_settings(**settings), lab_runtime=runtime, start_reaper=False)
    async with serve(app) as url:
        api = Api(url, app)
        await api.sql(
            "TRUNCATE users, auth_sessions, lab_sessions, modules, missions, mission_versions"
        )
        await api.sync(FIXTURE)
        yield api
