"""Web forms and history for independent recurring watches."""

import asyncio
import re
from typing import Annotated
from urllib.parse import unquote, urlsplit

from fastapi import Path, Request
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException

from .project_notes import LOCAL_SOURCE
from .scheduling import WEEKDAYS, format_pacific
from .storage import MAX_SCHEDULE_REQUEST_CHARS
from .watch_subjects import describe_subject, form_subject


RecordId = Annotated[int, Path(ge=1, le=2**63 - 1)]
OUTCOMES = {
    "baseline": "Baseline recorded",
    "material": "Material change",
    "unchanged": "No material change",
    "unavailable": "Could not verify",
}


def evidence_label(source):
    """Name the exact passage or site in a link to verified evidence."""
    if not isinstance(source, str):
        return None
    note = re.fullmatch(r"/notes/([1-9][0-9]*)\?revision=([1-9][0-9]*)(?:#claim-([1-9][0-9]*))?", source)
    if note:
        return f"Project note {note[1]} · revision {note[2]}" + (f" · claim {note[3]}" if note[3] else "")
    local = LOCAL_SOURCE.fullmatch(source)
    if local:
        _, kind, encoded, revision, page, line = local.groups()
        if kind == "evidence":
            return "Source snapshot " + encoded[:8]
        name = unquote(encoded)
        return name + (f" · revision {revision}" if revision else f" · page {page}" if page else "") + (f" · line {line}" if line else "")
    if any(ord(c) <= 32 for c in source) or "\\" in source:
        return None
    try:
        url = urlsplit(source)
        if url.scheme.lower() in {"http", "https"} and url.hostname and url.username is None:
            return source.partition("://")[2]
    except ValueError:
        pass
    return None


def install_watch_pages(app, store, templates, check_origin, check_size, wake, clock, enabled):
    templates.env.filters["watch_evidence_label"] = evidence_label
    templates.env.filters["watch_time"] = format_pacific
    templates.env.globals["watch_outcomes"] = OUTCOMES

    def render_watches(request, values=None, subject=None, subject_info=None, error=None, status=200):
        return templates.TemplateResponse(
            request=request, name="watches.html", status_code=status,
            context={"watches": store.list_watches(), "weekdays": WEEKDAYS, "error": error,
                     "values": values or {}, "subject": subject or {}, "subject_info": subject_info,
                     "max_chars": MAX_SCHEDULE_REQUEST_CHARS, "enabled": enabled},
        )

    def render_watch(request, watch_id, error=None, status=200):
        watch = store.get_watch(watch_id)
        if watch is None:
            raise HTTPException(404, "Watch not found.")
        return templates.TemplateResponse(
            request=request, name="watch.html", status_code=status,
            context={"watch": watch, "weekdays": WEEKDAYS, "error": error, "enabled": enabled},
        )

    @app.get("/watches")
    def watches(request: Request):
        try:
            subject = form_subject(request.query_params)
            subject_info = describe_subject(store, subject)
        except ValueError as exc:
            return render_watches(request, error=str(exc), status=400)
        return render_watches(request, subject=subject, subject_info=subject_info)

    @app.post("/watches")
    async def save_watch(request: Request):
        check_origin(request)
        check_size(request, MAX_SCHEDULE_REQUEST_CHARS * 12 + 2048)
        values = {}
        subject, subject_info = {}, None
        try:
            async with request.form(max_files=0, max_fields=7, max_part_size=MAX_SCHEDULE_REQUEST_CHARS * 4) as form:
                for field in ("request", "frequency", "local_time", "required_urls"):
                    value = form.get(field, "")
                    if not isinstance(value, str):
                        raise ValueError("Enter text for each watch field.")
                    values[field] = value
                subject = form_subject(form)
                subject_info = describe_subject(store, subject)
            frequency = values["frequency"]
            if frequency == "daily":
                cadence, weekday = "daily", None
            elif frequency in {str(day) for day in range(7)}:
                cadence, weekday = "weekly", int(frequency)
            else:
                raise ValueError("Choose a daily or weekly schedule.")
            watch_id = await asyncio.to_thread(store.create_watch, values["request"].strip(), cadence,
                                               weekday, values["local_time"], now=clock(), subject=subject or None,
                                               required_urls=[url.strip() for url in values["required_urls"].splitlines()
                                                              if url.strip()])
        except (HTTPException, ValueError) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return render_watches(request, values=values, subject=subject, subject_info=subject_info,
                                  error=message, status=400)
        wake.set()
        return RedirectResponse(request.url_for("watch_detail", watch_id=watch_id), status_code=303)

    @app.get("/watches/{watch_id}")
    def watch_detail(request: Request, watch_id: RecordId):
        return render_watch(request, watch_id)

    @app.post("/watches/{watch_id}/check-now")
    async def check_watch_now(request: Request, watch_id: RecordId):
        check_origin(request)
        check_size(request, 1024)
        if not enabled:
            return render_watch(request, watch_id, error="Configure a model to check this watch.", status=503)
        try:
            task_id = await asyncio.to_thread(store.enqueue_watch_now, watch_id, now=clock())
        except KeyError:
            raise HTTPException(404, "Watch not found.") from None
        except ValueError as exc:
            return render_watch(request, watch_id, error=str(exc), status=409)
        wake.set()
        return RedirectResponse(request.url_for("assignment", task_id=task_id), status_code=303)

    @app.post("/watches/{watch_id}/stop")
    async def stop_watch(request: Request, watch_id: RecordId):
        check_origin(request)
        check_size(request, 1024)
        try:
            await asyncio.to_thread(store.stop_watch, watch_id, now=clock())
        except KeyError:
            raise HTTPException(404, "Watch not found.") from None
        return RedirectResponse(request.url_for("watch_detail", watch_id=watch_id), status_code=303)

    @app.post("/watches/{watch_id}/notices/{notice_id}/acknowledge")
    async def acknowledge_watch(request: Request, watch_id: RecordId, notice_id: RecordId):
        check_origin(request)
        check_size(request, 1024)
        try:
            await asyncio.to_thread(store.acknowledge_watch_notice, watch_id, notice_id)
        except KeyError:
            raise HTTPException(404, "Watch finding not found.") from None
        return RedirectResponse(request.url_for("index"), status_code=303)
