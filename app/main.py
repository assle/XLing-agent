from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.data_deletion import DataDeletionService
from app.services.review import get_review_timeout_worker
from app.services.tool_queue import get_tool_queue_worker


def create_app() -> FastAPI:
    app = FastAPI(title="Xling", version="0.1.0")

    @app.on_event("startup")
    def startup() -> None:
        create_schema()
        db = SessionLocal()
        try:
            seed_data(db)
            DataDeletionService(db).retry_pending_checkpoint_deletions()
        finally:
            db.close()
        worker = get_tool_queue_worker(get_settings())
        worker.start()
        app.state.tool_queue_worker = worker
        review_worker = get_review_timeout_worker(get_settings())
        review_worker.start()
        app.state.review_timeout_worker = review_worker

    @app.on_event("shutdown")
    def shutdown() -> None:
        worker = getattr(app.state, "tool_queue_worker", None)
        if worker is not None:
            worker.stop()
        review_worker = getattr(app.state, "review_timeout_worker", None)
        if review_worker is not None:
            review_worker.stop()

    app.include_router(router)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
    return app


app = create_app()
