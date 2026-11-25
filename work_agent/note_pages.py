"""Reader and optimistic editor for revisioned project notes."""

from typing import Annotated

from fastapi import Path, Query, Request
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException

from .project_notes import MAX_CLAIMS, NoteConflict


RecordId = Annotated[int, Path(ge=1, le=2**63 - 1)]


def install_note_pages(app, store, templates, check_origin, check_size):
    @app.get("/notes")
    def project_notes(request: Request, q: str = ""):
        if len(q) > 160:
            raise HTTPException(422, "Use up to 160 characters.")
        return templates.TemplateResponse(
            request=request, name="project_notes.html", context={"notes": store.search_project_notes(q), "q": q},
        )

    def render(request, note_id, revision=None, *, error=None, draft=None, reason="", status=200):
        note = store.project_note_by_id(note_id, revision)
        if note is None:
            raise HTTPException(404, "Note revision not found.")
        return templates.TemplateResponse(
            request=request, name="project_note.html",
            context={"note": note, "historical": revision is not None,
                     "draft": note["claims"] if draft is None else draft, "reason": reason, "error": error},
            status_code=status,
        )

    @app.get("/notes/{note_id}")
    def project_note(request: Request, note_id: RecordId, revision: Annotated[int | None, Query(ge=1, le=2**63 - 1)] = None):
        return render(request, note_id, revision)

    @app.post("/notes/{note_id}")
    async def edit_project_note(request: Request, note_id: RecordId):
        check_origin(request)
        check_size(request, 2 * 1024 * 1024)
        claims = []
        reason = ""
        try:
            async with request.form(max_files=0, max_fields=3 * (MAX_CLAIMS + 1) + 2) as form:
                reason = form.get("reason", "")
                revision = form.get("expected_revision", "")
                if not isinstance(revision, str) or not revision.isascii() or not revision.isdigit() or len(revision) > 19:
                    raise ValueError("Reload the note before saving.")
                for text, sources, predecessor in zip(form.getlist("claim"), form.getlist("sources"),
                                                     form.getlist("supersedes"), strict=True):
                    if text.strip() or sources.strip():
                        claim = {"text": text, "sources": [url.strip() for url in sources.splitlines() if url.strip()]}
                        if predecessor.strip():
                            claim["supersedes"] = int(predecessor)
                        claims.append(claim)
            store.edit_project_note(note_id, int(revision), claims, reason)
        except (ValueError, TypeError) as exc:
            return render(request, note_id, error=str(exc), draft=claims, reason=reason,
                          status=409 if isinstance(exc, NoteConflict) else 422)
        return RedirectResponse(request.url_for("project_note", note_id=note_id), status_code=303)
