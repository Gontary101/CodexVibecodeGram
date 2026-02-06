from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable

from .config import Settings
from .models import Artifact
from .repository import Repository


def _kind_for_extension(ext: str) -> str:
    ext = ext.lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        return "image"
    if ext in {".mp4", ".webm"}:
        return "video"
    if ext in {".log", ".txt", ".json"}:
        return "log"
    if ext == ".pdf":
        return "document"
    return "file"


class ArtifactService:
    _PATH_IN_BACKTICKS = re.compile(r"`([^`\n]+)`")
    _PATH_GENERIC = re.compile(r"(?<![\w/])([~./]?[A-Za-z0-9_\-./]+?\.[A-Za-z0-9]{1,10})(?![\w])")

    def __init__(self, repo: Repository, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(64 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def register_file(self, job_id: int, path: Path, kind: str | None = None) -> Artifact | None:
        if not path.exists() or not path.is_file():
            return None
        ext = path.suffix.lower()
        if ext and ext not in self._settings.allowed_artifact_extensions:
            return None
        size = path.stat().st_size
        if size == 0:
            return None
        if size > self._settings.max_artifact_bytes:
            return None
        sha256 = self._sha256(path)
        artifact_kind = kind or _kind_for_extension(ext)
        return self._repo.add_artifact(job_id, artifact_kind, path.resolve(), size, sha256)

    def collect_from_run_dir(self, job_id: int, run_dir: Path) -> list[Artifact]:
        artifacts: list[Artifact] = []
        for candidate in sorted(run_dir.rglob("*")):
            if not candidate.is_file():
                continue
            artifact = self.register_file(job_id, candidate)
            if artifact is not None:
                artifacts.append(artifact)
        return artifacts

    def _iter_path_candidates(self, text: str) -> Iterable[str]:
        seen: set[str] = set()
        for match in self._PATH_IN_BACKTICKS.findall(text):
            candidate = match.strip().strip("\"'`")
            if candidate and candidate not in seen:
                seen.add(candidate)
                yield candidate
        for match in self._PATH_GENERIC.findall(text):
            candidate = match.strip().strip("\"'`")
            if candidate and candidate not in seen:
                seen.add(candidate)
                yield candidate

    def _is_under_any_root(self, path: Path, roots: Iterable[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root.resolve())
                return True
            except ValueError:
                continue
        return False

    def _resolve_candidate(self, candidate: str, base_dir: Path, roots: Iterable[Path]) -> Path | None:
        if candidate.startswith(("http://", "https://", "file://")):
            return None
        raw = Path(candidate).expanduser()
        resolved = (base_dir / raw).resolve() if not raw.is_absolute() else raw.resolve()
        if not resolved.exists() or not resolved.is_file():
            return None
        if not self._is_under_any_root(resolved, roots):
            return None
        return resolved

    def collect_from_output_texts(
        self,
        job_id: int,
        texts: Iterable[str],
        *,
        base_dir: Path,
        roots: Iterable[Path] | None = None,
    ) -> list[Artifact]:
        allowed_roots = tuple(roots or (base_dir,))
        existing_paths = {artifact.path.resolve() for artifact in self._repo.list_artifacts(job_id)}
        added: list[Artifact] = []
        for text in texts:
            if not text:
                continue
            for candidate in self._iter_path_candidates(text):
                resolved = self._resolve_candidate(candidate, base_dir=base_dir, roots=allowed_roots)
                if resolved is None or resolved in existing_paths:
                    continue
                artifact = self.register_file(job_id, resolved)
                if artifact is None:
                    continue
                existing_paths.add(resolved)
                added.append(artifact)
        return added
