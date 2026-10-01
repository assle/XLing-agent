from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core import diagnostics
from app.core.bootstrap import create_schema, seed_data
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.services.data_deletion import DataDeletionService
from app.services.review import get_review_timeout_worker
from app.services.tool_queue import get_tool_queue_worker


def create_app() -> FastAPI:
    """创建网页服务，注册接口、静态网页及启动和关闭处理。

    返回应用对象；数据初始化和后台任务启动会在服务启动事件中执行，而非仅创建对象时执行。
    """
    app = FastAPI(title="Xling", version="0.1.0")

    @app.on_event("startup")
    def startup() -> None:
        """服务启动时准备数据库和知识资料，恢复待清理状态并启动后台工作线程。

        数据库会话在初始化结束后关闭；工具任务和审核超时工作器保存到应用状态，供关闭时取用。
        """
        diagnostics.configure(get_settings())
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
        """服务关闭时停止已经创建的工具任务与审核超时工作器。

        先检查应用状态中是否存在对象，允许启动未完整完成的情况下执行清理。
        """
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
