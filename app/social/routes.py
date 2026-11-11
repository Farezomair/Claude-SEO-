"""Social page in Ascend: connect Facebook, switch the autopilot, see and skip upcoming posts."""
import json
import threading
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..auth import current_user
from . import facebook, planner, scheduler, store

router = APIRouter()
templates = None  # set by main.py


def _guard(request):
    return None if current_user(request) else RedirectResponse("/login", status_code=303)


def _back(msg=""):
    return RedirectResponse("/social" + (f"?msg={quote(msg)}" if msg else ""), status_code=303)


def _view(r, cfg):
    r = dict(r)
    r["day"] = datetime.fromisoformat(r["slot_at"]).astimezone(planner._tz(cfg)).strftime("%a %b %d, %I:%M %p")
    r["problem_list"] = store.problems(r)
    return r


@router.get("/social", response_class=HTMLResponse)
def social_page(request: Request, msg: str = ""):
    if (g := _guard(request)):
        return g
    cfg = store.load()
    upcoming = [_view(r, cfg) for r in store.posts("platform='facebook' AND status IN ('planned','handed')")]
    recent = [_view(r, cfg) for r in store.posts("platform='facebook' AND status NOT IN ('planned','handed')", order="slot_at DESC", limit=30)]
    try:
        preview = json.loads(store.get_meta("social_preview") or "{}")
    except ValueError:
        preview = {}
    return templates.TemplateResponse("social.html", {
        "request": request, "user": current_user(request), "nav": "social", "msg": msg, "cfg": cfg,
        "preview": preview, "connected": bool(facebook.token()), "loop_on": scheduler.ENABLED, "upcoming": upcoming, "recent": recent,
        "cfg_json": json.dumps({k: v for k, v in cfg.items() if k != "style_examples"}, indent=1),
    })


@router.get("/social/status")
def social_status(request: Request):
    if (g := _guard(request)):
        return g
    cfg = store.load()
    return JSONResponse({
        "loop_on": scheduler.ENABLED, "autopilot": cfg["enabled"], "connected": bool(facebook.token()),
        "page": cfg["facebook"].get("page_name"), "site": cfg["site"],
        "publisher": cfg["publisher"],
        "upcoming": [_view(r, cfg) for r in store.posts("platform='facebook' AND status IN ('planned','handed')")],
        "recent": [_view(r, cfg) for r in store.posts("platform='facebook' AND status NOT IN ('planned','handed')", order="slot_at DESC", limit=15)],
    })


@router.get("/social/queue")
def social_queue(request: Request):
    """For the Chrome scheduling task: drafted posts not yet in Meta's planner, soonest first."""
    if (g := _guard(request)):
        return g
    from datetime import timezone
    cfg = store.load()
    out = []
    for r in store.posts("platform='facebook' AND status='planned'"):
        at = datetime.fromisoformat(r["slot_at"])
        out.append({"id": r["id"], "slot_utc": at.astimezone(timezone.utc).isoformat(),
                    "slot_local": at.astimezone(planner._tz(cfg)).strftime("%Y-%m-%d %H:%M ") + cfg["timezone"],
                    "kind": r["kind"], "source_url": r["source_url"], "source_title": r["source_title"],
                    "text": r["message"].strip() + (f"\n\n{r['link']}" if r["link"] else ""),
                    "image_url": r["image_url"] or "", "image_file": (r["image_url"] or "").rsplit("/", 1)[-1]})
    return JSONResponse({"publisher": cfg["publisher"], "page": cfg["facebook"].get("page_name"), "posts": out})


@router.post("/social/handed/{pid}")
def social_handed(request: Request, pid: int):
    """The Chrome task scheduled this post in Meta's planner."""
    if (g := _guard(request)):
        return g
    store.update(pid, status="handed", posted_at=store.now_iso())
    return _back(f"Post {pid} marked as scheduled in Meta's planner.")


