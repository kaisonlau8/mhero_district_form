"""客户级字段脱敏。是否看完整明细由登录用户的权限决定（admin 或被授予 full_detail），不再使用共享口令。

脱敏规则：VIN 仅显示前 3 后 4；用车人/车主姓名仅保留首字；手机号 DMS 源数据本来就是 173****3111，
若遇到未脱敏的纯数字手机号则一律遮挡中间 4 位（对所有人，包括 admin）。
"""
from __future__ import annotations

import re

_PHONE_RE = re.compile(r"^\d{7,}$")


def mask_vin(v):
    if not v or len(str(v)) < 8:
        return v
    s = str(v)
    return s[:3] + "*" * (len(s) - 7) + s[-4:]


def mask_name(v):
    if not v:
        return v
    s = str(v)
    return s[0] + "*" * max(len(s) - 1, 1)


def mask_phone(v):
    if not v:
        return v
    s = str(v)
    return s[:3] + "****" + s[-4:] if _PHONE_RE.match(s) else s


def mask_row(ds, row: dict, full_detail: bool) -> dict:
    out = dict(row)
    for c in ds.mask_phone:
        out[c] = mask_phone(out.get(c))
    if full_detail:
        return out
    for c in ds.mask_vin:
        out[c] = mask_vin(out.get(c))
    for c in ds.mask_name:
        out[c] = mask_name(out.get(c))
    return out
