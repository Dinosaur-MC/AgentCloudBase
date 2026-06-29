"""共享资源：模板引擎、帮助文本"""

from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _autoescape(template_name: str) -> bool:
    return template_name is None or template_name.endswith((".html", ".jinja"))


env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=_autoescape,
)
templates = Jinja2Templates(env=env)

HELP_MD_PATH = Path(__file__).parent / "SKILL.md"
HELP_MD = HELP_MD_PATH.read_text(encoding="utf-8")


def resolve_help_level(level: str) -> tuple[str, str]:
    """校验 help level 参数，返回 (有效level, 提示消息)"""
    valid = {"basic", "full", "md", "1", "2"}
    if level in valid:
        return (level, "")
    alias = {"1": "basic", "2": "full"}
    if level in alias:
        return (alias[level], "")
    hint = f"Invalid help level '{level}', valid: basic, full, md (or 1, 2). Using 'basic'."
    return ("basic", hint)
