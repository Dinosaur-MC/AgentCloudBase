"""杂项路由 — /health, /help"""

from fastapi import APIRouter, Request, Query
from fastapi.responses import PlainTextResponse
from app.common import templates, HELP_MD, resolve_help_level

router = APIRouter(tags=["misc"])


@router.get("/health")
async def health_check():
    return {"status": "ok", "service": "AIWebFTP", "version": "1.0"}


@router.get("/help")
async def help_page(
    request: Request,
    level: str = "basic",
    format: str = Query("html"),
):
    cleaned, hint = resolve_help_level(level)
    if cleaned == "md":
        format = "md"
    context = {"level": cleaned, "hint": hint}
    if format == "md":
        return PlainTextResponse(HELP_MD)
    return templates.TemplateResponse(request, "help.jinja", context)
