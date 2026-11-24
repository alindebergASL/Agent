"""Single-process web application and background assignment runner."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .artifact_preview import is_upload_name, markdown_preview
from .runner import AssignmentError, OpenAIModel, run_assignment
from .storage import InactiveTurn, MAX_ARTIFACT_BYTES, RevisionConflict, Store

APP_DIR = Path(__file__).parent
MAX_FILES = 8
MAX_FILE_BYTES = 128 * 1024
MAX_REQUEST_CHARS = 8_000
MAX_FORM_BYTES = (MAX_FILES + 1) * MAX_FILE_BYTES
MAX_RECORD_NUMBER = 2**63 - 1
logger = logging.getLogger(__name__)


def work_pending(store, model, wake, stop, turn_lock):
    while not stop.is_set():
        wake.clear()
        task_id = store.claim()
        if task_id is None:
            wake.wait(timeout=1)
            continue

        run = store.get_run(task_id)
        if run is None:
            continue
        turn_id = run["turn_id"]

        @contextmanager
        def tool_scope():
            with turn_lock:
                store.require_running(task_id, turn_id)
                yield

        try:
            response = run_assignment(
                model,
                run["request"],
                store.read_uploads(task_id),
                lambda name, content: store.stage(task_id, name, content, turn_id=turn_id),
                lambda message: store.progress(task_id, message, turn_id=turn_id),
                lambda step: store.record_step(task_id, step, turn_id=turn_id),
                prior_messages=run["prior_messages"],
                saved_artifacts=run["artifacts"],
                read_saved_artifact=lambda name: store.read_run_artifact(task_id, turn_id, name),
                tool_scope=tool_scope,
            )
            store.complete(task_id, response, turn_id=turn_id)
        except InactiveTurn:
            continue
        except (AssignmentError, RevisionConflict) as exc:
            store.fail(task_id, str(exc), turn_id=turn_id)
        except Exception:
            logger.exception("Assignment %s failed", task_id)
            store.fail(task_id, "The assignment encountered an application error.", turn_id=turn_id)


def check_form_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Submit changes from the Work Agent page.")


def check_form_size(request: Request, limit: int) -> None:
    length = request.headers.get("content-length", "")
    if not length.isascii() or not length.isdecimal() or len(length) > len(str(limit)) or int(length) > limit:
        raise HTTPException(413, "The form is too large or has no content length.")


def assignment_request(value) -> str:
    if not isinstance(value, str) or not 0 < len(value.strip()) <= MAX_REQUEST_CHARS:
        raise ValueError(f"Enter a request of up to {MAX_REQUEST_CHARS:,} characters.")
    return value.strip()


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
    turn_lock = Lock()

    @asynccontextmanager
    async def lifespan(app):
        store.initialize()
        if model is None:
            yield
            return

        stop.clear()
        worker = Thread(target=work_pending, args=(store, model, wake, stop, turn_lock), daemon=True)
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
        check_form_origin(request)
        if model is None:
            return render_index(request, error="Configure a model before submitting an assignment.", status_code=503)

        try:
            check_form_size(request, MAX_FORM_BYTES)
        except HTTPException:
            return render_index(request, error="The upload is too large or has no content length.", status_code=413)

        try:
            async with request.form(
                max_files=MAX_FILES, max_fields=1, max_part_size=MAX_REQUEST_CHARS * 4
            ) as form:
                draft = form.get("request", "")
                prompt = assignment_request(draft)
                uploads = {}
                for file in form.getlist("files"):
                    if not isinstance(file, UploadFile):
                        raise ValueError("Choose files to upload.")
                    name = file.filename
                    if not name:
                        continue
                    if not is_upload_name(name):
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

        task_id = await asyncio.to_thread(store.submit, prompt, uploads)
        wake.set()
        return RedirectResponse(request.url_for("assignment", task_id=task_id), status_code=303)

    def render_assignment(
        request, task_id, *, draft="", error=None, correction_draft="", control_error=None, status_code=200,
    ):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "Assignment not found.")
        return templates.TemplateResponse(
            request=request,
            name="task.html",
            context={
                "task": task,
                "follow_up_enabled": model is not None,
                "draft": draft,
                "form_error": error,
                "correction_draft": correction_draft,
                "control_error": control_error,
            },
            status_code=status_code,
        )

    @app.get("/tasks/{task_id}")
    def assignment(request: Request, task_id: str):
        return render_assignment(request, task_id)

    @app.post("/tasks/{task_id}/follow-up")
    async def follow_up(request: Request, task_id: str):
        check_form_origin(request)
        check_form_size(request, MAX_REQUEST_CHARS * 12 + 1024)
        if model is None:
            return render_assignment(request, task_id, error="Configure a model to continue an assignment.", status_code=503)

        draft = ""
        try:
            async with request.form(max_files=0, max_fields=1, max_part_size=MAX_REQUEST_CHARS * 4) as form:
                draft = form.get("request", "")
                prompt = assignment_request(draft)
        except (HTTPException, ValueError) as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return render_assignment(request, task_id, draft=draft, error=error, status_code=400)
        try:
            await asyncio.to_thread(store.follow_up, task_id, prompt)
        except KeyError:
            raise HTTPException(404, "Assignment not found.") from None
        except ValueError as exc:
            return render_assignment(request, task_id, draft=draft, error=str(exc), status_code=409)
        wake.set()
        return RedirectResponse(request.url_for("assignment", task_id=task_id), status_code=303)

    def apply_turn_control(action, *args):
        with turn_lock:
            action(*args)

    async def turn_control(request, task_id, action, *, correction=False, requires_model=False):
        check_form_origin(request)
        check_form_size(request, MAX_REQUEST_CHARS * 12 + 1024 if correction else 1024)
        if requires_model and model is None:
            return render_assignment(
                request, task_id, control_error="Configure a model to continue this assignment.", status_code=503,
            )

        draft = ""
        try:
            async with request.form(
                max_files=0, max_fields=2 if correction else 1, max_part_size=MAX_REQUEST_CHARS * 4,
            ) as form:
                expected = form.get("turn_id", "")
                if not isinstance(expected, str) or not expected.isdecimal() or not 0 < len(expected) <= 19:
                    raise ValueError("Open the current turn before changing it.")
                expected_turn_id = int(expected)
                if not 1 <= expected_turn_id <= MAX_RECORD_NUMBER:
                    raise ValueError("Open the current turn before changing it.")
                args = (task_id, expected_turn_id)
                if correction:
                    value = form.get("request", "")
                    if isinstance(value, str):
                        draft = value
                    args += (assignment_request(value),)
        except (HTTPException, ValueError) as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return render_assignment(request, task_id, correction_draft=draft, control_error=error, status_code=400)

        try:
            await asyncio.to_thread(apply_turn_control, action, *args)
        except KeyError:
            raise HTTPException(404, "Assignment not found.") from None
        except (InactiveTurn, ValueError) as exc:
            return render_assignment(request, task_id, correction_draft=draft, control_error=str(exc), status_code=409)
        wake.set()
        return RedirectResponse(request.url_for("assignment", task_id=task_id), status_code=303)

    @app.post("/tasks/{task_id}/stop")
    async def stop_turn(request: Request, task_id: str):
        return await turn_control(request, task_id, store.stop_turn)

    @app.post("/tasks/{task_id}/correct")
    async def correct_turn(request: Request, task_id: str):
        return await turn_control(request, task_id, store.correct_turn, correction=True, requires_model=True)

    @app.post("/tasks/{task_id}/continue")
    async def continue_turn(request: Request, task_id: str):
        return await turn_control(request, task_id, store.continue_turn, requires_model=True)

    @app.get("/tasks/{task_id}/history")
    def run_history(task_id: str):
        history = store.run_history(task_id)
        if history is None:
            raise HTTPException(404, "Assignment not found.")
        return JSONResponse(history, headers={"Cache-Control": "no-store"})

    @app.get("/tasks/{task_id}/uploads/{name}")
    def uploaded_source(request: Request, task_id: str, name: str, download: bool = False):
        content = store.read_upload(task_id, name)
        if content is None:
            raise HTTPException(404, "Uploaded file not found.")
        if download:
            return Response(
                content,
                media_type="text/plain",
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}"},
            )
        return templates.TemplateResponse(
            request=request,
            name="upload.html",
            context={"task_id": task_id, "name": name, "lines": content.decode("utf-8").splitlines()},
        )

    def render_artifact(request, task_id, name, saved, *, historical=False, draft=None, error=None, status_code=200):
        text = saved["content"].decode("utf-8")
        return templates.TemplateResponse(
            request=request,
            name="artifact.html",
            context={
                "task_id": task_id,
                "name": name,
                "content": text,
                "markdown_html": markdown_preview.render(text) if name.endswith(".md") else None,
                "saved": saved,
                "historical": historical,
                "revisions": reversed(store.artifact_revisions(task_id, name)),
                "draft": text if draft is None else draft,
                "form_error": error,
            },
            status_code=status_code,
        )

    @app.get("/tasks/{task_id}/artifacts/{name}")
    def artifact(
        request: Request,
        task_id: str,
        name: str,
        download: bool = False,
        revision: Annotated[int | None, Query(ge=1, le=MAX_RECORD_NUMBER)] = None,
    ):
        saved = store.artifact_revision(task_id, name, revision)
        if saved is None:
            raise HTTPException(404, "Saved file not found.")
        if download:
            media_type = "text/markdown" if name.endswith(".md") else "text/csv"
            return Response(
                saved["content"],
                media_type=media_type,
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}"},
            )
        return render_artifact(request, task_id, name, saved, historical=revision is not None)

    @app.post("/tasks/{task_id}/artifacts/{name}")
    async def edit_artifact(request: Request, task_id: str, name: str):
        check_form_origin(request)
        check_form_size(request, MAX_ARTIFACT_BYTES * 3 + 1024)
        saved = store.artifact_revision(task_id, name)
        if not name.endswith(".md") or saved is None:
            raise HTTPException(404, "Editable Markdown file not found.")

        draft = None
        try:
            async with request.form(max_files=0, max_fields=2, max_part_size=MAX_ARTIFACT_BYTES) as form:
                content = form.get("content")
                expected = form.get("expected_revision", "")
                if not isinstance(content, str):
                    raise ValueError("Enter Markdown content.")
                draft = content
                if not isinstance(expected, str) or not expected.isdecimal() or not 0 < len(expected) <= 19:
                    raise ValueError("Open the current revision before saving an edit.")
                expected_revision = int(expected)
                if not 1 <= expected_revision <= MAX_RECORD_NUMBER:
                    raise ValueError("Open the current revision before saving an edit.")
            await asyncio.to_thread(store.save_markdown, task_id, name, draft, expected_revision)
        except RevisionConflict:
            saved = store.artifact_revision(task_id, name)
            return render_artifact(
                request, task_id, name, saved, draft=draft,
                error="A newer revision was saved. Review it above before saving your draft as a new revision.",
                status_code=409,
            )
        except KeyError:
            raise HTTPException(404, "Editable Markdown file not found.") from None
        except (HTTPException, ValueError) as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return render_artifact(request, task_id, name, saved, draft=draft, error=error, status_code=400)
        return RedirectResponse(request.url_for("artifact", task_id=task_id, name=name), status_code=303)

    return app


app = create_app()
