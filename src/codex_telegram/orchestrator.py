from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

from .artifacts import ArtifactService
from .config import Settings
from .executor import CodexExecutor, RuntimeProfile
from .models import ExecutionContext, Job, JobMode, JobStatus
from .policy import RiskPolicy
from .repository import Repository
from .sessions import SessionManager

logger = logging.getLogger(__name__)


class NotifierProtocol(Protocol):
    async def send_text(self, text: str) -> None: ...

    async def send_job_status(self, job: Job, heading: str) -> None: ...

    async def send_artifacts(self, artifacts: list) -> None: ...


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        policy: RiskPolicy,
        executor: CodexExecutor,
        artifact_service: ArtifactService,
        session_manager: SessionManager,
        settings: Settings,
        notifier: NotifierProtocol,
    ) -> None:
        self._repo = repo
        self._policy = policy
        self._executor = executor
        self._artifact_service = artifact_service
        self._session_manager = session_manager
        self._settings = settings
        self._notifier = notifier

        self._dispatch_task: asyncio.Task | None = None
        self._running_tasks: dict[int, asyncio.Task] = {}
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._dispatch_task is not None:
            return
        self._stop_event.clear()
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="job-dispatch-loop")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._dispatch_task:
            self._dispatch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatch_task
            self._dispatch_task = None
        tasks = list(self._running_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._running_tasks.clear()

    async def submit_job(self, prompt: str, mode: JobMode, session_name: str | None = None) -> Job:
        decision = self._policy.classify_prompt(prompt)
        initial_status = JobStatus.AWAITING_APPROVAL if decision.needs_approval else JobStatus.QUEUED
        job = self._repo.create_job(
            prompt=prompt,
            mode=mode,
            session_name=session_name,
            risk_level=decision.level,
            needs_approval=decision.needs_approval,
            status=initial_status,
        )
        self._repo.append_event(
            job.id,
            "job_submitted",
            {
                "mode": mode.value,
                "session_name": session_name,
                "risk_level": decision.level.value,
                "needs_approval": decision.needs_approval,
                "reason": decision.reason,
            },
        )

        if decision.needs_approval:
            self._repo.append_event(job.id, "approval_required", {"reason": decision.reason})
            await self._notifier.send_text(
                f"Job {job.id} is waiting for approval.\nreason={decision.reason}\nUse /approve {job.id} or /reject {job.id}."
            )

        return job

    def get_job(self, job_id: int) -> Job:
        return self._repo.get_job(job_id)

    def list_jobs(self, limit: int = 20) -> list[Job]:
        return self._repo.list_jobs(limit=limit)

    def count_jobs_by_status(self) -> dict[str, int]:
        return self._repo.count_jobs_by_status()

    def list_job_artifacts(self, job_id: int):
        return self._repo.list_artifacts(job_id)

    def list_job_events(self, job_id: int, limit: int = 20):
        return self._repo.list_events(job_id, limit=limit)

    def get_runtime_profile(self) -> RuntimeProfile:
        return self._executor.get_runtime_profile()

    def set_model(self, model: str | None, reasoning_effort: str | None = None) -> RuntimeProfile:
        return self._executor.set_model(model, reasoning_effort)

    def set_permissions(self, sandbox_mode: str | None) -> RuntimeProfile:
        return self._executor.set_sandbox_mode(sandbox_mode)

    def set_approvals(self, policy: str | None) -> RuntimeProfile:
        return self._executor.set_approval_policy(policy)

    def get_effective_approval_policy(self) -> str:
        return self._executor.get_effective_approval_policy()

    def set_search(self, enabled: bool) -> RuntimeProfile:
        return self._executor.set_search(enabled)

    def set_web_search_mode(self, mode: str | None) -> RuntimeProfile:
        return self._executor.set_web_search_mode(mode)

    def get_effective_workdir(self):
        return self._executor.get_effective_workdir()

    def get_allowed_workdirs(self):
        return self._executor.get_allowed_workdirs()

    def set_workdir(self, path_value: str | None):
        return self._executor.set_workdir(path_value)

    def get_active_session_for_chat(self, chat_id: int) -> str | None:
        return self._repo.get_active_session_for_chat(chat_id)

    def set_active_session_for_chat(self, chat_id: int, session_name: str | None) -> None:
        self._repo.set_active_session_for_chat(chat_id, session_name)

    def set_personality(self, personality: str, custom_instruction: str | None = None) -> RuntimeProfile:
        return self._executor.set_personality(personality, custom_instruction)

    def set_experimental(self, feature: str, enabled: bool) -> RuntimeProfile:
        return self._executor.set_experimental_feature(feature, enabled)

    def clear_experimentals(self) -> RuntimeProfile:
        return self._executor.clear_experimental_features()

    def running_jobs_count(self) -> int:
        return len(self._running_tasks)

    async def approve_job(self, job_id: int, user_id: int) -> Job:
        job = self._repo.approve_job(job_id, user_id)
        self._repo.append_event(job_id, "job_approved", {"user_id": user_id})
        return job

    async def reject_job(self, job_id: int, user_id: int) -> Job:
        job = self._repo.reject_job(job_id, user_id)
        self._repo.append_event(job_id, "job_rejected", {"user_id": user_id})
        await self._notifier.send_job_status(job, "Job rejected")
        return job

    async def cancel_job(self, job_id: int) -> Job:
        task = self._running_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        job = self._repo.cancel_job(job_id)
        self._repo.append_event(job_id, "job_canceled", None)
        await self._notifier.send_job_status(job, "Job canceled")
        return job

    async def _dispatch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                while len(self._running_tasks) < self._settings.max_parallel_jobs:
                    job = self._repo.reserve_next_runnable_job()
                    if job is None:
                        break
                    task = asyncio.create_task(self._run_job(job), name=f"job-{job.id}")
                    self._running_tasks[job.id] = task
                    task.add_done_callback(lambda _, job_id=job.id: self._running_tasks.pop(job_id, None))
            except Exception:
                logger.exception("dispatch loop error")

            await asyncio.sleep(self._settings.worker_poll_interval)

    async def _run_job(self, job: Job) -> None:
        run_dir = self._settings.runs_dir / str(job.id)

        def _read_limited(path, max_chars: int = 200_000) -> str:
            if not path.exists() or not path.is_file():
                return ""
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                return handle.read(max_chars)

        if job.mode == JobMode.SESSION and job.session_name:
            if not self._session_manager.is_session_active(job.session_name):
                failed = self._repo.set_job_status(
                    job.id,
                    JobStatus.FAILED,
                    summary_text="Session mode requested but session is inactive",
                    error_text=f"Session '{job.session_name}' is inactive",
                    finished=True,
                    exit_code=2,
                )
                self._repo.append_event(job.id, "job_failed", {"reason": "inactive_session"})
                await self._notifier.send_job_status(failed, "Job failed")
                return

        self._repo.append_event(job.id, "job_started", None)

        ctx = ExecutionContext(
            job=job,
            run_dir=run_dir,
            approved=bool(job.approved_by) or not job.needs_approval,
        )

        try:
            result = await self._executor.execute(ctx)
        except asyncio.CancelledError:
            canceled = self._repo.set_job_status(
                job.id,
                JobStatus.CANCELED,
                summary_text="Job canceled while running",
                finished=True,
                exit_code=130,
            )
            self._repo.append_event(job.id, "job_canceled_while_running", None)
            await self._notifier.send_job_status(canceled, "Job canceled")
            raise
        except Exception as exc:
            failed = self._repo.set_job_status(
                job.id,
                JobStatus.FAILED,
                summary_text="Executor raised an unexpected error",
                error_text=str(exc),
                finished=True,
                exit_code=1,
            )
            self._repo.append_event(job.id, "job_failed", {"exception": str(exc)})
            await self._notifier.send_job_status(failed, "Job failed")
            return

        executor_workdir = (
            self._executor.get_effective_workdir()
            if hasattr(self._executor, "get_effective_workdir")
            else self._settings.codex_workdir
        )
        allowed_roots = (
            self._executor.get_allowed_workdirs()
            if hasattr(self._executor, "get_allowed_workdirs")
            else self._settings.codex_allowed_workdirs
        )

        self._artifact_service.collect_from_run_dir(job.id, run_dir)
        self._artifact_service.collect_from_output_texts(
            job.id,
            [
                _read_limited(result.stdout_path),
                _read_limited(result.stderr_path),
                result.summary or "",
                result.error_text or "",
            ],
            base_dir=result.exec_cwd or executor_workdir,
            roots=[*allowed_roots, self._settings.runs_dir],
        )

        if result.exit_code == 0:
            finished = self._repo.set_job_status(
                job.id,
                JobStatus.SUCCEEDED,
                summary_text=result.summary,
                exit_code=result.exit_code,
                finished=True,
            )
            self._repo.append_event(job.id, "job_succeeded", {"exit_code": result.exit_code})
            await self._notifier.send_job_status(finished, "Job completed")
        else:
            finished = self._repo.set_job_status(
                job.id,
                JobStatus.FAILED,
                summary_text=result.summary,
                error_text=result.error_text,
                exit_code=result.exit_code,
                finished=True,
            )
            self._repo.append_event(job.id, "job_failed", {"exit_code": result.exit_code})
            await self._notifier.send_job_status(finished, "Job failed")

        artifacts = self._repo.list_artifacts(job.id)
        await self._notifier.send_artifacts(artifacts)
