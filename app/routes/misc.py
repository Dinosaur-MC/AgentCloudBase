"""杂项路由 — /health, /help"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import PlainTextResponse
from app.common import templates, HELP_MD, resolve_help_level
from app.schemas import HelpParams

router = APIRouter(tags=["misc"])


@router.get("/health")
async def health_check():
    return {"status": "ok", "service": "AIWebFTP", "version": "1.0"}


@router.get("/help")
async def help_page(
    request: Request,
    p: HelpParams = Depends(),
):
    cleaned, hint = resolve_help_level(p.level)
    fmt = "md" if cleaned == "md" else p.format
    context = {"level": cleaned, "hint": hint}
    if fmt == "md":
        return PlainTextResponse(HELP_MD)
    return templates.TemplateResponse(request, "help.jinja", context)
