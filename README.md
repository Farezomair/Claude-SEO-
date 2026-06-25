# SEO Agent System

A self-running SEO platform. Add a website, it gets a private workspace, and a
crew of Claude-powered agents audits it, fixes what is wrong, writes missing
content, and reports progress on a weekly schedule.

Built stage by stage. See `seo-agent-system-master-plan.md` for the full brief.

## Current stage — Stage 1: the empty shell

A web app with:

- single-owner login (no signup, no user management)
- a Websites list + Add-website button
- a Postgres-backed private workspace per site (Audit / Fixes / Content / Settings tabs)

No agents yet. This stage proves we can build, deploy, and store isolated
per-site data.

## Tech

- **FastAPI** + **Jinja2** (server-rendered HTML)
- **SQLAlchemy** + **Postgres** (SQLite fallback for local dev)
- Deployed on **Railway**

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env        # then edit APP_PASSWORD etc.
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 — it redirects to the login page. With no
`DATABASE_URL` set, it uses a local `seo_agent.db` SQLite file.

## Environment variables

| Variable       | Purpose                                              |
| -------------- | ---------------------------------------------------- |
| `SECRET_KEY`   | Signs the login session cookie. Long random string.  |
| `APP_USERNAME` | Owner login username (default `admin`).              |
| `APP_PASSWORD` | Owner login password. **Required** to log in.        |
| `DATABASE_URL` | Postgres URL. Provided automatically by Railway.     |

## Deploy on Railway

1. New project → Deploy from this GitHub repo.
2. Add a **Postgres** database to the project (sets `DATABASE_URL`).
3. Set `SECRET_KEY`, `APP_USERNAME`, `APP_PASSWORD` as service variables.
4. Open the generated URL and log in.
