"""Single-process web application and background assignment runner."""

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from threading import Event, Thread
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .runner import AssignmentError, OpenAIModel, run_assignment
from .storage import Store

APP_DIR = Path(__file__).parent
MAX_FILES = 8
MAX_FILE_BYTES = 128 * 1024
MAX_REQUEST_CHARS = 8_000
MAX_FORM_BYTES = (MAX_FILES + 1) * MAX_FILE_BYTES
logger = logging.getLogger(__name__)


def work_pending(store, model, wake, stop):
    while not stop.is_set():
        wake.clear()
        task_id = store.claim()
        if task_id is None:
            wake.wait(timeout=1)
            continue

        try:
            response = run_assignment(
                model,
                store.get_task(task_id)["request"],
                store.read_uploads(task_id),
                lambda name, content: store.stage(task_id, name, content),
                lambda message: store.progress(task_id, message),
            )
            store.complete(task_id, response)
        except AssignmentError as exc:
            store.fail(task_id, str(exc))
        except Exception:
            logger.exception("Assignment %s failed", task_id)
            store.fail(task_id, "The assignment encountered an application error.")


def create_app(data_dir: Path | None = None, model=None) -> FastAPI:
    store = Store(Path(data_dir or os.environ.get("WORK_AGENT_DATA_DIR", "data")))
    if model is None and os.environ.get("OPENAI_API_KEY"):
        model = OpenAIModel(
            os.environ["OPENAI_API_KEY"],
            os.environ.get("OPENAI_MODEL") or "gpt-6.2-sol",
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
        )

    wake = Event()
    stop = Event()

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        if model is None:
            yield
            return

        stop.clear()
        worker = Thread(target=work_pending, args=(store, model, wake, stop), daemon=True)
        worker.start()
        try:
            yield
        finally:
            stop.set()
            wake.set()
            await asyncio.to_thread(worker.join, 5)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"])
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=APP_DIR / "templates")

    def render_index(request, *, error=None, draft="", status_code=200):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"tasks": store.list_tasks(), "enabled": model is not None, "error": error, "draft": draft},
            status_code=status_code,
        )

    @app.get("/")
    def index(request: Request):
        return render_index(request)

    @app.post("/tasks")
    async def submit(request: Request):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "Submit assignments from the Work Agent page.")
        if model is None:
            return render_index(request, error="Configure a model before submitting an assignment.", status_code=503)

        length = request.headers.get("content-length", "")
        if not length.isdecimal() or int(length) > MAX_FORM_BYTES:
            return render_index(request, error="The upload is too large or has no content length.", status_code=413)

        try:
            async with request.form(
                max_files=MAX_FILES, max_fields=1, max_part_size=MAX_REQUEST_CHARS * 4
            ) as form:
                draft = form.get("request", "")
                if not isinstance(draft, str) or not 0 < len(draft.strip()) <= MAX_REQUEST_CHARS:
                    raise ValueError(f"Enter a request of up to {MAX_REQUEST_CHARS:,} characters.")
                uploads = {}
                for file in form.getlist("files"):
                    if not isinstance(file, UploadFile):
                        raise ValueError("Choose files to upload.")
                    name = file.filename
                    if not name:
                        continue
                    if (
                        len(name) > 120
                        or name in {".", ".."}
                        or any(char in name for char in "/\\")
                        or not name.isprintable()
                    ):
                        raise ValueError("Use a plain filename of up to 120 characters.")
                    if name in uploads:
                        raise ValueError("Each upload must have a different filename.")
                    data = await file.read(MAX_FILE_BYTES + 1)
                    if len(data) > MAX_FILE_BYTES:
                        raise ValueError("Each upload must be 128 KiB or smaller.")
                    try:
                        data.decode("utf-8")
                    except UnicodeDecodeError:
                        raise ValueError("Upload UTF-8 text files, such as CSV or Markdown.") from None
                    if b"\x00" in data:
                        raise ValueError("Upload UTF-8 text files, such as CSV or Markdown.")
                    uploads[name] = data
        except HTTPException as exc:
            return render_index(request, error=exc.detail, status_code=400)
        except ValueError as exc:
            return render_index(request, error=str(exc), status_code=400)

        task_id = await asyncio.to_thread(store.submit, draft.strip(), uploads)
        wake.set()
        return RedirectResponse(request.url_for("assignment", task_id=task_id), status_code=303)

    @app.get("/tasks/{task_id}")
    def assignment(request: Request, task_id: str):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "Assignment not found.")
        return templates.TemplateResponse(request=request, name="task.html", context={"task": task})

    @app.get("/tasks/{task_id}/artifacts/{name}")
    def artifact(request: Request, task_id: str, name: str, download: bool = False):
        content = store.read_artifact(task_id, name)
        if content is None:
            raise HTTPException(404, "Saved file not found.")
        if download:
            media_type = "text/csv" if name.lower().endswith(".csv") else "text/markdown"
            return Response(
                content,
                media_type=media_type,
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}"},
            )
        return templates.TemplateResponse(
            request=request,
            name="artifact.html",
            context={"task_id": task_id, "name": name, "content": content.decode("utf-8")},
        )

    return app


app = create_app()
