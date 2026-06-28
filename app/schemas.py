"""Pydantic 模型 — 请求参数校验与转换"""

from pydantic import BaseModel, Field, model_validator
from typing import Optional


class FileQuery(BaseModel):
    """文件查看/列表请求参数"""
    key: str = ""
    raw: bool = False
    download: bool = False
    json: bool = Field(False, alias="json")
    mkdir: bool = False
    upload_url: Optional[str] = None
    content: Optional[str] = None
    delete: bool = False
    rename_to: Optional[str] = None
    move_to: Optional[str] = None
    filename: Optional[str] = None
    help: Optional[str] = None

    @property
    def json_mode(self) -> bool:
        return self.json

    @property
    def has_write_op(self) -> bool:
        return any([self.mkdir, bool(self.upload_url),
                    self.content is not None, self.delete,
                    bool(self.rename_to), bool(self.move_to)])

    @model_validator(mode="after")
    def check_conflicts(self):
        ops = sum([1 for v in [self.mkdir, bool(self.upload_url),
                               self.content is not None, self.delete,
                               bool(self.rename_to), bool(self.move_to)] if v])
        if ops > 1:
            raise ValueError("Conflicting parameters: only one operation allowed per request")
        return self


class FileUploadParams(BaseModel):
    """PUT/POST 上传参数"""
    key: str = ""
    json: bool = Field(False, alias="json")
    filename: Optional[str] = None

    @property
    def json_mode(self) -> bool:
        return self.json


class EditParams(BaseModel):
    """/tool/edit 请求参数"""
    path: str
    key: str
    old_str: str
    new_str: str
    replace_all: bool = False
    json: bool = Field(False, alias="json")

    @property
    def json_mode(self) -> bool:
        return self.json


class HelpParams(BaseModel):
    """帮助页请求参数"""
    level: str = "basic"
    format: str = "html"


class LogFilter(BaseModel):
    """日志筛选参数"""
    page: int = 1
    per_page: int = 50
    action: Optional[str] = None
    path_filter: Optional[str] = Field(None, alias="path")
    ip_filter: Optional[str] = Field(None, alias="ip")
    keyword: Optional[str] = None
    date_from: Optional[str] = None
    date_to: Optional[str] = None
