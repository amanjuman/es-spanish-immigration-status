"""The single global check queue.

Every visit to the government site — on-demand or scheduled — flows through
one worker with enforced spacing between runs. Two concurrent browser
sessions from the same IP is exactly what escalates the F5 WAF, so this
serialisation is a correctness requirement, not an optimisation.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from ..config import settings
from ..core.browser import StatusChecker
from ..core.captcha import build_solver
from ..core.models import CheckError, CheckRequest, CheckResult

log = logging.getLogger(__name__)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    request: CheckRequest
    monitor_id: int | None = None  # None → on-demand job
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: JobStatus = JobStatus.QUEUED
    progress: str = "Waiting in queue…"
    result: CheckResult | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    finished_at: datetime | None = None

    def public_view(self, position: int | None = None) -> dict:
        view = {
            "id": self.id,
            "status": self.status.value,
            "progress": self.progress,
            "error": self.error,
            "expediente_id": self.request.expediente_id,
        }
        if position is not None and self.status == JobStatus.QUEUED:
            view["queue_position"] = position
        if self.result:
            view["fields"] = self.result.fields
            view["checked_at"] = self.result.checked_at.isoformat()
        return view


class CheckQueue:
    def __init__(self):
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._queued_monitor_ids: set[int] = set()
        self._worker_task: asyncio.Task | None = None
        self._last_run_finished: float = 0.0
        self.checker = StatusChecker(
            base_url=settings.base_url,
            solver=build_solver(settings.captcha_provider, settings.captcha_api_key,
                                settings.captcha_ocr_fallback),
            max_captcha_attempts=settings.captcha_max_attempts,
            debug_dir=settings.debug_dir if settings.debug_screenshots else None,
        )

    def start(self) -> None:
        self._worker_task = asyncio.create_task(self._worker(), name="check-queue-worker")

    async def stop(self) -> None:
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    def submit(self, request: CheckRequest, monitor_id: int | None = None) -> Job:
        if monitor_id is not None:
            if monitor_id in self._queued_monitor_ids:
                raise ValueError("This monitor already has a check queued")
            self._queued_monitor_ids.add(monitor_id)
        job = Job(request=request, monitor_id=monitor_id)
        self._jobs[job.id] = job
        self._queue.put_nowait(job)
        log.info("Queued job %s for %s (monitor=%s, depth=%d)",
                 job.id, request.expediente_id, monitor_id, self._queue.qsize())
        return job

    def is_monitor_queued(self, monitor_id: int) -> bool:
        return monitor_id in self._queued_monitor_ids

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def find_active(self, request: CheckRequest) -> Job | None:
        """A queued/running job for the same lookup — dedup for on-demand
        submissions so identical requests share one queue slot."""
        for job in self._jobs.values():
            if (job.request == request
                    and job.status in (JobStatus.QUEUED, JobStatus.RUNNING)):
                return job
        return None

    def position(self, job: Job) -> int:
        """1-based position among queued jobs (approximate, FIFO by creation)."""
        queued = sorted(
            (j for j in self._jobs.values() if j.status == JobStatus.QUEUED),
            key=lambda j: j.created_at,
        )
        try:
            return queued.index(job) + 1
        except ValueError:
            return 0

    def _prune_finished(self, keep: int = 200) -> None:
        finished = [j for j in self._jobs.values() if j.finished_at]
        finished.sort(key=lambda j: j.finished_at)
        for job in finished[:-keep] if len(finished) > keep else []:
            del self._jobs[job.id]

    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            job = await self._queue.get()
            wait = settings.check_spacing_seconds - (loop.time() - self._last_run_finished)
            if wait > 0:
                job.progress = f"Pacing requests to the site (waiting {int(wait)}s)…"
                await asyncio.sleep(wait)

            job.status = JobStatus.RUNNING
            job.progress = "Starting browser…"

            async def report(msg: str, _job=job) -> None:
                _job.progress = msg

            try:
                job.result = await asyncio.wait_for(
                    self.checker.check(job.request, progress=report, debug_label=job.id),
                    timeout=600,
                )
                job.status = JobStatus.DONE
                job.progress = "Done"
                from .limits import result_cache
                result_cache.put(job.request.expediente_id,
                                 job.request.fecha_presentacion,
                                 job.request.anio_nacimiento, job.result)
            except CheckError as e:
                job.status = JobStatus.ERROR
                job.error = str(e)
                log.warning("Job %s failed: %s", job.id, e)
            except asyncio.CancelledError:
                job.status = JobStatus.ERROR
                job.error = "Server shut down during the check."
                raise
            except Exception as e:
                job.status = JobStatus.ERROR
                job.error = f"Unexpected error: {e}"
                log.exception("Job %s crashed", job.id)
            finally:
                job.finished_at = datetime.now()
                self._last_run_finished = loop.time()
                if job.monitor_id is not None:
                    self._queued_monitor_ids.discard(job.monitor_id)
                self._queue.task_done()
                self._prune_finished()

            if self.on_job_finished:
                try:
                    await self.on_job_finished(job)
                except Exception:
                    log.exception("on_job_finished hook failed for job %s", job.id)

    # Set by the scheduler so monitor jobs update the DB and notify.
    on_job_finished = None


queue = CheckQueue()
