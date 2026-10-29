"""Register an isolated local classifier with the frozen ChatML input contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

CHATML_TEMPLATE = """{{ if .System }}<|im_start|>system
{{ .System }}<|im_end|>
{{ end }}{{ range .Messages }}<|im_start|>{{ .Role }}
{{ .Content }}<|im_end|>
{{ end }}<|im_start|>assistant
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-directory", type=Path, required=True)
    parser.add_argument("--system-prompt-file", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11435")
    parser.add_argument("--output", type=Path, required=True, help="New registration artifact directory")
    parser.add_argument("--experimental", action="store_true")
    args = parser.parse_args()
    import httpx

    args.output.mkdir(parents=True, exist_ok=False)
    with httpx.Client(trust_env=False) as client:
        response = client.post(f"{args.base_url}/api/show", json={"model": args.model}, timeout=5)
        if response.status_code == 200:
            raise RuntimeError("Candidate tag already exists; refusing to replace a registered model")
        if response.status_code != 404:
            response.raise_for_status()
            raise RuntimeError("Unexpected model lookup response; registration was not attempted")
    system = args.system_prompt_file.read_text(encoding="utf-8").strip()
    modelfile = args.output / "Modelfile"
    modelfile.write_text(
        f'FROM {args.model_directory.resolve()}\n\nTEMPLATE """{CHATML_TEMPLATE}"""\n\n'
        f'SYSTEM """{system}"""\n\n'
        'PARAMETER temperature 0\nPARAMETER num_predict 6\nPARAMETER repeat_penalty 1.0\n'
        'PARAMETER num_ctx 256\nPARAMETER seed 42\n'
        'PARAMETER stop "<|im_start|>"\nPARAMETER stop "<|im_end|>"\n',
        encoding="utf-8",
    )
    executable = shutil.which("ollama")
    if not executable:
        raise RuntimeError("Local Ollama executable is unavailable")
    command = [executable, "create", args.model, "-f", str(modelfile)]
    if args.experimental:
        command.append("--experimental")
    manifest = {
        "model": args.model, "baseURL": args.base_url, "shadowOnly": True,
        "sourceDirectory": str(args.model_directory.resolve()), "command": command,
        "systemSHA256": hashlib.sha256(system.encode()).hexdigest(),
        "modelfileSHA256": hashlib.sha256(modelfile.read_bytes()).hexdigest(),
        "status": "registering",
    }
    manifest_path = args.output / "registration.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    env = os.environ.copy()
    env["OLLAMA_HOST"] = args.base_url
    with (args.output / "registration.log").open("w") as logfile:
        completed = subprocess.run(command, env=env, stdout=logfile, stderr=subprocess.STDOUT, check=False)
    manifest.update(status="registered-loading" if completed.returncode == 0 else "failed", exitCode=completed.returncode)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    if completed.returncode:
        raise RuntimeError("Ollama registration failed; inspect the saved registration log")
    try:
        with httpx.Client(trust_env=False) as client:
            preload = client.post(
                f"{args.base_url}/api/generate", json={"model": args.model, "stream": False, "keep_alive": "1m"}, timeout=60,
            )
            manifest["loaderHTTPStatus"] = preload.status_code
            if not preload.is_success:
                raise RuntimeError("Registered model failed its empty-input load check; no dataset requests were attempted")
            body = preload.json()
            manifest.update(status="completed", loaderReady=True, loaderMetadata={
                key: body.get(key) for key in ("done", "done_reason", "eval_count", "prompt_eval_count")
            })
    except Exception as exc:
        manifest.update(status="loader-failed", loaderReady=False, errorType=type(exc).__name__)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        raise
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"model": args.model, "status": manifest["status"]}))


if __name__ == "__main__":
    main()
