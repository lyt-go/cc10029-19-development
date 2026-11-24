"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。

固定的复核路由先于通用资源路由注册：``/api/staging_area/partitions`` 这类
固定路径必须先匹配，否则会被 ``/api/{module}/{entry_id}`` 当成 entry_id。
"""
from __future__ import annotations

from app.routers import reconcile_review, resource, staging_review

ROUTERS = [staging_review, reconcile_review, resource]


def register(app) -> None:
    """按“固定复核路由 -> 通用资源路由”的顺序挂载，保证路径优先级。"""
    for module in ROUTERS:
        app.include_router(module.router)
