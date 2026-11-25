"""Single-process web application and background assignment runner."""

import asyncio
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime
import logging
import os
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Path as RoutePath, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .note_pages import install_note_pages
from .artifact_preview import is_upload_name, markdown_preview
from .connected_sources import ConnectedSources
from .evidence_changes import AssignmentEvidenceChanges
from .runner import AssignmentError, NeedsUserInput, OpenAIModel, run_assignment
from .scheduling import PACIFIC, WEEKDAYS, format_pacific, parse_schedule_time, utc_now
from .storage import (
    InactiveTurn, MAX_ARTIFACT_BYTES, MAX_FILES, MAX_SCHEDULE_REQUEST_CHARS, METHOD_FIELD_LIMITS,
    RevisionConflict, Store,
)

APP_DIR = Path(__file__).parent
MAX_FILE_BYTES = 128 * 1024
MAX_REQUEST_CHARS = 8_000
MAX_FORM_BYTES = (MAX_FILES + 1) * MAX_FILE_BYTES
MAX_RECORD_NUMBER = 2**63 - 1
MethodId = Annotated[int, RoutePath(ge=1, le=MAX_RECORD_NUMBER)]
ScheduleId = Annotated[int, RoutePath(ge=1, le=MAX_RECORD_NUMBER)]
logger = logging.getLogger(__name__)


def work_pending(store, model, wake, stop, turn_lock, clock, sources=None):
    while not stop.is_set():
        wake.clear()
        try:
            store.enqueue_due_schedule(now=clock())
            task_id = store.claim()
        except sqlite3.OperationalError as exc:
            # SQLite's low byte is the primary result code for extended errors too.
            if getattr(exc, "sqlite_errorcode", 0) & 0xff != sqlite3.SQLITE_BUSY:
                raise
            logger.warning("Database busy while polling for work. Retrying.")
            task_id = None
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
            uploads = store.read_uploads(task_id)
            prior_requests = [m["content"] for m in run["prior_messages"] if m["role"] == "user"]
            method_index = store.relevant_methods(
                run["request"], [*prior_requests[-1:], *prior_requests[:1], *(Path(name).stem for name in uploads)]
            )
            try:
                with (sources.open() if sources else nullcontext()) as catalog:
                    response = run_assignment(
                        model,
                        run["request"],
                        uploads,
                        lambda name, content: store.stage(task_id, name, content, turn_id=turn_id),
                        lambda message: store.progress(task_id, message, turn_id=turn_id),
                        lambda step: store.record_step(task_id, step, turn_id=turn_id),
                        prior_messages=run["prior_messages"],
                        attached_files=run["attached_files"] if run["kind"] != "initial" else None,
                        saved_artifacts=run["artifacts"],
                        read_saved_artifact=lambda name: store.read_run_artifact(task_id, turn_id, name),
                        prior_artifacts=run["prior_artifacts"],
                        find_evidence_uses=lambda source, offset=0: store.find_evidence_uses(
                            task_id, source, offset
                        ),
                        read_prior_artifact=lambda reference_id: store.read_prior_artifact(task_id, reference_id),
                        search_saved_work=lambda query, kind="any", paraphrases=(): store.search_saved_work(
                            task_id, query, kind, paraphrases
                        ),
                        pin_saved_work=lambda source_task_id, name, revision: store.pin_saved_work(
                            task_id, source_task_id, name, revision
                        ),
                        tool_scope=tool_scope,
                        project_note_store=store,
                        task_id=task_id,
                        stage_project_note=lambda **fields: store.stage_project_note(task_id, turn_id, **fields),
                        connected_catalog=catalog,
                        change_tracker=AssignmentEvidenceChanges(store, task_id, turn_id),
                        method_index=method_index,
                        read_method=store.get_method,
                        stage_method=lambda title, scope, procedure, evidence: store.stage_method(
                            task_id, title, scope, procedure, evidence, turn_id=turn_id,
                        ),
                    )
            except NeedsUserInput as pause:
                store.wait_for_input(task_id, pause.question, turn_id=turn_id)
            else:
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


def assignment_request(value, *, max_chars=MAX_REQUEST_CHARS) -> str:
    if not isinstance(value, str) or not 0 < len(value.strip()) <= max_chars:
        raise ValueError(f"Enter a request of up to {max_chars:,} characters.")
    return value.strip()