@router.post("/social/connect")
def social_connect(request: Request, app_id: str = Form(""), app_secret: str = Form(""),
                   user_token: str = Form(""), page_token: str = Form(""), page_id: str = Form("")):
    if (g := _guard(request)):
        return g
    try:
        if page_token.strip():
            res = facebook.connect_page_token(page_token)
        elif app_id and app_secret and user_token:
            res = facebook.connect(app_id, app_secret, user_token, page_id)
        else:
            return _back("Fill in the app ID, app secret and user token (or a page token).")
    except Exception as e:
        return _back(f"Not connected: {e}")
    return _back(f"Connected to {res['page_name']} ({res['page_id']}). Now run the test.")


@router.post("/social/test")
def social_test(request: Request):
    if (g := _guard(request)):
        return g
    cfg = store.load()
    img = ""
    try:
        from . import source
        posts = source.blog_posts(cfg["site"], cfg["exclude_words"], limit=5) if cfg["site"] else []
        img = next((im["url"] for p in posts for im in p["images"]), "")
        steps = facebook.test(img)
        return _back("Test passed: " + "; ".join(steps) + ". Nothing was published.")
    except Exception as e:
        return _back(f"Test failed: {e}")


@router.post("/social/autopilot")
def social_autopilot(request: Request, on: str = Form("")):
    if (g := _guard(request)):
        return g
    turn_on = on == "1"
    if turn_on and not facebook.token():
        return _back("Connect the Facebook page first.")
    store.save_patch({"enabled": turn_on})
    return _back("Autopilot is ON: planned posts publish at their time." if turn_on else "Autopilot is OFF: nothing will be posted.")


@router.post("/social/plan")
def social_plan(request: Request):
    if (g := _guard(request)):
        return g
    store.set_meta("social_last_plan", "2000-01-01T00:00:00+00:00")
    threading.Thread(target=planner.run_once, daemon=True).start()
    return _back("Planning started; refresh in a minute.")


@router.post("/social/skip/{pid}")
def social_skip(request: Request, pid: int):
    if (g := _guard(request)):
        return g
    store.update(pid, status="skipped")
    return _back("Skipped. Another post will be drafted for that day on the next run.")


@router.post("/social/edit/{pid}")
def social_edit(request: Request, pid: int, message: str = Form(...)):
    if (g := _guard(request)):
        return g
    store.update(pid, message=message.strip())
    return _back("Saved.")


@router.post("/social/upload-settings")
async def social_settings(request: Request, file: UploadFile = File(...)):
    if (g := _guard(request)):
        return g
    try:
        patch = json.loads((await file.read()).decode("utf-8-sig"))
    except ValueError as e:
        return _back(f"Not valid JSON: {e}")
    history = patch.pop("history", None)
    store.save_patch(patch)
    n = planner.import_history(history) if history else 0
    return _back(f"Settings saved{f', {n} past posts recorded' if history else ''}.")


def _preview_job():
    from datetime import date, datetime, time, timedelta, timezone
    cfg = store.load()
    start = date.fromisoformat(cfg["start_date"]) if cfg["start_date"] else datetime.now(timezone.utc).date()
    now = datetime.combine(max(start, datetime.now(timezone.utc).date()) - timedelta(days=1), time(15), tzinfo=timezone.utc)
    store.set_meta("social_preview", json.dumps({"status": "running", "at": store.now_iso(), "rows": []}))
    try:
        rows = planner.plan(now=now, dry=True)
        out = [dict(_view(r, cfg), problem_list=json.loads(r["problems"] or "[]")) for r in rows]
        store.set_meta("social_preview", json.dumps({"status": "done", "at": store.now_iso(), "rows": out}))
    except Exception as e:
        store.set_meta("social_preview", json.dumps({"status": f"failed: {e}", "at": store.now_iso(), "rows": []}))


@router.post("/social/preview")
def social_preview(request: Request):
    """Write the first planning window's posts without saving or posting them, to check the writing."""
    if (g := _guard(request)):
        return g
    threading.Thread(target=_preview_job, daemon=True).start()
    return _back("Preview started: the first week's drafts appear below in a minute or two. Nothing is saved or posted.")
