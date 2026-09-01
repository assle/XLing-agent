# Xling

Xling 是面向一般用户的非诊断性心理健康支持系统，帮助用户梳理困扰、制定短期行动，并在发现较高安全风险时转交人工审核。

它不提供疾病诊断、药物建议、独立治疗或紧急救援。正式部署前，部署方需要根据当地要求完成专业、法律和安全审查，并配置适用的专业支持与紧急资源。

## 快速启动

```bash
docker compose up -d --build
```

启动后访问 `http://127.0.0.1:8080`。如需在不连接模型的情况下体验完整流程，可设置 `AI_PROVIDER=mock`。

## 本地开发

需要 Python 3.12：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## 文档

- [领域说明与统一术语](CONTEXT.md)
- [部署指南](docs/deployment-guide.md)
- [重要设计决策](docs/adr/)
