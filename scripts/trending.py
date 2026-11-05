#!/usr/bin/env python3
"""Daily trending digest for one field (see config.json).

GitHub's API has no "star velocity", so we build it ourselves:
  1. collect a candidate pool via topic searches (top by stars + recently created)
  2. store today's star counts in data/stars/<date>.json
  3. diff against the previous snapshot -> stars gained per day
Plus github.com/trending (sitewide "stars today") as an extra signal.

Output: TRENDING.md (latest), trending/<date>.md (archive), and a GitHub Issue
that @mentions the owner so it lands in their notifications / inbox.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STARS_DIR = ROOT / "data" / "stars"
ARCHIVE = ROOT / "trending"
LATEST = ROOT / "TRENDING.md"

TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
LABEL = "daily-trending"
UA = "sqldata-trending (github actions; stdlib urllib)"
TIMEOUT = 25


# ---------- time ----------
def now_local() -> datetime:
    utc = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        from zoneinfo import ZoneInfo

        return utc.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001
        return utc.astimezone(timezone(timedelta(hours=8)))


# ---------- http ----------
def _request(url: str, method: str = "GET", data: dict | None = None, raw: bool = False):
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if TOKEN and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {TOKEN}"
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace") if raw else json.load(r)


def api(path: str, method: str = "GET", data: dict | None = None, retries: int = 3):
    url = path if path.startswith("http") else f"https://api.github.com/{path.lstrip('/')}"
    for i in range(retries):
        try:
            return _request(url, method, data)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and i < retries - 1:  # rate limited
                wait = int(e.headers.get("Retry-After") or 30)
                print(f"[warn] {e.code} on {url}, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"[warn] {url} -> HTTP {e.code} {e.read()[:200]!r}", file=sys.stderr)
            return None
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {url} -> {e}", file=sys.stderr)
            return None
    return None


# ---------- candidate pool ----------
def _norm(r: dict) -> dict:
    return {
        "stars": r["stargazers_count"],
        "desc": (r.get("description") or "").strip(),
        "lang": r.get("language") or "-",
        "url": r["html_url"],
        "created": r.get("created_at", "")[:10],
        "pushed": r.get("pushed_at", "")[:10],
        "topics": r.get("topics", []),
    }


def search(q: str, per_page: int) -> dict[str, dict]:
    url = f"search/repositories?q={urllib.parse.quote(q)}&sort=stars&order=desc&per_page={per_page}"
    res = api(url) or {}
    time.sleep(2.2)  # search API: 30 req/min
    return {r["full_name"]: _norm(r) for r in res.get("items", [])}


def collect_pool() -> dict[str, dict]:
    since_new = (datetime.now(timezone.utc) - timedelta(days=CFG["new_days"])).date().isoformat()
    since_active = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    pool: dict[str, dict] = {}
    for t in CFG["topics"]:
        pool.update(search(f"topic:{t} pushed:>{since_active}", 100))  # established & active
        pool.update(search(f"topic:{t} created:>{since_new}", 50))  # newcomers
    return pool


# ---------- github.com/trending ----------
def fetch_trending() -> list[dict]:
    try:
        page = _request("https://github.com/trending?since=daily", raw=True)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] trending page -> {e}", file=sys.stderr)
        return []
    out = []
    for art in page.split("<article")[1:]:
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", art, re.S)
        m = re.search(r'href="/([^/"]+/[^/"#?]+)"', h2.group(1) if h2 else art)
        if not m or m.group(1).startswith("sponsors/"):
            continue
        name = m.group(1)
        today = re.search(r"([\d,]+)\s+stars\s+today", art)
        total = re.search(r'/stargazers"[^>]*>.*?([\d,]+)\s*</a>', art, re.S)
        desc = re.search(r'<p class="col-9[^"]*">(.*?)</p>', art, re.S)
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)<', art)
        out.append(
            {
                "name": name,
                "stars_today": int(today.group(1).replace(",", "")) if today else 0,
                "stars": int(total.group(1).replace(",", "")) if total else 0,
                "desc": html.unescape(re.sub(r"<[^>]+>", "", desc.group(1))).strip() if desc else "",
                "lang": lang.group(1).strip() if lang else "-",
                "url": f"https://github.com/{name}",
            }
        )
    return out


def matches_field(name: str, desc: str, topics: list[str] | None = None) -> bool:
    hay = f"{name} {desc} {' '.join(topics or [])}".lower()
    return any(k in hay for k in CFG["keywords"]) or any(t in (topics or []) for t in CFG["topics"])


# ---------- snapshots ----------
def previous_snapshot(today: str) -> tuple[str, dict] | None:
    files = sorted(p for p in STARS_DIR.glob("????-??-??.json") if p.stem < today)
    if not files:
        return None
    p = files[-1]
    return p.stem, json.loads(p.read_text(encoding="utf-8"))


# ---------- render ----------
def esc(s: str, n: int = 80) -> str:
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def table(rows: list[list[str]], header: list[str], align: str) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(align.split(",")) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def render(local: datetime, rising: list[dict], newcomers: list[dict], site: list[dict], meta: dict) -> str:
    field = CFG["field"]
    L = [f"# 📈 {field} · GitHub 涨星日报 {local:%Y-%m-%d}", ""]
    L.append(
        f"生成于 {local:%Y-%m-%d %H:%M} (UTC+8) · 候选池 {meta['pool']} 个仓库 · "
        + (f"对比基线 {meta['prev']}（{meta['days']} 天）" if meta.get("prev") else "首日建立基线，明日起输出 24h 涨星数")
    )
    L.append("")

    L.append(f"## 🚀 {field} 领域涨星最快")
    L.append("")
    if rising:
        rows = [
            [
                str(i),
                f"[{r['name']}]({r['url']})",
                f"+{r['gain']:,}",
                f"{r['stars']:,}",
                r["lang"],
                esc(r["desc"]),
            ]
            for i, r in enumerate(rising, 1)
        ]
        L += table(rows, ["#", "Repo", meta["gain_label"], "Stars", "Lang", "Description"], "---:,---,---:,---:,---,---")
    else:
        L.append("_今天没有可用的涨星数据_")
    L.append("")

    L.append(f"## 🆕 {field} 新秀（{CFG['new_days']} 天内创建，按 Star 排序）")
    L.append("")
    if newcomers:
        rows = [
            [str(i), f"[{r['name']}]({r['url']})", f"{r['stars']:,}", r["created"], r["lang"], esc(r["desc"])]
            for i, r in enumerate(newcomers, 1)
        ]
        L += table(rows, ["#", "Repo", "Stars", "Created", "Lang", "Description"], "---:,---,---:,---,---,---")
    else:
        L.append("_无_")
    L.append("")

    L.append("## 🌍 全站 Trending（今日涨星 Top 10）")
    L.append("")
    if site:
        rows = [
            [
                str(i),
                f"[{r['name']}]({r['url']})" + (" 🎯" if r.get("field") else ""),
                f"+{r['stars_today']:,}",
                f"{r['stars']:,}",
                r["lang"],
                esc(r["desc"]),
            ]
            for i, r in enumerate(site, 1)
        ]
        L += table(rows, ["#", "Repo", "Today", "Stars", "Lang", "Description"], "---:,---,---:,---:,---,---")
        L.append("")
        L.append("🎯 = 命中本领域关键词")
    else:
        L.append("_github.com/trending 今日不可用_")
    L.append("")
    L.append("---")
    L.append(f"领域/关键词在 `config.json` 里改 · 历史见 `trending/` · 原始 star 快照见 `data/stars/`")
    L.append("")
    return "\n".join(L)


# ---------- issue ----------
def publish_issue(title: str, body: str) -> str | None:
    if not (TOKEN and REPO) or os.environ.get("SKIP_ISSUE"):
        print("[info] issue skipped (no token/repo or SKIP_ISSUE set)")
        return None
    api(f"repos/{REPO}/labels", "POST", {"name": LABEL, "color": "0E8A16", "description": "auto daily digest"})
    for it in api(f"repos/{REPO}/issues?labels={LABEL}&state=open&per_page=50") or []:
        api(f"repos/{REPO}/issues/{it['number']}", "PATCH", {"state": "closed", "state_reason": "completed"})
    body = f"{CFG['mention']} 今日榜单 👇\n\n" + body
    if len(body) > 60000:
        body = body[:60000] + "\n\n_(truncated)_"
    res = api(f"repos/{REPO}/issues", "POST", {"title": title, "body": body, "labels": [LABEL]})
    return res.get("html_url") if res else None


# ---------- main ----------
def main() -> int:
    local = now_local()
    today = local.date().isoformat()
    STARS_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE.mkdir(exist_ok=True)

    pool = collect_pool()
    site = fetch_trending()
    for s in site:  # trending repos that match the field join the pool
        s["field"] = matches_field(s["name"], s["desc"])
        if s["field"] and s["name"] not in pool and s["stars"]:
            pool[s["name"]] = {"stars": s["stars"], "desc": s["desc"], "lang": s["lang"], "url": s["url"], "created": "", "pushed": "", "topics": []}
    site_today = {s["name"]: s["stars_today"] for s in site}

    (STARS_DIR / f"{today}.json").write_text(
        json.dumps({k: v["stars"] for k, v in sorted(pool.items())}, indent=0) + "\n", encoding="utf-8"
    )

    prev = previous_snapshot(today)
    meta = {"pool": len(pool), "gain_label": "24h 涨星"}
    rising: list[dict] = []
    if prev:
        prev_date, prev_stars = prev
        days = max(1, (datetime.fromisoformat(today) - datetime.fromisoformat(prev_date)).days)
        meta.update(prev=prev_date, days=days)
        if days > 1:
            meta["gain_label"] = f"日均涨星({days}d)"
        for name, r in pool.items():
            if name in prev_stars:
                gain = round((r["stars"] - prev_stars[name]) / days)
            elif name in site_today:
                gain = site_today[name]
            else:
                continue
            if gain > 0:
                rising.append({"name": name, "gain": gain, **r})
    else:  # first run: only the trending page knows today's velocity
        meta["gain_label"] = "今日涨星"
        for s in site:
            if s["field"]:
                rising.append({"name": s["name"], "gain": s["stars_today"], **{k: s[k] for k in ("stars", "desc", "lang", "url")}})
    rising.sort(key=lambda r: (-r["gain"], -r["stars"]))
    rising = rising[: CFG["top_n"]]

    cutoff = (local - timedelta(days=CFG["new_days"])).date().isoformat()
    newcomers = sorted(
        ({"name": n, **r} for n, r in pool.items() if r["created"] >= cutoff),
        key=lambda r: -r["stars"],
    )[: CFG["top_n"]]

    site = sorted(site, key=lambda s: -s["stars_today"])[:10]

    md = render(local, rising, newcomers, site, meta)
    LATEST.write_text(md, encoding="utf-8")
    (ARCHIVE / f"{today}.md").write_text(md, encoding="utf-8")

    url = publish_issue(f"📈 {CFG['field']} 涨星日报 {today}", md)
    print(f"pool={len(pool)} rising={len(rising)} newcomers={len(newcomers)} site={len(site)} issue={url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
