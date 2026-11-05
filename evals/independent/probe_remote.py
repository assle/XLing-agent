from __future__ import annotations

import sys

import httpx

from app.core.config import Settings
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient
from evals.config import get_eval_settings


def main() -> None:
    channel = sys.argv[1] if len(sys.argv) > 1 else "default"
    if channel in {"quality", "judge"}:
        base = get_eval_settings()
        if channel == "quality":
            credential = base.quality_eval_gen_api_key or base.openai_api_key
            base_url = base.quality_eval_gen_base_url or base.openai_base_url
            model = base.quality_eval_gen_model or base.openai_model
        else:
            credential = base.quality_eval_judge_api_key or base.openai_api_key
            base_url = base.quality_eval_judge_base_url
            model = base.quality_eval_judge_model
        settings = base.model_copy(update={
            "ai_provider": "openai",
            "openai_api_key": credential,
            "openai_base_url": base_url,
            "openai_model": model,
        })
    else:
        settings = Settings()
    client = AiClient(settings)
    try:
        reply = client.complete([AiMessage(role="user", content="只回复：连接正常")])
        print({"success": True, "reply": reply, "metadata": client.last_completion_metadata})
    except httpx.HTTPStatusError as error:
        print({"success": False, "status": error.response.status_code, "response": error.response.text[:2000]})


if __name__ == "__main__":
    main()
