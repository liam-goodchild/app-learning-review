from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, joinedload

from app import auth
from app.auth import SessionUser
from app.config import get_settings
from app.database import SessionLocal, get_session, init_db
from app.importer.scanner import scan_vault
from app.models import AppSetting, GenerationJob, Question, SourceNote
from app.services import generation as generation_service
from app.services import reviews as review_service
from app.services.json_tools import loads_json

settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _json_pretty(value: str | None) -> str:
    data = loads_json(value, None)
    if data is None:
        return "" if value is None else value
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _format_datetime(value: Any) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M")


def _format_question_type(value: Any) -> str:
    if not value:
        return ""
    return str(value).replace("-", " ").capitalize()


def _format_due(value: Any) -> str:
    if value is None:
        return ""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    due = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    delta = due - now
    days = delta.total_seconds() / 86400
    if days < -1:
        return f"Overdue by {int(-days)}d"
    if days < 0:
        return "Due now"
    if days < 1:
        if due.date() == now.date():
            return "Due today"
        return "Due tomorrow"
    if days < 2:
        return "Due tomorrow"
    if days < 7:
        return f"Due in {int(days)}d"
    return f"Due {due.strftime('%b %d')}"


templates.env.filters["json_pretty"] = _json_pretty
templates.env.filters["datetime"] = _format_datetime
templates.env.filters["question_type"] = _format_question_type
templates.env.filters["due"] = _format_due


