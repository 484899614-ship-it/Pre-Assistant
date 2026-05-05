"""FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.router import api_router
from backend.api.websocket import router as websocket_router
from backend.config import settings


def create_app() -> FastAPI:
    app = FastAPI(
        title="Pre-Assistant",
        version="0.2.0",
        description="AI-powered academic paper to presentation converter with multi-agent pipeline.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Ensure directories exist
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    settings.workspaces_dir.mkdir(parents=True, exist_ok=True)
    settings.runtime_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/healthz")
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "name": "Pre-Assistant",
            "version": "0.2.0",
            "description": "AI-powered paper-to-PPT converter with multi-agent pipeline.",
        }

    app.include_router(api_router)
    app.include_router(websocket_router)

    # Serve session asset files
    sessions_path = settings.sessions_dir
    if sessions_path.exists():
        app.mount("/assets", StaticFiles(directory=str(sessions_path)), name="assets")

    return app


app = create_app()
