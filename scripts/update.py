#!/usr/bin/env python3
"""Daily Pulse: fetch a few public data sources and refresh README + data/ snapshots.

Sources (all keyless, each one fails independently):
  - Hacker News top stories
  - GitHub: most-starred repos created in the last 7 days
  - FX rates USD -> CNY/EUR/JPY/GBP (ECB via frankfurter.app)
  - BTC / ETH price in USD (CoinGecko)
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
README = ROOT / "README.md"
FX_HISTORY = DATA / "fx-history.csv"

UA = "daily-pulse (github actions; stdlib urllib)"
TIMEOUT = 20


def now_times() -> tuple[datetime, datetime]:
    utc = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        from zoneinfo import ZoneInfo

        local = utc.astimezone(ZoneInfo("Asia/Shanghai"))
    except Exception:  # tzdata missing
        local = utc.astimezone(timezone(timedelta(hours=8)))
    return utc, local


def fetch_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {url} -> {e}", file=sys.stderr)
        return None


# ---------- sources ----------
def hacker_news(n: int = 10) -> list[dict]:
    ids = fetch_json("https://hacker-news.firebaseio.com/v0/topstories.json") or []
    out = []
    for sid in ids[:n]:
        it = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json")
        if not it:
            continue
        out.append(
            {
                "title": it.get("title", ""),
                "score": it.get("score", 0),
                "comments": it.get("descendants", 0),
                "url": it.get("url") or f"https://news.ycombinator.com/item?id={sid}",
                "hn": f"https://news.ycombinator.com/item?id={sid}",
            }
        )
    return out


def github_new_repos(n: int = 10) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
    q = urllib.parse.quote(f"created:>{since} stars:>50")
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page={n}"
    headers = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    res = fetch_json(url, headers) or {}
    return [
        {
            "name": r["full_name"],
            "stars": r["stargazers_count"],
            "lang": r.get("language") or "-",
            "desc": (r.get("description") or "").strip()[:90],
            "url": r["html_url"],
        }
        for r in res.get("items", [])
    ]


def fx_rates() -> dict:
    res = fetch_json("https://api.frankfurter.app/latest?from=USD&to=CNY,EUR,JPY,GBP") or {}
    return {"date": res.get("date"), "rates": res.get("rates", {})}


def crypto() -> dict:
    res = (
        fetch_json(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
        )
        or {}
    )
    return {
        "btc": res.get("bitcoin", {}).get("usd"),
        "btc_24h": res.get("bitcoin", {}).get("usd_24h_change"),
        "eth": res.get("ethereum", {}).get("usd"),
        "eth_24h": res.get("ethereum", {}).get("usd_24h_change"),
    }


# ---------- persistence ----------
def save_snapshot(snap: dict, utc: datetime) -> Path:
    DATA.mkdir(exist_ok=True)
    p = DATA / f"{utc:%Y-%m-%d-%H%M}.json"
    p.write_text(json.dumps(snap, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def append_fx_history(snap: dict, utc: datetime) -> None:
    if not snap["fx"]["rates"] and snap["crypto"].get("btc") is None:
        return  # nothing worth recording this run
    new = not FX_HISTORY.exists()
    with FX_HISTORY.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["utc", "usd_cny", "usd_eur", "usd_jpy", "usd_gbp", "btc_usd", "eth_usd"])
        r = snap["fx"]["rates"]
        c = snap["crypto"]
        w.writerow(
            [utc.isoformat(), r.get("CNY"), r.get("EUR"), r.get("JPY"), r.get("GBP"), c.get("btc"), c.get("eth")]
        )


def stats() -> dict:
    files = sorted(DATA.glob("????-??-??-????.json"))
    days = sorted({f.name[:10] for f in files})
    streak = 0
    if days:
        cur = datetime.strptime(days[-1], "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        if (today - cur).days <= 1:
            dset = set(days)
            while cur.isoformat() in dset:
                streak += 1
                cur -= timedelta(days=1)
    return {"snapshots": len(files), "days": len(days), "first": days[0] if days else "-", "streak": streak}


# ---------- README ----------
def fmt_num(x, nd=2):
    return "-" if x is None else f"{x:,.{nd}f}"


def fmt_pct(x):
    return "-" if x is None else f"{x:+.1f}%"


def render_readme(snap: dict, st: dict, utc: datetime, local: datetime) -> str:
    L: list[str] = []
    L.append("# Daily Pulse")
    L.append("")
    L.append(
        "Twice-daily snapshot of Hacker News, new GitHub repos, FX rates and crypto, "
        "refreshed automatically by GitHub Actions (08:17 / 20:17 Asia/Shanghai)."
    )
    L.append("")
    L.append(f"**Last updated:** {local:%Y-%m-%d %H:%M} (UTC+8) · `{utc:%Y-%m-%d %H:%M}Z`  ")
    L.append(
        f"**Stats:** {st['snapshots']} snapshots · {st['days']} days tracked · "
        f"current streak {st['streak']} day(s) · since {st['first']}"
    )
    L.append("")
    L.append("📈 **Daily trending digest:** [TRENDING.md](TRENDING.md) · archive in `trending/` · a GitHub Issue is opened every morning")
    L.append("")

    L.append("## Markets")
    L.append("")
    r = snap["fx"]["rates"]
    c = snap["crypto"]
    L.append("| Pair | Value | | Asset | USD | 24h |")
    L.append("|---|---:|---|---|---:|---:|")
    L.append(f"| USD/CNY | {fmt_num(r.get('CNY'), 4)} | | BTC | {fmt_num(c.get('btc'), 0)} | {fmt_pct(c.get('btc_24h'))} |")
    L.append(f"| USD/EUR | {fmt_num(r.get('EUR'), 4)} | | ETH | {fmt_num(c.get('eth'), 0)} | {fmt_pct(c.get('eth_24h'))} |")
    L.append(f"| USD/JPY | {fmt_num(r.get('JPY'), 2)} | | | | |")
    L.append(f"| USD/GBP | {fmt_num(r.get('GBP'), 4)} | | | | |")
    L.append("")
    L.append(f"FX reference date (ECB): {snap['fx'].get('date') or '-'} · history in `data/fx-history.csv`")
    L.append("")

    L.append("## Hacker News · Top 10")
    L.append("")
    if snap["hn"]:
        L.append("| # | Story | Points | Comments |")
        L.append("|---:|---|---:|---:|")
        for i, s in enumerate(snap["hn"], 1):
            title = s["title"].replace("|", "\\|")
            L.append(f"| {i} | [{title}]({s['url']}) | {s['score']} | [{s['comments']}]({s['hn']}) |")
    else:
        L.append("_unavailable this run_")
    L.append("")

    L.append("## GitHub · Hottest repos created in the last 7 days")
    L.append("")
    if snap["github"]:
        L.append("| # | Repo | Stars | Lang | Description |")
        L.append("|---:|---|---:|---|---|")
        for i, g in enumerate(snap["github"], 1):
            desc = g["desc"].replace("|", "\\|")
            L.append(f"| {i} | [{g['name']}]({g['url']}) | {g['stars']:,} | {g['lang']} | {desc} |")
    else:
        L.append("_unavailable this run_")
    L.append("")
    L.append("---")
    L.append("Raw snapshots live in `data/` (one JSON per run). Script: `scripts/update.py`.")
    L.append("")
    return "\n".join(L)


def main() -> int:
    utc, local = now_times()
    snap = {
        "utc": utc.isoformat(),
        "local": local.isoformat(),
        "hn": hacker_news(),
        "github": github_new_repos(),
        "fx": fx_rates(),
        "crypto": crypto(),
    }
    p = save_snapshot(snap, utc)
    append_fx_history(snap, utc)
    st = stats()
    README.write_text(render_readme(snap, st, utc, local), encoding="utf-8")
    ok = sum(bool(snap[k]) for k in ("hn", "github")) + bool(snap["fx"]["rates"]) + bool(snap["crypto"]["btc"])
    print(f"wrote {p.relative_to(ROOT)} · sources ok: {ok}/4 · {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
