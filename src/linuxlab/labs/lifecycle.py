"""Lab lifecycle: creating, ending and reconciling labs. See docs/architecture.md#labs.

Two systems hold a lab's state, with no transaction spanning them:

- PostgreSQL is the source of truth for ownership and lifecycle (lab_sessions);
- Docker is the source of truth for whether the container exists and runs.

Every step is ordered so that repeating it, or running it concurrently with another
actor (a request, the reaper, a crashed and restarted API), is safe:

- creation commits the row before creating the container, so every lab container
  has a row by the time it can be listed;
- ending commits `terminating` before touching the terminal or the container, so a
  lab is never reported usable while it is being removed;
- only a confirmed removal moves a lab to `terminated`; until then the reaper keeps
  retrying it;
- each status change is a conditional UPDATE from the expected status, so
  concurrent actors cannot apply conflicting transitions, and the database refuses
  transitions that are not in the lifecycle at all.

Containers are found by their deterministic name and accepted only if their labels
say they are this deployment's container for that lab.

A lab is created for a published mission. The mission's current version is pinned,
and its parameters generated, in the transaction that inserts the row; everything
after that reads the pinned version. The container is then prepared (`labctl init`,
then the version's setup script) before the lab becomes ready. Only the request that
inserted the row runs setup, once: a provisioning interrupted by a crash is failed by
the reaper, never resumed.

Replacing a lab (a reset, for the same mission, or a mission switch) never reuses it:
the lab is ended and removed, then a new lab is created that names it in
previous_lab_id. A lab has at most one successor (unique index), so repeating a
replacement, or racing another one, converges on the same successor. There is no
state of its own: a replacement interrupted after the lab ended is resumed by asking
for it again. See docs/architecture.md#replacing-a-lab.

A lab asked for through a session is inserted only while that session exists: the
inserting transaction locks the session's row, which logout deletes, so each insert
is ordered before or after the logout. Logout ends the user's lab again once the
session is gone, which catches every lab inserted before. See
docs/architecture.md#logout-and-lab-creation.
"""

import asyncio
import enum
import logging
import uuid
from collections.abc import Callable, Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from linuxlab.auth.models import AuthSession
from linuxlab.content.models import Mission, MissionVersion, Module
from linuxlab.content.params import ParamError, generate_params
from linuxlab.content.stored import (
    InvalidSpecError,
    ProvisioningSpec,
    SetupScript,
    provisioning_spec,
)
from linuxlab.labs.models import (
    ACTIVE_STATUSES,
    REPLACEMENT_REASONS,
    RUNNING_STATUSES,
    EndReason,
    LabSession,
    LabStatus,
)
from linuxlab.labs.runtime import (
    ContainerInfo,
    ContainerNotFoundError,
    LabContainerSpec,
    LabRuntime,
    LabRuntimeError,
)
from linuxlab.labs.runtime.spec import (
    DEPLOYMENT_LABEL,
    LAB_ID_LABEL,
    MANAGED_LABEL,
    container_name,
)
from linuxlab.labs.terminal.relay import TerminalRegistry

logger = logging.getLogger(__name__)

# Timeouts from docs/architecture.md#labs.
NO_TERMINAL_TIMEOUT = timedelta(minutes=15)
NO_INPUT_TIMEOUT = timedelta(minutes=30)
MAX_LIFETIME = timedelta(hours=2)
PROVISIONING_TIMEOUT = timedelta(minutes=2)
# A lab left in terminating or failed this long is finished by the reaper. Before
# that, the request that ended it is presumably still removing it. At startup there
# is no such request, and the reaper finishes them right away.
TERMINATION_GRACE = timedelta(minutes=1)
# How long ending a lab waits for its terminal to close, including the terminal's
# cleanup exec (10 s), before removing the container anyway.
TERMINAL_CLOSE_SECONDS = 15