async def read_form_uploads(form: FormData) -> dict[str, bytes]:
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
    return uploads


def form_turn_id(form: FormData) -> int:
    expected = form.get("turn_id", "")
    if not isinstance(expected, str) or not expected.isdecimal() or not 0 < len(expected) <= 19:
        raise ValueError("Open the current turn before changing it.")
    expected_turn_id = int(expected)
    if not 1 <= expected_turn_id <= MAX_RECORD_NUMBER:
        raise ValueError("Open the current turn before changing it.")
    return expected_turn_id


def create_app(data_dir: Path | None = None, model=None, *, clock=utc_now) -> FastAPI:
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
        config_path = os.environ.get("WORK_AGENT_MCP_CONFIG")
        sources = ConnectedSources.from_file(Path(config_path)) if config_path else None
        worker = Thread(target=work_pending, args=(store, model, wake, stop, turn_lock, clock, sources), daemon=True)
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
    install_note_pages(app, store, templates, check_form_origin, check_form_size)

    def render_index(request, *, prior_revision=None, error=None, draft="", status_code=200):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "tasks": store.list_tasks(), "enabled": model is not None, "error": error, "draft": draft,
                "prior_revision": prior_revision,
            },
            status_code=status_code,
        )

    @app.get("/")
    def index(request: Request):
        return render_index(request)

    def render_responsibility(request, *, values=None, error=None, status_code=200):
        return templates.TemplateResponse(
            request=request,
            name="responsibility.html",
            context={
                "schedule": store.get_schedule(),
                "weekdays": WEEKDAYS,
                "values": values if values is not None else {"request": "", "weekday": "4", "local_time": "09:00"},
                "max_request_chars": MAX_SCHEDULE_REQUEST_CHARS,
                "format_pacific": format_pacific,
                "iso_pacific": lambda instant: datetime.fromtimestamp(instant, PACIFIC).isoformat(),
                "enabled": model is not None,
                "error": error,
            },
            status_code=status_code,
        )

    @app.get("/responsibility")
    def responsibility(request: Request):
        return render_responsibility(request)

    @app.post("/responsibility")
    async def save_responsibility(request: Request):
        check_form_origin(request)
        check_form_size(request, MAX_SCHEDULE_REQUEST_CHARS * 12 + 1024)
        values = {}
        try:
            async with request.form(max_files=0, max_fields=3, max_part_size=MAX_SCHEDULE_REQUEST_CHARS * 4) as form:
                for field in ("request", "weekday", "local_time"):
                    value = form.get(field, "")
                    if not isinstance(value, str):
                        raise ValueError("Enter text for each responsibility field.")
                    values[field] = value
            prompt = assignment_request(values["request"], max_chars=MAX_SCHEDULE_REQUEST_CHARS)
            if "\x00" in prompt:
                raise ValueError("Enter a plain-text request.")
            if len(values["weekday"]) != 1 or values["weekday"] not in "0123456":
                raise ValueError("Choose a weekday.")
            parse_schedule_time(values["local_time"])
        except (HTTPException, ValueError) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return render_responsibility(request, values=values, error=message, status_code=400)
        try:
            await asyncio.to_thread(
                store.create_schedule, prompt, int(values["weekday"]), values["local_time"], now=clock(),
            )
        except ValueError as exc:
            return render_responsibility(request, values=values, error=str(exc), status_code=409)
        wake.set()
        return RedirectResponse(request.url_for("responsibility"), status_code=303)

    @app.post("/responsibility/{schedule_id}/run-now")
    async def run_responsibility_now(request: Request, schedule_id: ScheduleId):
        check_form_origin(request)
        check_form_size(request, 1024)
        if model is None:
            return render_responsibility(request, error="Configure a model before running the responsibility.", status_code=503)
        try:
            task_id = await asyncio.to_thread(store.enqueue_schedule_now, schedule_id, now=clock())
        except KeyError:
            raise HTTPException(404, "Weekly responsibility not found.") from None
        except ValueError as exc:
            return render_responsibility(request, error=str(exc), status_code=409)
        wake.set()
        return RedirectResponse(request.url_for("assignment", task_id=task_id), status_code=303)

    @app.post("/responsibility/{schedule_id}/stop")
    async def stop_responsibility(request: Request, schedule_id: ScheduleId):
        check_form_origin(request)
        check_form_size(request, 1024)
        try:
            await asyncio.to_thread(store.stop_schedule, schedule_id, now=clock())
        except KeyError:
            raise HTTPException(404, "Weekly responsibility not found.") from None
        except ValueError as exc:
            return render_responsibility(request, error=str(exc), status_code=409)
        wake.set()
        return RedirectResponse(request.url_for("responsibility"), status_code=303)

    async def submit_assignment(request: Request, prior_revision=None):
        check_form_origin(request)
        if model is None:
            return render_index(request, prior_revision=prior_revision,
                                error="Configure a model before submitting an assignment.", status_code=503)

        try:
            check_form_size(request, MAX_FORM_BYTES)
        except HTTPException:
            return render_index(request, prior_revision=prior_revision,
                                error="The upload is too large or has no content length.", status_code=413)

        draft = ""
        try:
            async with request.form(
                max_files=MAX_FILES, max_fields=1, max_part_size=MAX_REQUEST_CHARS * 4
            ) as form:
                draft = form.get("request", "")
                prompt = assignment_request(draft)
                uploads = await read_form_uploads(form)
            task_id = await asyncio.to_thread(store.submit, prompt, uploads, prior_revision=prior_revision)
        except HTTPException as exc:
            return render_index(request, prior_revision=prior_revision, draft=draft, error=exc.detail, status_code=400)
        except ValueError as exc:
            return render_index(request, prior_revision=prior_revision, draft=draft, error=str(exc), status_code=400)

        wake.set()
        return RedirectResponse(request.url_for("assignment", task_id=task_id), status_code=303)

    @app.post("/tasks")
    async def submit(request: Request):
        return await submit_assignment(request)

    reuse_path = "/tasks/{task_id}/artifacts/{name}/revisions/{revision}/new-assignment"

    @app.get(reuse_path)
    def new_from_revision(request: Request, task_id: str, name: str,
                          revision: Annotated[int, RoutePath(ge=1, le=MAX_RECORD_NUMBER)]):
        if store.artifact_revision(task_id, name, revision) is None:
            raise HTTPException(404, "Saved revision not found.")
        return render_index(request, prior_revision=(task_id, name, revision))

    @app.post(reuse_path)
    async def submit_from_revision(request: Request, task_id: str, name: str,
                                   revision: Annotated[int, RoutePath(ge=1, le=MAX_RECORD_NUMBER)]):
        return await submit_assignment(request, (task_id, name, revision))

    reuse_upload_path = "/tasks/{task_id}/uploads/{name}/new-assignment"

    @app.get(reuse_upload_path)
    def new_from_upload(request: Request, task_id: str, name: str):
        if store.read_upload(task_id, name) is None:
            raise HTTPException(404, "Uploaded file not found.")
        return render_index(request, prior_revision=(task_id, name, None))

    @app.post(reuse_upload_path)
    async def submit_from_upload(request: Request, task_id: str, name: str):
        return await submit_assignment(request, (task_id, name, None))

    def render_assignment(
        request, task_id, *, draft="", error=None, reply_to=None, correction_draft="", control_error=None, status_code=200,
    ):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "Assignment not found.")
        waiting_turn_id = task["turns"][-1]["id"] if task["status"] == "waiting" else None
        return templates.TemplateResponse(
            request=request,
            name="task.html",
            context={
                "task": task,
                "follow_up_enabled": model is not None,
                "draft": draft if reply_to == waiting_turn_id else "",
                "form_error": error,
                "correction_draft": correction_draft,
                "control_error": control_error,
            },
            status_code=status_code,
        )

    @app.get("/tasks/{task_id}")
    def assignment(request: Request, task_id: str):
        return render_assignment(request, task_id)

    async def queue_reply(request, task_id, *, answering):
        check_form_origin(request)
        check_form_size(request, MAX_FORM_BYTES)
        if model is None:
            return render_assignment(request, task_id, error="Configure a model to continue an assignment.", status_code=503)

        draft = ""
        expected = None
        try:
            async with request.form(
                max_files=MAX_FILES, max_fields=2 if answering else 1, max_part_size=MAX_REQUEST_CHARS * 4,
            ) as form:
                if answering:
                    expected = form_turn_id(form)
                value = form.get("request", "")
                if isinstance(value, str):
                    draft = value
                uploads = await read_form_uploads(form)
                if answering and isinstance(value, str) and not value.strip():
                    if not uploads:
                        raise ValueError("Enter an answer or attach the requested file.")
                    prompt = "I attached the requested source files."
                else:
                    prompt = assignment_request(value)
        except (HTTPException, ValueError) as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return render_assignment(request, task_id, draft=draft, reply_to=expected, error=error, status_code=400)
        try:
            if answering:
                await asyncio.to_thread(store.answer, task_id, expected, prompt, uploads)
            else:
                await asyncio.to_thread(store.follow_up, task_id, prompt, uploads)
        except KeyError:
            raise HTTPException(404, "Assignment not found.") from None
        except (ValueError, InactiveTurn) as exc:
            return render_assignment(request, task_id, draft=draft, reply_to=expected, error=str(exc), status_code=409)
        wake.set()
        return RedirectResponse(request.url_for("assignment", task_id=task_id), status_code=303)

    @app.post("/tasks/{task_id}/follow-up")
    async def follow_up(request: Request, task_id: str):
        return await queue_reply(request, task_id, answering=False)

    @app.post("/tasks/{task_id}/answer")
    async def answer(request: Request, task_id: str):
        return await queue_reply(request, task_id, answering=True)

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
                expected_turn_id = form_turn_id(form)
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

    @app.get("/methods")
    def methods(request: Request):
        return templates.TemplateResponse(
            request=request, name="methods.html", context={"methods": store.list_methods()},
        )

    def find_method(method_id):
        method = store.get_method(method_id)
        if method is None:
            raise HTTPException(404, "Saved method not found.")
        return method

    def check_method_source(task_id):
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(404, "Assignment not found.")
        if task["status"] != "completed":
            raise HTTPException(409, "Save methods from completed assignments.")

    def render_method_form(
        request, source_task_id, *, method_id=None, values=None, error=None, status_code=200,
        expected_revision=None, current=None, removed=False,
    ):
        return templates.TemplateResponse(
            request=request,
            name="method_form.html",
            context={
                "source_task_id": source_task_id,
                "method_id": method_id,
                "values": values or {},
                "limits": METHOD_FIELD_LIMITS,
                "error": error,
                "expected_revision": expected_revision,
                "current": current,
                "removed": removed,
            },
            status_code=status_code,
        )

    def method_revision(form):
        revision = form.get("expected_revision", "")
        if not isinstance(revision, str) or not revision.isascii() or not revision.isdigit() or len(revision) > 19:
            raise HTTPException(400, "Open the current method before changing it.")
        number = int(revision)
        if not 1 <= number <= MAX_RECORD_NUMBER:
            raise HTTPException(400, "Invalid method revision.")
        return number

    async def submit_method(request, source_task_id=None, *, method_id=None):
        check_form_origin(request)
        check_form_size(request, sum(METHOD_FIELD_LIMITS.values()) * 12 + 1024)
        values = {}
        expected_revision = None
        try:
            async with request.form(
                max_files=0, max_fields=len(METHOD_FIELD_LIMITS) + 1, max_part_size=max(METHOD_FIELD_LIMITS.values()) * 4,
            ) as form:
                for field in METHOD_FIELD_LIMITS:
                    value = form.get(field, "")
                    if not isinstance(value, str):
                        raise ValueError("Enter text for each method field.")
                    values[field] = value
                if method_id is not None:
                    expected_revision = method_revision(form)
            if method_id is None:
                method_id = await asyncio.to_thread(store.save_method, source_task_id, **values)
            else:
                await asyncio.to_thread(store.update_method, method_id, expected_revision=expected_revision, **values)
        except (RevisionConflict, KeyError):
            if method_id is None:
                raise HTTPException(404, "Assignment not found.") from None
            current = store.get_method(method_id)
            if current is None:
                return render_method_form(
                    request, source_task_id, method_id=method_id, values=values, removed=True, status_code=404,
                    error="This method was removed. You can copy your draft below.",
                )
            return render_method_form(
                request, source_task_id, method_id=method_id, values=values, current=current,
                expected_revision=current["revision"], status_code=409,
                error="Review the latest saved method below and reconcile your draft before saving.",
            )
        except (HTTPException, ValueError) as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return render_method_form(
                request, source_task_id, method_id=method_id, values=values, expected_revision=expected_revision,
                error=error, status_code=400,
            )
        return RedirectResponse(request.url_for("method_detail", method_id=method_id), status_code=303)

    @app.get("/tasks/{task_id}/save-method")
    def method_from_task(request: Request, task_id: str):
        check_method_source(task_id)
        return render_method_form(request, task_id)

    @app.post("/tasks/{task_id}/save-method")
    async def save_method(request: Request, task_id: str):
        check_method_source(task_id)
        return await submit_method(request, task_id)

    @app.get("/methods/{method_id}")
    def method_detail(request: Request, method_id: MethodId, edit: bool = False):
        method = find_method(method_id)
        if edit:
            return render_method_form(
                request, method["source_task_id"], method_id=method_id, values=method, expected_revision=method["revision"],
            )
        return templates.TemplateResponse(request=request, name="method.html", context={"method": method})

    @app.post("/methods/{method_id}")
    async def update_method(request: Request, method_id: MethodId):
        return await submit_method(request, method_id=method_id)

    @app.post("/methods/{method_id}/remove")
    async def remove_method(request: Request, method_id: MethodId):
        check_form_origin(request)
        check_form_size(request, 1024)
        async with request.form(max_files=0, max_fields=1, max_part_size=64) as form:
            expected_revision = method_revision(form)
        try:
            await asyncio.to_thread(store.remove_method, method_id, expected_revision=expected_revision)
        except RevisionConflict:
            return templates.TemplateResponse(
                request=request, name="method.html",
                context={"method": find_method(method_id), "error": "Review the updated method before removing it."},
                status_code=409,
            )
        except KeyError:
            raise HTTPException(404, "Saved method not found.") from None
        return RedirectResponse(request.url_for("methods"), status_code=303)

    @app.get("/tasks/{task_id}/prior-artifacts/{reference_id}")
    def prior_artifact(request: Request, task_id: str, reference_id: Annotated[int, RoutePath(ge=1, le=MAX_RECORD_NUMBER)]):
        prior = store.prior_artifact(task_id, reference_id)
        if prior is None:
            raise HTTPException(404, "Earlier file not found.")
        if prior.get("kind") == "upload":
            url = request.url_for("uploaded_source", task_id=prior["source_task_id"], name=quote(prior["name"], safe=""))
            return RedirectResponse(url, status_code=303)
        url = request.url_for("artifact", task_id=prior["source_task_id"], name=prior["name"])
        # Relative citations in the earlier report resolve within its original assignment.
        return RedirectResponse(url.include_query_params(revision=prior["revision"]), status_code=303)

    @app.get("/tasks/{task_id}/evidence/{source_ref}")
    def source_evidence(request: Request, task_id: str, source_ref: str):
        receipt = store.source_evidence(task_id, source_ref)
        if receipt is None:
            raise HTTPException(404, "Source evidence not found.")
        return templates.TemplateResponse(
            request=request, name="source_evidence.html", context={"receipt": receipt, "task_id": task_id},
        )

    @app.get("/tasks/{task_id}/evidence-uses/{kind}/{name}")
    def evidence_uses(request: Request, task_id: str, kind: str, name: str,
                      revision: Annotated[int | None, Query(ge=1, le=MAX_RECORD_NUMBER)] = None,
                      offset: Annotated[int, Query(ge=0, le=MAX_RECORD_NUMBER - 100)] = 0):
        if kind == "upload" and revision is None and store.read_upload(task_id, name) is not None:
            source = request.url_for("uploaded_source", task_id=task_id, name=quote(name, safe=""))
        elif kind == "artifact" and revision and store.artifact_revision(task_id, name, revision) is not None:
            source = request.url_for("artifact", task_id=task_id, name=name).include_query_params(revision=revision)
        else:
            raise HTTPException(404, "Evidence source not found.")
        return templates.TemplateResponse(
            request=request, name="evidence_uses.html",
            context={"name": name, "revision": revision, "source": source,
                     **store.evidence_uses(task_id, name, revision, offset)},
        )

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
