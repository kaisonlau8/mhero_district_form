"""友好的 403 页面（账号没有开通本系统）：Ant Design v5 风格，自包含（内联精简样式 + 图标 sprite），手机友好。

render_no_access(name, system_label, hub_url, logout_url, ds_dir=None) -> str
本文件是 m-hero-auth/mhero_auth/forbidden.py 的拷贝（reportdb 读取自己的 assets/ds）；改动请两处同步。
品牌：Ant Design 蓝。logo / favicon 从 ds/brand/ 读取并内联为 data URI（页面自包含，不依赖静态路由）。
"""
from __future__ import annotations

import base64
import re
from functools import lru_cache
from html import escape
from pathlib import Path

DEFAULT_HUB_URL = "https://oa-m-hero.41box.com/"
_ICONS = ("i-lock", "i-home", "i-logout", "i-user")
_SYMBOL_RE = re.compile(r"<symbol\b[^>]*id=\"([^\"]+)\"[^>]*>.*?</symbol>", re.S)


@lru_cache(maxsize=4)
def _sprite(ds_dir: str) -> str:
    try:
        txt = (Path(ds_dir) / "sprite.svg").read_text(encoding="utf-8")
    except OSError:
        return ""
    parts = [m.group(0) for m in _SYMBOL_RE.finditer(txt) if m.group(1) in _ICONS]
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute" aria-hidden="true">'
            + "".join(parts) + "</svg>")


@lru_cache(maxsize=8)
def _brand_datauri(ds_dir: str, name: str) -> str:
    """ds/brand/<name> 内联为 data URI（文件缺失返回 ''，页面退化为纯文字）。"""
    mime = "image/png" if name.endswith(".png") else "image/x-icon"
    try:
        raw = (Path(ds_dir) / "brand" / name).read_bytes()
    except OSError:
        return ""
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


_CSS = """
*{box-sizing:border-box}
html,body{margin:0;padding:0;touch-action:manipulation}
body{min-height:100vh;background:#f5f5f5;color:rgba(0,0,0,.88);font:14px/1.5714 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB','Microsoft YaHei','Helvetica Neue',Helvetica,Arial,sans-serif;
display:flex;align-items:center;justify-content:center;padding:24px 16px}
.ai{width:1em;height:1em;fill:currentColor;vertical-align:-.125em;display:inline-block;flex:none}
.card{width:100%;max-width:520px;background:#fff;border-radius:8px;padding:40px 32px 32px;text-align:center;
box-shadow:0 1px 2px 0 rgba(0,0,0,.03),0 1px 6px -1px rgba(0,0,0,.02),0 2px 4px 0 rgba(0,0,0,.02)}
.logo{height:28px;width:auto;display:block;margin:0 auto 20px}
.ico{width:72px;height:72px;border-radius:50%;background:#fffbe6;color:#faad14;display:inline-flex;align-items:center;justify-content:center;font-size:36px}
h1{font-size:24px;line-height:1.35;font-weight:600;margin:20px 0 8px}
.sub{color:rgba(0,0,0,.65);margin:0 0 4px;word-break:break-word}
.hint{color:rgba(0,0,0,.45);font-size:13px;margin:0 0 28px}
.who{display:inline-flex;align-items:center;gap:6px;background:#fafafa;border:1px solid #f0f0f0;border-radius:6px;padding:2px 10px;margin:12px 0 16px;color:rgba(0,0,0,.88);max-width:100%}
.who span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.act{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;height:40px;min-width:132px;padding:0 20px;border-radius:6px;border:1px solid #d9d9d9;
background:#fff;color:rgba(0,0,0,.88);font-size:15px;text-decoration:none;cursor:pointer;transition:all .2s}
.btn:hover{color:#4096ff;border-color:#4096ff}
.btn.p{background:#1677ff;border-color:#1677ff;color:#fff;box-shadow:0 2px 0 rgba(5,145,255,.1)}
.btn.p:hover{background:#4096ff;border-color:#4096ff;color:#fff}
.btn:focus-visible{outline:4px solid rgba(5,145,255,.3);outline-offset:1px}
.btn.is-loading{opacity:.65;pointer-events:none;cursor:default}
.btn.is-loading::before{content:"";width:14px;height:14px;border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:fbspin 1s linear infinite;flex:none}
@keyframes fbspin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.btn.is-loading::before{animation:none;border-right-color:currentColor}}
@media (max-width:480px){.card{padding:32px 20px 24px}h1{font-size:20px}.act{flex-direction:column}.btn{width:100%;height:44px}}
"""


def render_no_access(name: str, system_label: str, hub_url: str, logout_url: str, ds_dir: str | Path | None = None) -> str:
    ds_dir = str(ds_dir or (Path(__file__).resolve().parent / "assets" / "ds"))
    n = escape(name or "", quote=True)
    who = (f'<div class="who"><svg class="ai" aria-hidden="true"><use href="#i-user"/></svg><span>当前账号：{n}</span></div>' if n else "")
    logo = _brand_datauri(ds_dir, "logo-blue.png")
    logo_html = f'<img class="logo" src="{logo}" alt="猛士" height="28">' if logo else ""
    fav = _brand_datauri(ds_dir, "icon-32.png")
    fav_html = f'<link rel="icon" type="image/png" sizes="32x32" href="{fav}">' if fav else ""
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">'
        '<title>没有访问权限</title><meta name="robots" content="noindex">'
        f'{fav_html}<meta name="theme-color" content="#1677ff">'
        f'<style>{_CSS}</style></head><body>{_sprite(ds_dir)}'
        '<main class="card" role="main" data-mhero-403="1">'
        f'{logo_html}'
        '<div class="ico"><svg class="ai" aria-hidden="true"><use href="#i-lock"/></svg></div>'
        '<h1>你的账号没有开通本系统的访问权限</h1>'
        f'{who}'
        f'<p class="sub">你还没有被授权使用「{escape(system_label)}」。</p>'
        '<p class="hint">如需使用，请联系管理员开通；也可以先回到黄页，使用你已开通的系统。</p>'
        '<div class="act">'
        f'<a class="btn p" id="back-hub" href="{escape(hub_url, quote=True)}"><svg class="ai" aria-hidden="true"><use href="#i-home"/></svg>返回黄页</a>'
        f'<a class="btn" id="logout-link" href="{escape(logout_url, quote=True)}"><svg class="ai" aria-hidden="true"><use href="#i-logout"/></svg>退出登录</a>'
        '</div></main>'
        '<script>document.addEventListener("click",function(e){var a=e.target.closest&&e.target.closest("a.btn");'
        'if(!a||e.button||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;'
        'a.classList.add("is-loading");a.setAttribute("aria-busy","true");});</script>'
        '</body></html>')