# Preparing a container, inside the provisioning timeout. labctl ships with the lab
# image and runs as root; setup runs as the version's setup user and reads the script
# from standard input, so the script is never written to the lab's filesystem.
LABCTL_INIT = ("python3", "-I", "-S", "/opt/labctl/labctl", "init")
LABCTL_INIT_SECONDS = 10
SETUP_COMMAND = ("bash", "-euo", "pipefail")
PARAM_ENV_PREFIX = "LAB_PARAM_"
PUBLISHED = "published"

UNIQUE_VIOLATION = "23505"
# Serializes lab creation, so the global capacity check cannot be raced.
CREATION_LOCK = 0x6C61_6273  # "labs"


class LabCapacityError(Exception):
    pass


class LabStartError(Exception):
    """Provisioning failed. The lab has been marked failed and cleaned up."""


class MissionNotFoundError(Exception):
    """No published mission with this slug. Nothing was created."""


class DifferentMissionError(Exception):
    """The user already has a lab for another mission. Nothing was created."""

    def __init__(self, lab: "LabSession") -> None:
        super().__init__(lab.id)
        self.lab = lab


class MissionSpecError(Exception):
    """The mission's current version cannot be provisioned: its stored specification
    is invalid or its parameters could not be generated. Nothing was created."""


class NotCurrentError(Exception):
    """The lab cannot be replaced: it already has a successor for another mission, the
    user has another active lab, or it was ended by a logout or by a replacement of the
    other kind."""


class SessionEndedError(Exception):
    """The session that asked for the lab was ended (logout) before the lab was
    inserted. Nothing was created."""


class PreviousLabNotRemovedError(Exception):
    """The lab being replaced has ended but its container could not be removed yet.
    It stays terminating for the reaper; asking again resumes the replacement."""


class PreparationStage(enum.StrEnum):
    INIT = "init"  # labctl init, part of the platform
    SETUP = "setup"  # the mission version's setup script


class PreparationError(Exception):
    """labctl init or setup did not complete. Carries no output and no parameter.

    `stage` and `timed_out` say which command failed and how, so that callers (the
    mission tests) can tell a platform failure from a mission's own without reading
    the message.
    """

    def __init__(self, stage: PreparationStage, exit_code: int, timed_out: bool) -> None:
        command = "labctl init" if stage == PreparationStage.INIT else "setup"
        super().__init__(f"{command}: exit {exit_code}, timed out {timed_out}")
        self.stage = stage
        self.exit_code = exit_code
        self.timed_out = timed_out


class LabGoneError(Exception):
    """The lab's container is no longer running; the lab has been ended."""


@dataclass(frozen=True)
class Creation:
    lab: LabSession
    created: bool


