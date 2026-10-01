from __future__ import annotations

import hashlib
import inspect
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.core.config import Settings
from app.services.action_plan import ActionPlanService
from app.services.ai import PromptTemplates
from app.services.cbt import CBTService


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
        """将一次运行的版本信息转换为接口和评估文件使用的字典。

        明确映射字段名称，保留各版本值，包括尚未启用项的空字符串。
        """
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
    """汇总模型及评估运行使用的输入版本，便于比较不同运行的条件。"""

    def __init__(
        self,
        settings: Settings,
        *,
        knowledge_paths: list[Path] | None = None,
        code_revision: str | None = None,
    ) -> None:
        """记录解析版本所需的配置、知识文件列表及可选代码版本。

        未提供非空知识列表时使用项目知识目录；此处只准备路径，不加载模型。
        """
        self.settings = settings
        self.knowledge_paths = knowledge_paths or sorted(
            (settings.project_root / "app" / "knowledge").glob("*.md")
        )
        self.code_revision = code_revision

    def current(self, dataset_path: str | Path | None = None) -> ArtifactVersion:
        """汇总当前模型、提示词、知识索引、数据集和代码的版本标识。

        dataset_path 指定需要计算内容指纹的数据集；未指定时数据集版本为空。
        文件读取和提示词源码读取失败会向上传递；代码版本读取失败另有 unknown 回退值。
        """
        return ArtifactVersion(
            chat_model=self._chat_model(),
            classifier_model=self.settings.ollama_classifier_model,
            prompt_version=self._prompt_version(),
            embedding_model=self.settings.openai_embedding_model,
            reranker_model="",
            index_version=self._index_version(),
            dataset_version=self._dataset_version(dataset_path),
            calibration_version=self._calibration_version(),
            code_revision=self.code_revision or _git_revision(self.settings.project_root),
        )

    def _chat_model(self) -> str:
        """按配置中的模型提供方选择用于记录的对话模型名称。

        本地服务和远程服务分别使用对应配置，其他提供方统一标记为 mock（模拟模型）。
        """
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            return self.settings.ollama_model
        if provider == "openai":
            return self.settings.openai_model
        return "mock"

    def _prompt_version(self) -> str:
        """通过指定提示词类和相关函数的完整源码生成内容指纹。

        这里读取的是源码文本，注释和排版也会影响结果；返回固定长度的 SHA-256 摘要（内容变化检测值）。
        """
        prompt_sources = (
            PromptTemplates,
            LangGraphAgentRuntimeService._rewrite_query,
            LangGraphAgentRuntimeService._summarize_memory,
            ActionPlanService._generation_prompt,
            CBTService._extraction_prompt,
        )
        source = "\n".join(inspect.getsource(item) for item in prompt_sources)
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def _index_version(self) -> str:
        """对嵌入模型配置、切片参数和知识文件内容生成统一指纹。

        文件按名称排序后依次参与计算，避免传入列表顺序不同造成无意义的版本变化。
        """
        digest = hashlib.sha256()
        digest.update(self.settings.openai_embedding_model.encode("utf-8"))
        digest.update(str(self.settings.knowledge_chunk_size).encode("ascii"))
        digest.update(str(self.settings.knowledge_chunk_overlap).encode("ascii"))
        # 按文件名排序后计算指纹，避免列表顺序影响知识版本。
        for path in sorted(self.knowledge_paths, key=lambda item: item.name):
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _dataset_version(dataset_path: str | Path | None) -> str:
        """计算指定数据集文件的内容指纹。

        dataset_path 为 None 时返回空字符串；指定路径时读取原始字节，文件缺失等错误直接向上传递。
        """
        if dataset_path is None:
            return ""
        path = Path(dataset_path)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _calibration_version(self) -> str:
        """返回当前实现使用的空校准版本标记。

        此版本解析器尚未从校准文件读取版本，因此不能把空值理解为已验证某个校准版本。
        """
        return ""


def _git_revision(project_root: Path) -> str:
    """读取当前代码提交编号，并标记工作区是否存在未提交内容。

    project_root 指定仓库目录；状态查询也会看到未跟踪文件，因此这类文件会触发 dirty 标记。
    无法运行版本管理命令或命令失败时返回 unknown，不中断上层版本汇总。
    """
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
