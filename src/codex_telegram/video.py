from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .artifacts import ArtifactService
from .config import Settings
from .models import Artifact
from .repository import Repository


class VideoError(RuntimeError):
    pass


class VideoService:
    def __init__(self, repo: Repository, artifact_service: ArtifactService, settings: Settings) -> None:
        self._repo = repo
        self._artifact_service = artifact_service
        self._settings = settings

    async def generate_for_job(self, job_id: int) -> Artifact:
        self._repo.get_job(job_id)

        if shutil.which("ffmpeg") is None:
            raise VideoError("ffmpeg is not installed on the server")

        run_dir = self._settings.runs_dir / str(job_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        output_path = run_dir / "recap.mp4"

        artifacts = self._repo.list_artifacts(job_id)
        image_paths = [a.path for a in artifacts if a.kind == "image" and a.path.exists()]

        if image_paths:
            cmd = [
                "ffmpeg",
                "-y",
                "-loop",
                "1",
                "-i",
                str(image_paths[0]),
                "-t",
                "6",
                "-vf",
                "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black",
                "-pix_fmt",
                "yuv420p",
                str(output_path),
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1280x720:d=6",
                "-pix_fmt",
                "yuv420p",
                str(output_path),
            ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return_code = await proc.wait()
        if return_code != 0 or not output_path.exists():
            raise VideoError("ffmpeg failed to create recap video")

        artifact = self._artifact_service.register_file(job_id, output_path, kind="video")
        if artifact is None:
            raise VideoError("video generated but rejected by artifact policy")
        return artifact
