from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from app.core.config import Settings
from app.services.ai import PromptTemplates


@dataclass(frozen=True)
class ArtifactVersion:
    chat_model: str
    classifier_model: str
    prompt_version: str
    embedding_model: str
    reranker_model: str
    index_version: str
    dataset_version: str
    calibration_version: str
    code_revision: str

    def to_dict(self) -> dict[str, str]:
        values = asdict(self)
        return {
            "chatModel": values["chat_model"],
            "classifierModel": values["classifier_model"],
            "promptVersion": values["prompt_version"],
            "embeddingModel": values["embedding_model"],
            "rerankerModel": values["reranker_model"],
            "indexVersion": values["index_version"],
            "datasetVersion": values["dataset_version"],
            "calibrationVersion": values["calibration_version"],
            "codeRevision": values["code_revision"],
        }


class ArtifactVersionResolver:
    """Resolve every input needed to reproduce one model or eval run."""

    def __init__(
        self,
        settings: Settings,
        *,
        knowledge_paths: list[Path] | None = None,
        code_revision: str | None = None,
    ) -> None:
        self.settings = settings
        self.knowledge_paths = knowledge_paths or sorted(
            (settings.project_root / "app" / "knowledge").glob("*.md")
        )
        self.code_revision = code_revision

    def current(self, dataset_path: str | Path | None = None) -> ArtifactVersion:
        embedding_model = (
            self.settings.bge_embedding_model
            if self.settings.knowledge_retriever == "bge_m3"
            else self.settings.openai_embedding_model
        )
        return ArtifactVersion(
            chat_model=self._chat_model(),
            classifier_model=self.settings.ollama_classifier_model,
            prompt_version=self._prompt_version(),
            embedding_model=embedding_model,
            reranker_model=(
                self.settings.bge_reranker_model
                if self.settings.knowledge_retriever == "bge_m3"
                and self.settings.bge_rerank_enabled
                else ""
            ),
            index_version=self._index_version(),
            dataset_version=self._dataset_version(dataset_path),
            calibration_version=self._calibration_version(),
            code_revision=self.code_revision or _git_revision(self.settings.project_root),
        )

    def _chat_model(self) -> str:
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            return self.settings.ollama_model
        if provider == "openai":
            return self.settings.openai_model
        return "mock"

    def _prompt_version(self) -> str:
        source = inspect.getsource(PromptTemplates)
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _index_version(self) -> str:
        digest = hashlib.sha256()
        embedding_model = (
            self.settings.bge_embedding_model
            if self.settings.knowledge_retriever == "bge_m3"
            else self.settings.openai_embedding_model
        )
        digest.update(embedding_model.encode("utf-8"))
        digest.update(str(self.settings.knowledge_chunk_size).encode("ascii"))
        digest.update(str(self.settings.knowledge_chunk_overlap).encode("ascii"))
        for path in sorted(self.knowledge_paths, key=lambda item: item.name):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _dataset_version(dataset_path: str | Path | None) -> str:
        if dataset_path is None:
            return ""
        path = Path(dataset_path)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _calibration_version(self) -> str:
        value = self.settings.risk_calibration_artifact
        if not value:
            return ""
        path = Path(value)
        if not path.is_absolute():
            path = self.settings.project_root / path
        if not path.exists():
            return "missing"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return "invalid"
        return str(data.get("calibrationVersion", "unknown"))


def _git_revision(project_root: Path) -> str:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return f"{revision}-dirty" if dirty else revision
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
