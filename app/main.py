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
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload

from app import auth
from app.auth import SessionUser
from app.config import get_settings
from app.database import SessionLocal, get_session, init_db
from app.importer.scanner import scan_vault
from app.models import AppSetting, GenerationJob, Question, SourceNote
from app.services import generation as generation_service
from app.services import questions as question_service
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


templates.env.filters["json_pretty"] = _json_pretty
templates.env.filters["datetime"] = _format_datetime


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


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    due = review_service.due_count(db)
    draft_count = int(db.scalar(select(func.count(Question.id)).where(Question.status == "draft")) or 0)
    needs_questions = int(
        db.scalar(select(func.count(SourceNote.id)).where(SourceNote.learning_status == "needs-questions")) or 0
    )
    active_questions = int(db.scalar(select(func.count(Question.id)).where(Question.status == "active")) or 0)
    pending_generation_jobs = int(db.scalar(select(func.count(GenerationJob.id)).where(GenerationJob.status == "pending")) or 0)
    attempts = review_service.recent_attempts(db, limit=8)
    return templates.TemplateResponse(
        "dashboard.html",
        context(
            request,
            user,
            due_count=due,
            draft_count=draft_count,
            needs_questions=needs_questions,
            active_questions=active_questions,
            pending_generation_jobs=pending_generation_jobs,
            recent_attempts=attempts,
        ),
    )


@app.post("/sources/scan")
def trigger_scan(
    request: Request,
    csrf_token: str = Form(...),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    result = scan_vault(db, settings)
    message = f"scan=Imported {result.imported}, created {result.draft_questions_created} drafts, errors {result.skipped_errors}"
    return redirect(f"/sources?{message}")


@app.get("/sources", response_class=HTMLResponse)
def sources(
    request: Request,
    learning_status: str = Query("all"),
    scan: str | None = Query(None),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    query = select(SourceNote).order_by(desc(SourceNote.last_imported_at), SourceNote.title.asc())
    if learning_status != "all":
        query = query.where(SourceNote.learning_status == learning_status)
    notes = db.scalars(query).all()
    return templates.TemplateResponse(
        "sources.html",
        context(request, user, notes=notes, selected_status=learning_status, scan_message=scan),
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
    source_questions = (
        db.execute(
            select(Question)
            .options(joinedload(Question.schedule))
            .where(Question.source_note_id == source.id)
            .order_by(Question.status.asc(), Question.id.asc())
        )
        .unique()
        .scalars()
        .all()
    )
    generation_jobs = generation_service.jobs_for_source(db, source.id)
    return templates.TemplateResponse(
        "source_detail.html",
        context(request, user, source=source, questions=source_questions, generation_jobs=generation_jobs),
    )


@app.post("/sources/{source_id}/generation-jobs")
def enqueue_generation_job(
    source_id: int,
    request: Request,
    csrf_token: str = Form(...),
    question_count: int = Form(8),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    source = db.get(SourceNote, source_id)
    if source is None:
        raise HTTPException(status_code=404)
    generation_service.enqueue_generation_job(db, source, question_count=question_count)
    return redirect(f"/sources/{source_id}")


@app.get("/generation-jobs", response_class=HTMLResponse)
def generation_jobs(
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    jobs = generation_service.recent_jobs(db, limit=100)
    return templates.TemplateResponse("generation_jobs.html", context(request, user, jobs=jobs))


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


@app.post("/sources/{source_id}/questions")
def add_question(
    source_id: int,
    request: Request,
    csrf_token: str = Form(...),
    question_type: str = Form("short-answer"),
    prompt: str = Form(...),
    answer: str = Form(""),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    question_service.create_question(db, source_id, question_type=question_type, prompt=prompt, answer=answer)
    return redirect(f"/sources/{source_id}")


@app.get("/questions/drafts", response_class=HTMLResponse)
def draft_questions(
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    drafts = (
        db.execute(
            select(Question)
            .options(joinedload(Question.source_note))
            .where(Question.status == "draft")
            .order_by(Question.created_at.asc())
        )
        .unique()
        .scalars()
        .all()
    )
    return templates.TemplateResponse("drafts.html", context(request, user, drafts=drafts))


@app.get("/questions/{question_id}/edit", response_class=HTMLResponse)
def edit_question_form(
    question_id: int,
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("question_edit.html", context(request, user, question=question, error=None))


@app.post("/questions/{question_id}", response_class=HTMLResponse)
def update_question(
    question_id: int,
    request: Request,
    csrf_token: str = Form(...),
    question_type: str = Form(...),
    prompt: str = Form(...),
    answer: str = Form(""),
    options_json: str = Form(""),
    rubric_json: str = Form(""),
    feedback_json: str = Form(""),
    source_reference: str = Form(""),
    difficulty: int = Form(2),
    question_status: str = Form("draft"),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> Any:
    auth.validate_csrf(request, settings, csrf_token)
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404)
    try:
        question_service.update_question(
            db,
            question,
            question_type=question_type,
            prompt=prompt,
            answer=answer,
            options_json=options_json,
            rubric_json=rubric_json,
            feedback_json=feedback_json,
            source_reference=source_reference,
            difficulty=difficulty,
            status=question_status,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "question_edit.html",
            context(request, user, question=question, error=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return redirect(f"/sources/{question.source_note_id}")


@app.post("/questions/{question_id}/approve")
def approve_question(
    question_id: int,
    request: Request,
    csrf_token: str = Form(...),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404)
    question_service.approve_question(db, question)
    return redirect(f"/sources/{question.source_note_id}")


@app.post("/questions/{question_id}/retire")
def retire_question(
    question_id: int,
    request: Request,
    csrf_token: str = Form(...),
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> RedirectResponse:
    auth.validate_csrf(request, settings, csrf_token)
    question = db.get(Question, question_id)
    if question is None:
        raise HTTPException(status_code=404)
    question_service.retire_question(db, question)
    return redirect(f"/sources/{question.source_note_id}")


@app.get("/review", response_class=HTMLResponse)
def start_review(
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> Any:
    queue = review_service.due_questions(db, limit=settings.review_session_size)
    if not queue:
        return review_summary(request, user=user, db=db)
    return redirect(f"/review/{queue[0].id}")


@app.get("/review/{question_id:int}", response_class=HTMLResponse)
def review_question(
    question_id: int,
    request: Request,
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
    queue = review_service.due_questions(db, limit=settings.review_session_size)
    if queue:
        return redirect(f"/review/{queue[0].id}")
    return redirect("/review/summary")


@app.get("/review/summary", response_class=HTMLResponse)
def review_summary(
    request: Request,
    user: SessionUser = Depends(require_user),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    attempts = review_service.recent_attempts(db, limit=20)
    weak = review_service.weak_areas(db, limit=5)
    return templates.TemplateResponse(
        "review_summary.html",
        context(request, user, attempts=attempts, weak_areas=weak, due_count=review_service.due_count(db)),
    )


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

