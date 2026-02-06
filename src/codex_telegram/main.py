from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot

from .approval_checklists import ApprovalChecklistStore
from .approval_polls import ApprovalPollStore
from .artifacts import ArtifactService
from .bot import build_dispatcher
from .config import ConfigError, load_settings
from .db import Database
from .executor import CodexExecutor
from .logging_setup import setup_logging
from .notifier import TelegramNotifier
from .orchestrator import Orchestrator
from .policy import RiskPolicy
from .repository import Repository
from .sessions import SessionManager
from .video import VideoService

logger = logging.getLogger(__name__)


async def _run_async() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)

    db = Database(settings.sqlite_path)
    db.init_schema()
    repo = Repository(db)
    repo.ensure_owner(settings.owner_telegram_id)

    bot = Bot(token=settings.telegram_bot_token)
    approval_polls = ApprovalPollStore()
    approval_checklists = ApprovalChecklistStore()

    notifier = TelegramNotifier(
        bot=bot,
        owner_chat_id=settings.owner_telegram_id,
        response_mode=settings.telegram_response_mode,
        approval_polls=approval_polls,
        approval_checklists=approval_checklists,
        business_connection_id=settings.telegram_business_connection_id,
    )
    policy = RiskPolicy()
    executor = CodexExecutor(settings)
    artifact_service = ArtifactService(repo=repo, settings=settings)
    session_manager = SessionManager(repo=repo, settings=settings)
    video_service = VideoService(repo=repo, artifact_service=artifact_service, settings=settings)

    orchestrator = Orchestrator(
        repo=repo,
        policy=policy,
        executor=executor,
        artifact_service=artifact_service,
        session_manager=session_manager,
        settings=settings,
        notifier=notifier,
    )

    dispatcher = build_dispatcher(
        bot=bot,
        orchestrator=orchestrator,
        session_manager=session_manager,
        video_service=video_service,
        owner_user_id=settings.owner_telegram_id,
        command_cooldown_seconds=settings.command_cooldown_seconds,
        runs_dir=settings.runs_dir,
        approval_polls=approval_polls,
        approval_checklists=approval_checklists,
    )

    await orchestrator.start()
    try:
        await dispatcher.start_polling(bot)
    finally:
        await orchestrator.stop()
        await bot.session.close()
        db.close()


def run() -> int:
    try:
        asyncio.run(_run_async())
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        print("Hint: copy .env.example to .env and set required values.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
