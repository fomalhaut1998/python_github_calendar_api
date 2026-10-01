# -*- coding: UTF-8 -*-
# 修订说明（2026-10）：
# GitHub 把个人主页的贡献日历改成异步加载之后，老版本抓 https://github.com/<user> 的两条正则全部失效，
# datadate 抓成空列表，zip(*[]) 抛 ValueError，Vercel 就返回 FUNCTION_INVOCATION_FAILED（站点上表现为日历永远转圈）。
# 这里改成抓 ./users/<user>/contributions 片段，并按新的 DOM 结构解析。
import json
import re
from http.server import BaseHTTPRequestHandler

import requests

# GitHub 对 python-requests 的默认 UA 是放行的（实测 200 / 225568 字节），
# 这里仍然带上浏览器 UA 作为保险，避免哪天上游开始挑 UA。
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 每个格子长这样（注意属性顺序是 data-date -> id -> data-level）：
# <td tabindex="0" ... data-date="2026-10-01" id="contribution-day-component-4-52" data-level="0" ...>
# 旧的 data-date="(.*?)" data-level 会一直吃到 data-level，把 id 之类的尾巴也吞进去。
DATE_RE = re.compile(r'data-date="([^"]+)"[^>]*id="([^"]+)"')

# 每天的计数以前是 <span class="sr-only">4 contributions</span>（现在只剩 5 处，不再是每天一个），
# 现在搬进了 tool-tip：<tool-tip for="contribution-day-component-4-52">4 contributions on October 1st.</tool-tip>
# 当天没有贡献时写的是 "No contributions on ..."。
TIP_RE = re.compile(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]*)</tool-tip>')


def list_split(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def getdata(name):
    resp = requests.get(
        "https://github.com/users/" + name + "/contributions",
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.text

    cells = DATE_RE.findall(data)
    tips = dict(TIP_RE.findall(data))

    if not cells:
        raise ValueError("没有从 GitHub 页面解析到贡献格子（页面 %d 字节）" % len(data))

    datalist = []
    for date, cell_id in cells:
        tip = tips.get(cell_id, "")
        head = tip.split(" ")[0] if tip else ""
        datalist.append({
            "date": date,
            "count": int(head) if head.isdigit() else 0,
        })

    # 片段里的 DOM 顺序是按「星期几」分行的（同一行的日期相差 7 天），必须先按日期排成时间序，
    # 后面 list_split(..., 7) 才能切出正确的 53 周（首周 7 天 …… 末周 5 天）。
    datalist.sort(key=lambda item: item["date"])

    return {
        "total": sum(item["count"] for item in datalist),
        "contributions": list_split(datalist, 7),
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 用法：/api?<github-user>
        user = self.path.split("?", 1)[1].split("&")[0].strip() if "?" in self.path else ""

        if not user:
            self._json(400, {"error": "缺少用户名，用法：/api?<github-user>"})
            return

        try:
            data = getdata(user)
        except Exception as exc:
            # 不要再让 Vercel 甩一句 FUNCTION_INVOCATION_FAILED，
            # 把真正的异常名和消息写进响应体（同时也会出现在 Vercel 的 Logs 里）。
            self._json(502, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return

        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        # 贡献数据一天最多变几次：让 CDN 缓存 1 小时，回源给 GitHub 的压力降两个数量级，
        # GitHub 抽风时也能靠 stale-while-revalidate 继续供数。
        self.send_header("Cache-Control", "public, s-maxage=3600, stale-while-revalidate=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
