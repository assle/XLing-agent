import json
import os
import subprocess
from pathlib import Path

from app.core.config import Settings
from app.services.model_assets import finetuned_model_status

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_ollama_model_is_the_single_answer_model_identity(tmp_path):
    settings = Settings(
        _env_file=None,
        ollama_model="answer-v2",
        finetuned_model_dir=str(tmp_path),
        finetuned_model_file="answer.gguf",
    )

    status = finetuned_model_status(settings)

    assert status["name"] == "answer-v2"
    assert "finetuned_model_name" not in settings.model_dump()


def test_registration_script_uses_ollama_model_instead_of_the_retired_name(tmp_path):
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "answer.gguf").write_bytes(b"test-only")
    (model_dir / "Modelfile").write_text("FROM ./answer.gguf\n", encoding="utf-8")
    ollama_log = tmp_path / "ollama.json"
    fake_ollama = tmp_path / "ollama"
    fake_ollama.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['OLLAMA_LOG'], 'w') as output:\n"
        "    json.dump(sys.argv[1:], output)\n",
        encoding="utf-8",
    )
    fake_ollama.chmod(0o755)
    env = os.environ.copy()
    env.update({
        "OLLAMA_BIN": str(fake_ollama),
        "OLLAMA_LOG": str(ollama_log),
        "OLLAMA_MODEL": "answer-v3",
        "FINETUNED_MODEL_NAME": "retired-name",
        "FINETUNED_MODEL_DIR": str(model_dir),
        "FINETUNED_MODEL_FILE": "answer.gguf",
    })

    subprocess.run(
        [str(PROJECT_ROOT / "scripts/create-finetuned-model.sh")],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(ollama_log.read_text(encoding="utf-8")) == [
        "create", "answer-v3", "-f", str(model_dir / "Modelfile"),
    ]