@dataclass(frozen=True)
class Target:
    """A published mission version selected for a new lab, with what provisioning
    reads. The parameters are secret."""

    mission_id: int
    slug: str
    version: int
    provisioning: ProvisioningSpec
    image: str
    params: dict[str, str]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Labs:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        runtime: LabRuntime,
        terminals: TerminalRegistry,
        *,
        image: str,
        deployment: str,
        capacity: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._runtime = runtime
        self._terminals = terminals
        # Image aliases a mission may name, resolved from trusted configuration.
        self._images = {"base": image}
        self._deployment = deployment
        self._capacity = capacity

    # Queries

    async def get(self, lab_id: uuid.UUID) -> LabSession | None:
        async with self._sessionmaker() as db:
            return await db.get(LabSession, lab_id)

    async def active(self, user_id: uuid.UUID) -> LabSession | None:
        async with self._sessionmaker() as db:
            return await db.scalar(_active_lab(user_id))

    # Creation

    async def create(
        self, user_id: uuid.UUID, mission_slug: str, *, session_id: int | None
    ) -> Creation:
        """Create a lab for the user and the mission, or return the user's active lab.

        An active lab for the same mission is returned as it is; one for another
        mission raises DifferentMissionError, unless it is already terminating.
        Raises MissionNotFoundError unless the mission is published, MissionSpecError
        if its current version cannot be provisioned, LabCapacityError when the global
        cap is reached and LabStartError if the container could not be started or
        prepared.

        `session_id` is the auth session the lab is asked for through, None only
        outside a request. SessionEndedError if it is gone when the lab is inserted.
        """
        async with self._sessionmaker() as db:
            await db.execute(select(func.pg_advisory_xact_lock(CREATION_LOCK)))
            existing = await db.scalar(_active_lab(user_id))
            if existing is not None:
                return await self._existing(db, existing, mission_slug)
            # The version is read, and pinned below, in the transaction that inserts
            # the lab: a sync that publishes a new version afterwards does not change it.
            target = await self._target(db, mission_slug)
            try:
                lab = await self._insert(db, user_id, target, session_id)
            except IntegrityError as error:
                await db.rollback()
                if getattr(error.orig, "sqlstate", None) != UNIQUE_VIOLATION:
                    raise
                existing = await db.scalar(_active_lab(user_id))
                if existing is None:
                    raise
                return await self._existing(db, existing, mission_slug)
        return await self._provision(lab, target)

    async def _target(self, db: AsyncSession, mission_slug: str) -> Target:
        """The mission's current version, if the mission is published, with its
        parameters generated. Raises MissionNotFoundError or MissionSpecError."""
        pinned = (
            await db.execute(
                select(Mission.id, MissionVersion.version, MissionVersion.spec)
                .join(Module, Module.id == Mission.module_id)
                .join(
                    MissionVersion,
                    (MissionVersion.mission_id == Mission.id)
                    & (MissionVersion.version == Mission.current_version),
                )
                .where(
                    (Mission.slug == mission_slug)
                    & (Mission.status == PUBLISHED)
                    & (Module.status == PUBLISHED)
                    & Mission.position.is_not(None)
                )
            )
        ).one_or_none()
        if pinned is None:
            raise MissionNotFoundError(mission_slug)
        mission_id, version, spec = pinned
        try:
            provisioning = provisioning_spec(spec)
            image = self._images[provisioning.environment.image]
            params = generate_params(provisioning.params)
        except (InvalidSpecError, ParamError, KeyError) as error:
            # These messages name fields and parameters, never values.
            logger.error(
                "lab not created, mission version unusable: mission=%s version=%s error=%s",
                mission_slug,
                version,
                error,
            )
            raise MissionSpecError(mission_slug) from None
        return Target(mission_id, mission_slug, version, provisioning, image, params)

    async def _insert(
        self,
        db: AsyncSession,
        user_id: uuid.UUID,
        target: Target,
        session_id: int | None,
        previous_lab_id: uuid.UUID | None = None,
    ) -> LabSession:
        """Insert the lab in provisioning and commit. The caller holds the creation lock.

        Raises SessionEndedError when the session is gone, LabCapacityError when the
        global cap is reached, and IntegrityError when the user already has an active
        lab or the previous lab a successor.
        """
        if session_id is not None:
            # Held until the commit. Logout's DELETE of the row waits for it, and an
            # insert that comes after the DELETE finds no row.
            session = await db.scalar(
                select(AuthSession.id)
                .where(AuthSession.id == session_id)
                .with_for_update(key_share=True)
            )
            if session is None:
                raise SessionEndedError
        active = await db.scalar(
            select(func.count())
            .select_from(LabSession)
            .where(LabSession.status.in_(ACTIVE_STATUSES))
        )
        if (active or 0) >= self._capacity:
            raise LabCapacityError
        now = utcnow()
        lab = LabSession(
            id=uuid.uuid4(),
            user_id=user_id,
            status=LabStatus.PROVISIONING,
            created_at=now,
            last_activity_at=now,
            expires_at=now + MAX_LIFETIME,
            mission_id=target.mission_id,
            mission_version=target.version,
            params=target.params,
            previous_lab_id=previous_lab_id,
        )
        db.add(lab)
        # The partial unique indexes are what guarantee one active lab per user and one
        # successor per lab.
        await db.commit()
        return lab

    async def _provision(self, lab: LabSession, target: Target) -> Creation:
        """Create, start and prepare the container of a lab just inserted, then mark it
        ready."""
        logger.info(
            "lab provisioning: lab=%s user=%s mission=%s version=%s previous=%s",
            lab.id,
            lab.user_id,
            target.slug,
            target.version,
            lab.previous_lab_id,
        )
        try:
            async with asyncio.timeout(PROVISIONING_TIMEOUT.total_seconds()):
                info = await self._runtime.create(
                    LabContainerSpec(
                        lab_id=lab.lab_key, image=target.image, deployment=self._deployment
                    )
                )
                await self._runtime.start(info.id)
                await self._prepare(lab.id, info.id, target.provisioning.setup, target.params)
        except (LabRuntimeError, PreparationError, TimeoutError) as error:
            if await self._fail(lab.id, EndReason.PROVISIONING_FAILED):
                logger.error("lab provisioning failed: lab=%s error=%s", lab.id, error)
                raise LabStartError from error
            # The lab was ended meanwhile, and its container removed under the request.
            return Creation(await self._ended_while_provisioning(lab.id), created=True)

        ready = await self._transition(
            lab.id, {LabStatus.PROVISIONING}, LabStatus.READY, last_activity_at=utcnow()
        )
        if ready is not None:
            logger.info("lab ready: lab=%s", lab.id)
            return Creation(ready, created=True)
        return Creation(await self._ended_while_provisioning(lab.id), created=True)

    # Replacement

    async def replace(
        self,
        lab_id: uuid.UUID,
        mission_slug: str,
        *,
        session_id: int | None,
        admit: Callable[[], None],
    ) -> Creation:
        """Replace the lab with a new one for the mission: a reset when it is the lab's
        mission, a mission switch otherwise. The caller has checked ownership.

        1. If the lab already has a successor, return it when it is for this mission
           (created=False, in whatever state it is) and raise NotCurrentError
           otherwise. Nothing is created: a lab is replaced at most once.
        2. Raise NotCurrentError if the user has another active lab, if the lab was
           ended by a logout, or if a replacement of the other kind ended it.
        3. Call `admit`, which raises to refuse, unless a replacement already ended the
           lab: resuming it creates no more labs than the first attempt counted.
        4. Select the mission's current version and generate its parameters (raises
           MissionNotFoundError or MissionSpecError), and check that capacity is left
           (LabCapacityError). The lab is untouched so far.
        5. End the lab with `reset` or `mission_switch` (a reason recorded before is
           kept) and remove its container. Raise PreviousLabNotRemovedError if the
           removal is not confirmed.
        6. Create the successor at the selected version and provision it as create()
           does. From here on LabCapacityError (taken meanwhile), SessionEndedError
           (logged out meanwhile) and LabStartError leave the user without an active
           lab.
        """
        async with self._sessionmaker() as db:
            source = await db.get(LabSession, lab_id)
            assert source is not None
            successor = await db.scalar(_successor(lab_id))
            if successor is not None:
                return await self._converge(db, successor, mission_slug)
            active = await db.scalar(_active_lab(source.user_id))
            if active is not None and active.id != source.id:
                raise NotCurrentError(lab_id)
            source_slug = await self._slug(db, source.mission_id)
        reason = EndReason.RESET if source_slug == mission_slug else EndReason.MISSION_SWITCH
        if source.end_reason == EndReason.LOGOUT:
            raise NotCurrentError(lab_id)
        if source.end_reason in REPLACEMENT_REASONS:
            if source.end_reason != reason:
                raise NotCurrentError(lab_id)
        else:
            admit()

        async with self._sessionmaker() as db:
            target = await self._target(db, mission_slug)
            # The lab being replaced frees its own place.
            active_count = await db.scalar(
                select(func.count())
                .select_from(LabSession)
                .where(LabSession.status.in_(ACTIVE_STATUSES), LabSession.id != lab_id)
            )
        if (active_count or 0) >= self._capacity:
            raise LabCapacityError
        logger.info(
            "lab replacement: lab=%s reason=%s mission=%s version=%s",
            lab_id,
            reason,
            target.slug,
            target.version,
        )

        ended = await self.end(lab_id, reason)
        assert ended is not None
        if ended.status != LabStatus.TERMINATED:
            raise PreviousLabNotRemovedError(lab_id)
        if ended.end_reason == EndReason.LOGOUT:
            # Logged out while the lab was being replaced.
            raise NotCurrentError(lab_id)
        return await self._create_successor(ended, target, session_id)

    async def _create_successor(
        self, source: LabSession, target: Target, session_id: int | None
    ) -> Creation:
        async with self._sessionmaker() as db:
            await db.execute(select(func.pg_advisory_xact_lock(CREATION_LOCK)))
            successor = await db.scalar(_successor(source.id))
            if successor is not None:
                return await self._converge(db, successor, target.slug)
            if await db.scalar(_active_lab(source.user_id)) is not None:
                raise NotCurrentError(source.id)
            try:
                lab = await self._insert(
                    db, source.user_id, target, session_id, previous_lab_id=source.id
                )
            except IntegrityError as error:
                await db.rollback()
                if getattr(error.orig, "sqlstate", None) != UNIQUE_VIOLATION:
                    raise
                successor = await db.scalar(_successor(source.id))
                if successor is None:
                    raise NotCurrentError(source.id) from None
                return await self._converge(db, successor, target.slug)
        return await self._provision(lab, target)

    async def _converge(
        self, db: AsyncSession, successor: LabSession, mission_slug: str
    ) -> Creation:
        """The lab's successor, if it is for the mission asked for."""
        if await self._slug(db, successor.mission_id) != mission_slug:
            raise NotCurrentError(successor.previous_lab_id)
        return Creation(successor, created=False)

    async def _slug(self, db: AsyncSession, mission_id: int | None) -> str | None:
        if mission_id is None:
            return None
        return await db.scalar(select(Mission.slug).where(Mission.id == mission_id))

    async def _existing(
        self, db: AsyncSession, existing: LabSession, mission_slug: str
    ) -> Creation:
        """The user's active lab, unless it is for another mission and not ending."""
        if (
            existing.status != LabStatus.TERMINATING
            and await self._slug(db, existing.mission_id) != mission_slug
        ):
            raise DifferentMissionError(existing)
        return Creation(existing, created=False)

    async def _prepare(
        self,
        lab_id: uuid.UUID,
        container_id: str,
        setup: SetupScript,
        params: Mapping[str, str],
    ) -> None:
        """Run labctl init, then the setup script, in the lab's container.

        Output is discarded, since it may contain parameters. Only the exit code and
        whether the command timed out are reported.
        """
        init = await self._runtime.exec(
            container_id, LABCTL_INIT, user="root", time_limit=LABCTL_INIT_SECONDS
        )
        if init.exit_code != 0 or init.timed_out:
            raise PreparationError(PreparationStage.INIT, init.exit_code, init.timed_out)
        result = await self._runtime.exec(
            container_id,
            SETUP_COMMAND,
            user=setup.user,
            time_limit=setup.timeout_seconds,
            stdin=setup.script.encode("utf-8"),
            env={f"{PARAM_ENV_PREFIX}{name.upper()}": value for name, value in params.items()},
        )
        logger.info(
            "lab setup finished: lab=%s exit=%s timed_out=%s",
            lab_id,
            result.exit_code,
            result.timed_out,
        )
        if result.exit_code != 0 or result.timed_out:
            raise PreparationError(PreparationStage.SETUP, result.exit_code, result.timed_out)

    async def _ended_while_provisioning(self, lab_id: uuid.UUID) -> LabSession:
        # Ended by logout, delete or the reaper. Whoever ended it may have removed the
        # container before it existed, so remove it here as well.
        logger.info("lab ended while provisioning: lab=%s", lab_id)
        await self._remove_quietly(lab_id.hex)
        lab = await self.get(lab_id)
        assert lab is not None
        return lab

    # Ending

    async def end(self, lab_id: uuid.UUID, reason: EndReason) -> LabSession | None:
        """End the lab and remove its terminal and container.

        Idempotent. The first recorded reason is kept. If the container cannot be
        removed now, the lab stays `terminating` and the reaper retries. Returns the
        lab's state afterwards.
        """
        ended = await self._transition(
            lab_id,
            RUNNING_STATUSES,
            LabStatus.TERMINATING,
            end_reason=reason,
            ended_at=utcnow(),
        )
        if ended is not None:
            logger.info("lab ending: lab=%s reason=%s", lab_id, reason)
        lab = ended or await self.get(lab_id)
        if lab is None:
            return None
        if lab.status in (LabStatus.TERMINATING, LabStatus.FAILED):
            await self._finish(lab)
            lab = await self.get(lab_id)
        return lab

    async def end_active(self, user_id: uuid.UUID, reason: EndReason) -> LabSession | None:
        lab = await self.active(user_id)
        if lab is None:
            return None
        return await self.end(lab.id, reason)

    async def _fail(self, lab_id: uuid.UUID, reason: EndReason) -> bool:
        """provisioning -> failed, then clean up. False if the lab had already ended."""
        failed = await self._transition(
            lab_id, {LabStatus.PROVISIONING}, LabStatus.FAILED, end_reason=reason, ended_at=utcnow()
        )
        if failed is None:
            return False
        await self._finish(failed)
        return True

    async def _finish(self, lab: LabSession) -> bool:
        """terminating or failed -> terminated: terminal, then container, then the row."""
        with suppress(TimeoutError):
            async with asyncio.timeout(TERMINAL_CLOSE_SECONDS):
                await self._terminals.end(lab.lab_key)
        try:
            await self._remove_container(lab.lab_key)
        except LabRuntimeError as error:
            logger.warning("lab removal failed, will retry: lab=%s error=%s", lab.id, error)
            return False
        if await self._transition(
            lab.id, {LabStatus.TERMINATING, LabStatus.FAILED}, LabStatus.TERMINATED
        ):
            logger.info("lab terminated: lab=%s reason=%s", lab.id, lab.end_reason)
        return True

    # Containers

    async def running_container(self, lab: LabSession) -> ContainerInfo:
        """The running container of a ready lab.

        If the container stopped or disappeared, ends the lab (recording an OOM kill
        when Docker reports one) and raises LabGoneError.
        """
        info = await self._container(lab.lab_key)
        if info is not None and info.running:
            return info
        reason = EndReason.OOM if info is not None and info.oom_killed else EndReason.CONTAINER_LOST
        logger.warning("lab container not running: lab=%s reason=%s", lab.id, reason)
        await self.end(lab.id, reason)
        raise LabGoneError(lab.id)

    async def _container(self, lab_key: str) -> ContainerInfo | None:
        try:
            info = await self._runtime.inspect(container_name(lab_key))
        except ContainerNotFoundError:
            return None
        if not self._owns(info, lab_key):
            # Something else took the name; never touch it.
            logger.warning("container name in use by another container: lab=%s", lab_key)
            return None
        return info

    def _owns(self, info: ContainerInfo, lab_key: str) -> bool:
        return (
            info.labels.get(MANAGED_LABEL) == "true"
            and info.labels.get(LAB_ID_LABEL) == lab_key
            and info.labels.get(DEPLOYMENT_LABEL) == self._deployment
            and info.name == container_name(lab_key)
        )

    async def _remove_container(self, lab_key: str) -> None:
        info = await self._container(lab_key)
        if info is not None:
            await self._runtime.remove(info.id)

    async def _remove_quietly(self, lab_key: str) -> None:
        try:
            await self._remove_container(lab_key)
        except LabRuntimeError as error:
            logger.warning("lab container removal failed: lab=%s error=%s", lab_key, error)

    # Reconciliation

    async def reconcile(self, *, startup: bool = False, now: datetime | None = None) -> None:
        """One reaper pass. See docs/architecture.md#labs.

        - stores terminal activity recorded since the last pass;
        - ends ready labs past a timeout, and ready labs whose container stopped
          (recording OOM kills) or disappeared;
        - fails labs stuck in provisioning;
        - finishes labs left in terminating or failed, after a grace period that does
          not apply at startup;
        - removes this deployment's lab containers that have no unfinished lab.

        `now` replaces the current time, for tests.
        """
        await self._store_activity()
        now = now or utcnow()
        # Listed before the rows are read: a container is created only after its row
        # is committed, so every listed lab container has a visible row.
        containers = await self._runtime.list_labs(self._deployment)
        async with self._sessionmaker() as db:
            labs = list(
                await db.scalars(
                    select(LabSession).where(LabSession.status != LabStatus.TERMINATED)
                )
            )
        by_key = {container.labels.get(LAB_ID_LABEL, ""): container for container in containers}

        for lab in labs:
            try:
                await self._reconcile_lab(lab, by_key.get(lab.lab_key), now, startup=startup)
            except Exception:
                logger.exception("lab reconciliation failed: lab=%s", lab.id)

        unfinished = {lab.lab_key for lab in labs}
        for container in containers:
            lab_key = container.labels.get(LAB_ID_LABEL, "")
            if lab_key in unfinished:
                continue
            if not self._owns(container, lab_key):
                logger.warning("unexpected lab container left alone: container=%s", container.name)
                continue
            # No lab, or a lab already terminated: the container should not exist.
            logger.warning("removing orphan lab container: lab=%s", lab_key)
            try:
                await self._runtime.remove(container.id)
            except LabRuntimeError as error:
                logger.warning("orphan removal failed: lab=%s error=%s", lab_key, error)

    async def _reconcile_lab(
        self, lab: LabSession, listed: ContainerInfo | None, now: datetime, *, startup: bool
    ) -> None:
        if lab.status == LabStatus.PROVISIONING:
            if now - lab.created_at > PROVISIONING_TIMEOUT:
                logger.warning("lab stuck in provisioning: lab=%s", lab.id)
                await self._fail(lab.id, EndReason.PROVISIONING_TIMEOUT)
        elif lab.status == LabStatus.READY:
            reason = self._timeout(lab, now)
            if reason is not None:
                await self.end(lab.id, reason)
            elif listed is None or not listed.running:
                # The listing may predate a container created since; inspect it directly.
                with suppress(LabGoneError):
                    await self.running_container(lab)
        elif lab.ended_at is not None and (startup or now - lab.ended_at > TERMINATION_GRACE):
            await self._finish(lab)

    def _timeout(self, lab: LabSession, now: datetime) -> EndReason | None:
        if now >= lab.expires_at:
            return EndReason.MAX_LIFETIME
        idle = now - lab.last_activity_at
        if self._terminals.connected(lab.lab_key):
            return EndReason.NO_INPUT if idle >= NO_INPUT_TIMEOUT else None
        return EndReason.NO_TERMINAL if idle >= NO_TERMINAL_TIMEOUT else None

    async def _store_activity(self) -> None:
        activity = self._terminals.take_activity()
        if not activity:
            return
        async with self._sessionmaker() as db:
            for lab_key, seen in activity.items():
                await db.execute(
                    update(LabSession)
                    .where(LabSession.id == uuid.UUID(hex=lab_key))
                    .where(LabSession.status == LabStatus.READY)
                    .values(last_activity_at=func.greatest(LabSession.last_activity_at, seen))
                )
            await db.commit()

    # Transitions

    async def _transition(
        self,
        lab_id: uuid.UUID,
        sources: Collection[LabStatus],
        target: LabStatus,
        **values: Any,
    ) -> LabSession | None:
        """Move the lab to `target` if it is in one of `sources`. Returns it, or None."""
        async with self._sessionmaker() as db:
            lab = await db.scalar(
                update(LabSession)
                .where(LabSession.id == lab_id)
                .where(LabSession.status.in_(sources))
                .values(status=target, **values)
                .returning(LabSession)
            )
            await db.commit()
            return lab


def _successor(lab_id: uuid.UUID) -> Select[LabSession]:
    return select(LabSession).where(LabSession.previous_lab_id == lab_id)


def _active_lab(user_id: uuid.UUID) -> Select[LabSession]:
    return select(LabSession).where(
        LabSession.user_id == user_id, LabSession.status.in_(ACTIVE_STATUSES)
    )
