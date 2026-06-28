# AI Agent FTP 服务

本服务为 AI Agent 设计的类 FTP 加密文件服务。
所有写操作经过 SSRF 防护、路径穿越检测和速率限制。

## 端点

| 方法 | 路径 | 说明 | 权限 |
|------|------|------|------|
| GET | /s/{path}?key=xxx | 查看文件或目录 | read/list |
| GET | ?mkdir=1 | 创建目录 | write |
| GET | ?upload_url=URL | 从 URL 拉取 | write |
| GET | ?content=数据 | 上传文本(64KB上限) | write |
| GET | ?delete=1 | 删除 | delete |
| GET | ?rename_to=新名 | 重命名 | rename |
| GET | ?move_to=/目标 | 移动 | rename |
| PUT | /s/{path}?key=xxx | 上传(Body原始内容) | write |
| POST | /s/{path}?key=xxx | 上传(multipart) | write |
| DELETE | /s/{path}?key=xxx | 删除 | delete |
| GET | /perm/{path}?key=xxx | 查询权限 | — |

## 通用参数

- &raw=1 原始内容  &download=1 下载  &json=1 JSON输出

## 安全限制

- 上传最大 500MB | content= 最大 64KB
- upload_url 仅 HTTPS，拦截内网(SSRF防护)
- 写操作 60 次/分钟(速率限制)
- 文件名净化，拒绝路径穿越

## 限制 Agent 示例(仅GET)

```
mkdir:  GET /s/data?key=abc&mkdir=1
upload: GET /s/data?key=abc&upload_url=https://example.com/file.zip
content:GET /s/data/note.txt?key=abc&content=Hello+World
delete: GET /s/data/old.txt?key=abc&delete=1
rename: GET /s/data/old.txt?key=abc&rename_to=new.txt
perm:   GET /perm/data?key=abc
```

## 管理

- /admin 管理界面  /admin/stats 统计  /health 健康检查
