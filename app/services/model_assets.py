from __future__ import annotations

from pathlib import Path

from app.core.config import Settings


def finetuned_model_status(settings: Settings) -> dict:
    """检查配置中的微调模型文件和模型定义文件是否存在。

    返回路径、文件大小及注册脚本名称；存在文件不代表已注册到模型服务或已能完成推理。
    """
    root = settings.project_root
    model_dir = resolve_model_dir(settings)
    gguf_path = model_dir / settings.finetuned_model_file
    modelfile_path = model_dir / "Modelfile"
    return {
        "name": settings.finetuned_model_name,
        "directory": str(model_dir.relative_to(root)) if model_dir.is_relative_to(root) else str(model_dir),
        "ggufFile": settings.finetuned_model_file,
        "ggufExists": gguf_path.exists(),
        "ggufSizeBytes": gguf_path.stat().st_size if gguf_path.exists() else 0,
        "modelfileExists": modelfile_path.exists(),
        "ollamaCreateCommand": "scripts/create-finetuned-model.sh",
    }


def resolve_model_dir(settings: Settings) -> Path:
    """将模型目录配置转换为实际路径。

    相对路径以项目根目录解释，绝对路径保持不变。
    """
    path = Path(settings.finetuned_model_dir)
    return path if path.is_absolute() else settings.project_root / path
