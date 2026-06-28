"""特化工具路由 — /tool/*"""

from fastapi import APIRouter, Request, Query
from app.utils import (
    find_share_by_vpath,
    check_access,
    get_absolute_path,
    handle_edit,
    error_response,
    write_log,
    mask_key,
)

router = APIRouter(prefix="/tool", tags=["tools"])


@router.get("/edit")
async def tool_edit(
    request: Request,
    path: str = Query(...),
    key: str = Query(...),
    old_str: str = Query(...),
    new_str: str = Query(...),
    replace_all: int = Query(0),
    json: int = Query(0, alias="json"),
):
    json_mode = json == 1
    share = find_share_by_vpath("/" + path)
    if not share:
        return error_response("No share matched", 404, json_mode)
    err_msg = check_access(share, key)
    if err_msg:
        write_log(
            {
                "action": "access_denied",
                "path": path,
                "ip": request.client.host,
                "key": mask_key(key),
                "reason": err_msg,
            }
        )
        return error_response(err_msg, 403, json_mode)
    abs_path = get_absolute_path(share, "/" + path)
    return await handle_edit(
        share,
        abs_path,
        old_str,
        new_str,
        replace_all == 1,
        path,
        request.client.host,
        key,
        json_mode,
    )
