"""特化工具路由 — /tool/*"""

from fastapi import APIRouter, Request, Depends
from app.schemas import EditParams
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
    params: EditParams = Depends(),
):
    json_mode = params.json_mode
    share = find_share_by_vpath("/" + params.path)
    if not share:
        return error_response("No share matched", 404, json_mode)
    err_msg = check_access(share, params.key)
    if err_msg:
        write_log(
            {
                "action": "access_denied",
                "path": params.path,
                "ip": request.client.host,
                "key": mask_key(params.key),
                "reason": err_msg,
            }
        )
        return error_response(err_msg, 403, json_mode)
    abs_path = get_absolute_path(share, "/" + params.path)
    return await handle_edit(
        share,
        abs_path,
        params.old_str,
        params.new_str,
        params.replace_all,
        params.path,
        request.client.host,
        params.key,
        json_mode,
    )