async def periodic_scan_loop() -> None:
    while True:
        await asyncio.sleep(settings.scan_interval_seconds)
        with SessionLocal() as db:
            scan_vault(db, settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as db:
        auth.ensure_password_hash(db, settings)
    task: asyncio.Task[None] | None = None
    if settings.enable_periodic_scan:
        task = asyncio.create_task(periodic_scan_loop())
    try:
        yield
    finally:
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Enhanced Learning App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def require_user(request: Request) -> SessionUser:
    return auth.require_user(request, settings)


def require_worker(authorization: str | None = Header(default=None)) -> None:
    if not settings.worker_token:
        raise HTTPException(status_code=404, detail="Worker API is not enabled")
    expected = f"Bearer {settings.worker_token}"
    if authorization != expected:
        raise HTTPException(status_code=403, detail="Invalid worker token")


def context(request: Request, user: SessionUser | None = None, **extra: Any) -> dict[str, Any]:
    if user is None:
        user = auth.current_user_from_request(request, settings)
    base = {
        "request": request,
        "user": user,
        "csrf_token": user.csrf_token if user else "",
        "settings": settings,
    }
    base.update(extra)
    return base


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> Any:
    if auth.current_user_from_request(request, settings):
        return redirect("/")
    return templates.TemplateResponse("login.html", context(request))


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_session),
) -> Any:
    password_hash = auth.get_password_hash(db)
    if username != settings.app_username or not password_hash or not auth.verify_password(password, password_hash):
        return templates.TemplateResponse(
            "login.html",
            context(request, error="Invalid username or password"),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    response = redirect("/")
    auth.set_login_cookie(response, settings, username)
    return response


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    response = redirect("/login")
    auth.clear_login_cookie(response, settings)
    return response


@app.get("/")
def root(_: SessionUser = Depends(require_user)) -> RedirectResponse:
    return redirect("/sources")


@app.post("/sources/scan")
def trigger_scan(
    request: Request,
    csrf_token: str = Form(...),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    result = scan_vault(db, settings)
    message = (
        f"scan=Imported {result.imported}, enqueued {result.generation_jobs_enqueued} generation jobs, "
        f"errors {result.skipped_errors}"
    )
    return redirect(f"/sources?{message}")


@app.get("/sources", response_class=HTMLResponse)
def sources(
    request: Request,
    tag: str = Query(""),
    scan: str | None = Query(None),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    notes = db.scalars(
        select(SourceNote).order_by(desc(SourceNote.last_imported_at), SourceNote.title.asc())
    ).all()
    tag_clean = tag.strip().lower()
    all_tags: set[str] = set()
    filtered = []
    for note in notes:
        tags = loads_json(note.tags_json, [])
        tag_list = [str(t).lower() for t in tags] if isinstance(tags, list) else []
        for t in tag_list:
            all_tags.add(t)
        if not tag_clean or tag_clean in tag_list:
            filtered.append(note)
    return templates.TemplateResponse(
        "sources.html",
        context(
            request,
            user,
            notes=filtered,
            selected_tag=tag_clean,
            all_tags=sorted(all_tags),
            scan_message=scan,
        ),
    )


@app.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(
    source_id: int,
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    source = db.get(SourceNote, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    active_questions = (
        db.execute(
            select(Question)
            .options(joinedload(Question.schedule))
            .where(Question.source_note_id == source.id, Question.status == "active")
            .order_by(Question.id.asc())
        )
        .unique()
        .scalars()
        .all()
    )
    jobs = generation_service.jobs_for_source(db, source.id)
    pending_job = next((j for j in jobs if j.status in ("pending", "running")), None)
    last_failed_job = next((j for j in jobs if j.status == "failed"), None)
    return templates.TemplateResponse(
        "source_detail.html",
        context(
            request,
            user,
            source=source,
            active_questions=active_questions,
            pending_job=pending_job,
            last_failed_job=last_failed_job,
        ),
    )


@app.post("/sources/{source_id}/delete")
def delete_source(
    source_id: int,
    request: Request,
    csrf_token: str = Form(...),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    source = db.get(SourceNote, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    db.delete(source)
    db.commit()
    return redirect("/sources")


@app.post("/sources/{source_id}/regenerate")
def regenerate_source(
    source_id: int,
    request: Request,
    csrf_token: str = Form(...),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    source = db.get(SourceNote, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    existing = db.scalars(select(Question).where(Question.source_note_id == source.id)).all()
    for q in existing:
        db.delete(q)
    source.learning_status = "needs-questions"
    db.flush()
    generation_service.enqueue_generation_job(db, source)
    db.commit()
    return redirect(f"/sources/{source_id}")


@app.post("/worker/generation-jobs/claim")
def worker_claim_generation_job(
    _: None = Depends(require_worker),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    job = generation_service.claim_next_job(db, settings)
    if job is None:
        return {"job": None}
    return {
        "job": {
            "id": job.id,
            "source_id": job.source_note_id,
            "source_title": job.source_note.title,
            "provider": job.provider,
            "question_count": job.question_count,
            "prompt": job.prompt_text,
        }
    }


def _job_with_source(db: Session, job_id: int) -> GenerationJob:
    job = (
        db.execute(
            select(GenerationJob)
            .options(joinedload(GenerationJob.source_note))
            .where(GenerationJob.id == job_id)
        )
        .unique()
        .scalar_one_or_none()
    )
    if job is None:
        raise HTTPException(status_code=404)
    return job


@app.post("/worker/generation-jobs/{job_id:int}/complete")
def worker_complete_generation_job(
    job_id: int,
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_worker),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    job = _job_with_source(db, job_id)
    raw_output = str(payload.get("raw_output", ""))
    try:
        completed = generation_service.complete_generation_job(db, job, raw_output)
    except ValueError as exc:
        failed = generation_service.fail_generation_job(db, job, str(exc), raw_output=raw_output)
        return {"id": failed.id, "status": failed.status, "error": failed.error, "draft_questions_created": 0}
    return {
        "id": completed.id,
        "status": completed.status,
        "draft_questions_created": completed.draft_questions_created,
    }


@app.post("/worker/generation-jobs/{job_id:int}/fail")
def worker_fail_generation_job(
    job_id: int,
    payload: dict[str, Any] = Body(...),
    _: None = Depends(require_worker),
    db: Session = Depends(get_session),
) -> dict[str, Any]:
    job = _job_with_source(db, job_id)
    failed = generation_service.fail_generation_job(
        db,
        job,
        str(payload.get("error", "Unknown worker failure")),
        raw_output=str(payload.get("raw_output", "")),
    )
    return {"id": failed.id, "status": failed.status, "error": failed.error}


@app.get("/review/{question_id:int}", response_class=HTMLResponse)
def review_question(
    question_id: int,
    request: Request,
    return_to: str = Query(""),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    question = (
        db.execute(
            select(Question)
            .options(joinedload(Question.source_note), joinedload(Question.schedule))
            .where(Question.id == question_id)
        )
        .unique()
        .scalar_one_or_none()
    )
    if question is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(
        "review.html",
        context(
            request,
            user,
            question=question,
            options=review_service.option_list(question),
            rubric=review_service.rubric_items(question),
            categorisation=review_service.categorisation_data(question),
            return_to=return_to,
        ),
    )


@app.post("/review/{question_id:int}", response_class=HTMLResponse)
async def submit_review_answer(
    question_id: int,
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    form = await request.form()
    auth.validate_csrf(request, settings, str(form.get("csrf_token", "")))
    question = (
        db.execute(
            select(Question)
            .options(joinedload(Question.source_note))
            .where(Question.id == question_id)
        )
        .unique()
        .scalar_one_or_none()
    )
    if question is None:
        raise HTTPException(status_code=404)

    if question.type == "categorisation":
        data = {}
        for key, value in form.multi_items():
            if key.startswith("category_"):
                data[key.replace("category_", "")] = str(value)
        submitted_answer = json.dumps(data, sort_keys=True)
    else:
        submitted_answer = str(form.get("answer", ""))

    feedback = review_service.evaluate_answer(question, submitted_answer)
    return templates.TemplateResponse(
        "review_feedback.html",
        context(
            request,
            user,
            question=question,
            submitted_answer=submitted_answer,
            feedback=feedback,
            return_to=str(form.get("return_to", "")),
        ),
    )


@app.post("/review/{question_id:int}/rate")
async def rate_review_answer(
    question_id: int,
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    form = await request.form()
    auth.validate_csrf(request, settings, str(form.get("csrf_token", "")))
    question = (
        db.execute(
            select(Question)
            .options(joinedload(Question.schedule))
            .where(Question.id == question_id)
        )
        .unique()
        .scalar_one_or_none()
    )
    if question is None:
        raise HTTPException(status_code=404)
    score_text = str(form.get("score", ""))
    score = float(score_text) if score_text else None
    rubric_points = [float(value) for value in form.getlist("rubric_points") if str(value).strip()]
    if rubric_points:
        total = sum(float(item.get("points", 1)) for item in review_service.rubric_items(question)) or 1.0
        score = min(1.0, sum(rubric_points) / total)

    review_service.record_attempt_and_schedule(
        db,
        question,
        submitted_answer=str(form.get("submitted_answer", "")),
        rating=str(form.get("rating", "good")),
        confidence=str(form.get("confidence", "medium")),
        score=score,
    )
    return_to = str(form.get("return_to", "")).strip()
    if return_to.startswith("/"):
        return redirect(return_to)
    return redirect(f"/sources/{question.source_note_id}")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    saved: str | None = Query(None),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    password_setting = db.get(AppSetting, auth.PASSWORD_SETTING_KEY)
    return templates.TemplateResponse(
        "settings.html",
        context(request, user, saved=saved, password_setting=password_setting),
    )


@app.post("/settings")
def update_settings(
    request: Request,
    csrf_token: str = Form(...),
    new_password: str = Form(""),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    if new_password.strip():
        setting = db.get(AppSetting, auth.PASSWORD_SETTING_KEY)
        encoded = json.dumps(auth.hash_password(new_password.strip()))
        if setting:
            setting.value_json = encoded
        else:
            db.add(AppSetting(key=auth.PASSWORD_SETTING_KEY, value_json=encoded))
        db.commit()
    return redirect("/settings?saved=1")

