# -*- coding: UTF-8 -*-
# 修订说明（2026-10）v2：
# v1 只改了正则，部署后仍然返回 Vercel 的 FUNCTION_INVOCATION_FAILED —— 说明异常发生在 do_GET 之前
# （最可能是 import requests 失败 / 依赖没装上），v1 的 try/except 兜不住，所以看不到真正原因。
# v2 做三件事：
#   1) 全部改用标准库（urllib），彻底不再依赖 requests，requirements.txt 有没有都无所谓；
#   2) 所有分支统一走一个出口，任何异常都变成可读的 JSON，不再出现 Vercel 的通用 500 页；
#   3) 解析失败时把「页面有多大、data-date 出现几次、开头长什么样」一起返回，便于远程定位。
import json
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 每个格子长这样（属性顺序是 data-date -> id -> data-level）：
# <td tabindex="0" ... data-date="2026-10-01" id="contribution-day-component-4-52" data-level="0" ...>
DATE_RE = re.compile(r'data-date="([^"]+)"[^>]*id="([^"]+)"')

# 每天的计数从 <span class="sr-only">4 contributions</span> 搬进了 tool-tip：
# <tool-tip for="contribution-day-component-4-52">4 contributions on October 1st.</tool-tip>
TIP_RE = re.compile(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]*)</tool-tip>')

CONTRIBUTIONS_URL = "https://github.com/users/%s/contributions"


def list_split(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def fetch(url):
    req = Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


def parse(data):
    cells = DATE_RE.findall(data)
    tips = dict(TIP_RE.findall(data))

    if not cells:
        raise ValueError(
            "页面里没有贡献格子：%d 字节 | data-date=%d 次 | tool-tip=%d 个 | 开头：%s"
            % (
                len(data),
                data.count("data-date="),
                data.count("<tool-tip"),
                re.sub(r"\s+", " ", data[:200]),
            )
        )

    datalist = []
    for date, cell_id in cells:
        tip = tips.get(cell_id, "")
        head = tip.split(" ")[0] if tip else ""
        datalist.append({"date": date, "count": int(head) if head.isdigit() else 0})

    # 片段里的 DOM 顺序是按「星期几」分行的（同一行的日期相差 7 天），
    # 必须先按日期排成时间序，list_split(..., 7) 才能切出正确的 53 周。
    datalist.sort(key=lambda item: item["date"])

    return {
        "total": sum(item["count"] for item in datalist),
        "contributions": list_split(datalist, 7),
    }


def getdata(name):
    data = fetch(CONTRIBUTIONS_URL % name)
    return parse(data)


def diagnose(name):
    info = {"python": sys.version.replace("\n", " "), "platform": sys.platform}
    url = CONTRIBUTIONS_URL % name
    try:
        data = fetch(url)
        info["fetch"] = "ok"
        info["bytes"] = len(data)
        info["data_date_hits"] = data.count("data-date=")
        info["tooltip_hits"] = data.count("<tool-tip")
        info["head"] = re.sub(r"\s+", " ", data[:200])
    except HTTPError as exc:
        info["fetch"] = "HTTPError %s" % exc.code
    except URLError as exc:
        info["fetch"] = "URLError %s" % exc.reason
    except Exception as exc:
        info["fetch"] = "%s: %s" % (type(exc).__name__, exc)
    return info


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            status, payload, cache = self.route()
        except BaseException as exc:
            status, cache = 502, False
            payload = {
                "error": "%s: %s" % (type(exc).__name__, exc),
                "trace": traceback.format_exc()[-1500:],
            }
        self._json(status, payload, cache)

    def route(self):
        # 用法：/api?<github-user>，另有 /api?__debug 做自检
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        user = query.split("&")[0].strip().split("/")[0]

        if not user:
            return 400, {"error": "缺少用户名，用法：/api?<github-user>"}, False
        if user == "__debug":
            return 200, diagnose("fomalhaut1998"), False
        return 200, getdata(user), True

    def _json(self, status, payload, cache):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        if cache:
            # 贡献数据一天最多变几次：让 CDN 缓存 1 小时，回源给 GitHub 的压力降两个数量级，
            # GitHub 抽风时也能靠 stale-while-revalidate 继续供数。
            self.send_header("Cache-Control", "public, s-maxage=3600, stale-while-revalidate=86400")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
