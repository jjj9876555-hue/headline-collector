#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Xを読む係（headline collector）

  - 5アカウントの新着ヘッドラインを取得して data/headlines.jsonl に溜める
  - 直近 6 / 12 / 24 時間のダイジェスト（data/latest_*h.md）を書き出す
  - COMEX金先物の建玉を取得する（取れない日は「取得できず」と表示するだけ）

判断は一切しない。取得・重複統合・整形だけ。
"""
import html
import json
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ========== 設定（変えたいときはここだけ触れば十分） ==========
X_ACCOUNTS = ["DeItaone", "FirstSquawk", "financialjuice", "Yuto_Headline", "SBILM"]

# Telegram の公開ミラー（ログイン不要）: チャンネル名 -> 対応する X アカウント
# ミラーが見つかったら行を足すだけで二重化できる
TG_CHANNELS = {
    "WalterBloomberg": "DeItaone",
    "firstsquaw": "FirstSquawk",
    "FinancialJuice": "financialjuice",
}

# X の埋め込みエンドポイントが混雑（429）のときに使う予備ルート（Nitter系のRSS）
NITTER_INSTANCES = [
    "https://xcancel.com",
    "https://nitter.net",
    "https://nitter.privacydev.net",
]

# X の取得順（Telegramミラーが無いアカウントを先に。埋め込み窓口は同じIPから連続で叩くと 429 になりやすい）
X_FETCH_ORDER = ["Yuto_Headline", "SBILM", "financialjuice", "DeItaone", "FirstSquawk"]
X_SPACING_SEC = 12          # アカウント間の待ち（秒）
X_429_WAITS = [30, 60]      # 429 が出たときの待ち（秒）。1回目30秒→2回目60秒→それでもだめなら諦める
X_PHASE_BUDGET_SEC = 360    # X 取得全体の上限（秒）。超えたら残りは諦めて先へ進む

KEEP_HOURS = 72             # 溜めておく時間
DIGEST_HOURS = [6, 12, 24]  # 書き出すダイジェストの窓
HIGH_FREQ = ["DeItaone", "FirstSquawk", "financialjuice"]  # 空白チェックの対象
GAP_WARN_MIN = 45           # この分数を超える空白に ⚠ を付ける（週末・米国夜間は自然に空く）
TEXT_MAX = 240              # ダイジェスト1行の最大文字数
# ==============================================================

UTC = timezone.utc
JST = ZoneInfo("Asia/Tokyo")
NY = ZoneInfo("America/New_York")

DATA_DIR = "data"
STORE = os.path.join(DATA_DIR, "headlines.jsonl")
STATUS = os.path.join(DATA_DIR, "last_run.json")
CME_FILE = os.path.join(DATA_DIR, "cme_gold_oi.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ja;q=0.8"})
TIMEOUT = 25

# ダイジェストの先頭に入れる AI への指示（ファイルを丸ごと貼れば要約が出る）
INSTRUCTION = """\
> **AIへの指示（このファイルを丸ごと貼り付けてください）**
> 以下は @DeItaone @FirstSquawk @financialjuice @Yuto_Headline @SBILM の直近{h}時間のヘッドライン一覧です。金（XAUUSD）トレーダー向けに要約してください。
> 出力の順番：①共通（地政学・リスク）→②通貨別 USD→EUR→JPY→GBP→AUD→NZD→CAD→CHF →③原油・金（末尾に「COMEX金先物 建玉」の内容をそのまま転記）→④XAUUSDへの示唆を1行。
> 各セクションは重要度順の箇条書き。該当なしの通貨は「特段なし」と1行。各項目の末尾に情報源アカウント名を付記。一覧にない情報は加えない。
> 「取得状況」に ⚠ や取得エラーがあれば、冒頭に1行で明記する。
> 文体は標準敬語（です・ます調）。経済指標・為替用語は日本語表記（前月比・前年比／前年同月比・前期比・季節調整年率。MoM/YoY/QoQ/SAAR等の英語略称は使わない）。証券種別・市場用語も日本語（ビル→短期国債、BEI→期待インフレ率、タームプレミアム→期間プレミアム）。ティッカーやデータ系列名は原文のまま。
> 比喩的・曖昧な相場表現は禁止。シナリオは「どの価格水準でどうなったら該当か」を数値条件で明示。数値が多く並ぶ比較は表形式にする。
"""


# ---------------------------------------------------------------- 共通
def now_utc():
    return datetime.now(UTC)


def clean_text(t):
    t = html.unescape(t or "")
    t = re.sub(r"\s+", " ", t).strip()
    # ミラー由来の末尾サフィックスを落とす（"|FJ"、"(@FirstSquaw)" など）
    t = re.sub(r"\s*\|\s*FJ\s*$", "", t, flags=re.I)
    t = re.sub(r"\s*\(@\w+\)\s*$", "", t)
    return t.strip()


def norm_key(t):
    """同文判定用のキー（URL・記号・接頭辞を落として先頭120文字）"""
    t = re.sub(r"https?://\S+", "", t or "")
    t = re.sub(r"\|\s*FJ\s*$", "", t.strip(), flags=re.I)
    t = re.sub(r"^(RT @\w+:|\*|BREAKING:?|JUST IN:?)\s*", "", t.strip(), flags=re.I)
    t = re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", "", t.lower())
    return t[:120]


def fmt_jst(iso, fmt="%m/%d %H:%M"):
    return datetime.fromisoformat(iso).astimezone(JST).strftime(fmt)


def fmt_n(n):
    return f"{n:,}" if isinstance(n, int) else "-"


def fmt_signed(n):
    if not isinstance(n, int):
        return "-"
    return f"+{n:,}" if n > 0 else f"{n:,}"


def to_int(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("+", "")
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


# ---------------------------------------------------------------- X（埋め込み用の公開エンドポイント・認証不要）
DEAD_INSTANCES = set()        # この実行中に落ちていた Nitter インスタンス


def fetch_x(handle, deadline):
    """埋め込みルート（429なら待って再試行）→ だめなら Nitter RSS。戻り値: (items, route)"""
    errs = []
    for attempt in range(len(X_429_WAITS) + 1):
        if time.time() > deadline:
            errs.append("syndication skipped (時間切れ)")
            break
        try:
            return fetch_x_syndication(handle), "syndication"
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else None
            errs.append(f"syndication http {code}")
            if code == 429 and attempt < len(X_429_WAITS):
                time.sleep(X_429_WAITS[attempt] + random.uniform(0, 5))
                continue
            break
        except Exception as e:
            errs.append(f"syndication {str(e)[:60]}")
            break
    try:
        items, base = fetch_x_rss(handle)
        return items, f"rss:{base}"
    except Exception as e:
        errs.append(str(e)[:160])
    raise RuntimeError(" / ".join(errs))


def fetch_x_rss(handle):
    last_err = "全インスタンス不通"
    for base in NITTER_INSTANCES:
        if base in DEAD_INSTANCES:
            continue
        try:
            r = S.get(f"{base}/{handle}/rss", timeout=15)
            if r.status_code != 200 or b"<item>" not in r.content:
                last_err = f"{base} http {r.status_code}"
                DEAD_INSTANCES.add(base)
                continue
            items = parse_rss(r.content, handle)
            if items:
                return items, base
            last_err = f"{base} 0件"
        except Exception as e:
            last_err = f"{base} {str(e)[:60]}"
            DEAD_INSTANCES.add(base)
    raise RuntimeError(f"rss failed ({last_err})")


def parse_rss(content, handle):
    root = ET.fromstring(content)
    ns_dc = "{http://purl.org/dc/elements/1.1/}creator"
    items = []
    for it in root.iter("item"):
        link = it.findtext("link") or ""
        m = re.search(r"/status/(\d+)", link)
        if not m:
            continue
        tid = m.group(1)
        pub = it.findtext("pubDate") or ""
        try:
            created = parsedate_to_datetime(pub).astimezone(UTC)
        except Exception:
            continue
        title = it.findtext("title") or ""
        desc = it.findtext("description") or ""
        text = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True) if desc else title
        creator = (it.findtext(ns_dc) or "").lstrip("@") or handle
        if title.startswith("RT by ") or title.startswith("RT @"):
            text = f"RT @{creator}: {text}"
        text = clean_text(re.sub(r"https?://\S*(nitter|xcancel)\S*", "", text))
        if not text:
            continue
        author = handle if not title.startswith("RT") else creator
        items.append({
            "id": f"x:{tid}",
            "source": "x-rss",
            "account": handle,
            "author": author,
            "time_utc": created.isoformat(timespec="seconds"),
            "text": text,
            "url": f"https://x.com/{author}/status/{tid}",
        })
    return items


def fetch_x_syndication(handle):
    url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{handle}"
    r = S.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("__NEXT_DATA__ が見つかりません（ブロックか仕様変更）")
    data = json.loads(m.group(1))
    timeline = ((data.get("props") or {}).get("pageProps") or {}).get("timeline") or {}
    entries = timeline.get("entries") or []
    items = []
    for e in entries:
        tw = (e.get("content") or {}).get("tweet")
        if not tw:
            continue
        rt = tw.get("retweeted_status")
        src = rt or tw
        text = src.get("full_text") or src.get("text") or ""
        for u in ((src.get("entities") or {}).get("urls") or []):
            if u.get("url") and u.get("expanded_url"):
                text = text.replace(u["url"], u["expanded_url"])
        text = re.sub(r"https?://t\.co/\w+", "", text)
        if rt:
            text = f"RT @{(rt.get('user') or {}).get('screen_name', '')}: {text}"
        text = clean_text(text)
        tid = str(tw.get("id_str") or tw.get("id") or "")
        if not text or not tid:
            continue
        created = datetime.strptime(tw["created_at"], "%a %b %d %H:%M:%S %z %Y").astimezone(UTC)
        author = (tw.get("user") or {}).get("screen_name") or handle
        items.append({
            "id": f"x:{tid}",
            "source": "x",
            "account": handle,
            "author": author,
            "time_utc": created.isoformat(timespec="seconds"),
            "text": text,
            "url": f"https://x.com/{author}/status/{tid}",
        })
    return items


# ---------------------------------------------------------------- Telegram（公開プレビュー t.me/s/ ・認証不要）
def fetch_tg(channel, account, known_ids=frozenset(), max_pages=4, backfill_hours=6):
    items, before, pages = [], None, 0
    cutoff = now_utc() - timedelta(hours=backfill_hours)
    while pages < max_pages:
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        r = S.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        msgs = soup.select("div.tgme_widget_message[data-post]")
        if not msgs:
            if pages == 0:
                raise RuntimeError("メッセージが読めません（ブロックか仕様変更）")
            break
        page_items = []
        for m in msgs:
            mid = (m.get("data-post") or "").rsplit("/", 1)[-1]
            tnode = m.select_one("time[datetime]")
            txt = m.select_one("div.tgme_widget_message_text")
            if not mid.isdigit() or not tnode or not txt:
                continue
            ts = datetime.fromisoformat(tnode["datetime"].replace("Z", "+00:00")).astimezone(UTC)
            text = clean_text(txt.get_text(" ", strip=True))
            if not text:
                continue
            page_items.append({
                "id": f"tg:{channel}:{mid}",
                "source": "telegram",
                "account": account,
                "author": channel,
                "time_utc": ts.isoformat(timespec="seconds"),
                "text": text,
                "url": f"https://t.me/{channel}/{mid}",
            })
        items.extend(page_items)
        pages += 1
        if not page_items:
            break
        oldest = min(page_items, key=lambda x: x["time_utc"])
        # ページ全部が未知で、まだ窓の中なら、さらに前をたどる（取得が遅れていたときの穴埋め）
        all_new = all(it["id"] not in known_ids for it in page_items)
        if all_new and datetime.fromisoformat(oldest["time_utc"]) > cutoff:
            before = int(oldest["id"].rsplit(":", 1)[-1])
            continue
        break
    return items


# ---------------------------------------------------------------- CME 金先物 建玉（product 437）
CME_DETAIL = {"last": None}


def fetch_cme():
    hdr = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.cmegroup.com/markets/metals/precious/gold.volume.html",
        "Origin": "https://www.cmegroup.com",
        "X-Requested-With": "XMLHttpRequest",
    }
    today = now_utc()
    for back in range(0, 8):
        d = (today - timedelta(days=back)).strftime("%Y%m%d")
        for typ, label in (("F", "確報"), ("P", "速報")):
            url = (f"https://www.cmegroup.com/CmeWS/mvc/Volume/Details/F/437/{d}/{typ}"
                   f"?tradeDate={d}&pageSize=500&_t={int(time.time())}")
            try:
                r = S.get(url, headers=hdr, timeout=TIMEOUT)
                if r.status_code != 200:
                    CME_DETAIL["last"] = f"{d}/{typ} http {r.status_code}"
                    if r.status_code in (403, 429):
                        return None  # ブロックされている。日付を変えても同じなので打ち切り
                    continue
                j = r.json()
            except Exception as e:
                CME_DETAIL["last"] = f"{d}/{typ} {type(e).__name__}: {str(e)[:80]}"
                continue
            tot = j.get("totals") or {}
            oi = to_int(tot.get("atClose"))
            if not oi:
                continue
            center = None
            for m in (j.get("monthData") or []):
                c = to_int(m.get("atClose"))
                if c and (center is None or c > center["oi"]):
                    center = {
                        "month": str(m.get("month") or m.get("monthID") or ""),
                        "oi": c,
                        "change": to_int(m.get("change")),
                    }
            return {
                "trade_date": d,
                "type": label,
                "oi": oi,
                "change": to_int(tot.get("change")),
                "volume": to_int(tot.get("totalVolume")),
                "center": center,
                "fetched_utc": today.isoformat(timespec="seconds"),
            }
    return None


def fetch_gold_direction(trade_date):
    """Yahoo の GC=F 日足から、取引日の終値方向（↑/↓）を取る。ベストエフォート。"""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF?range=1mo&interval=1d"
    r = S.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    days = [(datetime.fromtimestamp(t, tz=UTC).astimezone(NY).strftime("%Y%m%d"), c)
            for t, c in zip(ts, closes) if c is not None]
    idx = None
    for i, (d, _) in enumerate(days):
        if d <= trade_date:
            idx = i
    if idx is None or idx == 0:
        return None
    d, c = days[idx]
    prev = days[idx - 1][1]
    return {
        "date": d,
        "close": round(c, 2),
        "prev_close": round(prev, 2),
        "dir": "↑" if c > prev else "↓" if c < prev else "→",
    }


def judge(price_dir, total_change, center_change):
    """価格↑建玉↑=新規買い / 価格↑建玉↓=買い戻し / 価格↓建玉↑=新規売り / 価格↓建玉↓=ロング投げ"""
    if price_dir not in ("↑", "↓"):
        return "保留（価格方向が取得できず）"
    basis, note = total_change, ""
    if (isinstance(total_change, int) and isinstance(center_change, int)
            and (total_change >= 0) != (center_change >= 0)):
        basis, note = center_change, "（総量と中心限月の符号が逆＝ロール期の可能性。中心限月基準で判定）"
    if not isinstance(basis, int):
        return "保留（建玉の前日比なし）"
    if basis == 0:
        return "保留（建玉横ばい）"
    table = {("↑", True): "新規買い", ("↑", False): "買い戻し",
             ("↓", True): "新規売り", ("↓", False): "ロング投げ"}
    return table[(price_dir, basis > 0)] + note


def cme_lines(cme):
    if not cme:
        return ["- 取得できず（CME側で未掲載またはアクセス不可）。判定は保留。"]
    out = []
    d = cme["trade_date"]
    stale = "（前回取得分・今回は取得できず）" if cme.get("stale") else ""
    out.append(f"- 取引日 {d[:4]}-{d[4:6]}-{d[6:]}・{cme['type']}{stale}")
    out.append(f"- 建玉残高 {fmt_n(cme['oi'])}枚（前日比 {fmt_signed(cme['change'])}）、出来高 {fmt_n(cme['volume'])}枚")
    c = cme.get("center")
    if c:
        out.append(f"- 中心限月 {c['month']}: {fmt_n(c['oi'])}枚（前日比 {fmt_signed(c['change'])}）")
    p = cme.get("price")
    if p:
        out.append(f"- 価格方向 {p['dir']}（Yahoo日足 {p['date']} 終値 {p['close']} vs 前日 {p['prev_close']}）")
    else:
        out.append("- 価格方向 取得できず")
    out.append(f"- 判定: {judge(p['dir'] if p else None, cme.get('change'), c['change'] if c else None)}")
    return out


# ---------------------------------------------------------------- 蓄積
def load_store():
    d = {}
    if os.path.exists(STORE):
        with open(STORE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    it = json.loads(line)
                    d[it["id"]] = it
                except Exception:
                    pass
    return d


def add_items(store, items):
    n = 0
    for it in items:
        if it["id"] not in store:
            store[it["id"]] = it
            n += 1
    return n


def save_store(store):
    cutoff = now_utc() - timedelta(hours=KEEP_HOURS)
    keep = [it for it in store.values() if datetime.fromisoformat(it["time_utc"]) >= cutoff]
    keep.sort(key=lambda x: x["time_utc"])
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STORE, "w", encoding="utf-8") as f:
        for it in keep:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return keep


# ---------------------------------------------------------------- ダイジェスト
def build_digest(items, hours, status, cme):
    now = now_utc()
    start = now - timedelta(hours=hours)
    win = sorted((it for it in items if datetime.fromisoformat(it["time_utc"]) >= start),
                 key=lambda x: x["time_utc"])

    # 同文を1行に統合し、アカウントを併記
    groups, order = {}, []
    for it in win:
        k = norm_key(it["text"]) or it["id"]
        if k not in groups:
            groups[k] = {"time_utc": it["time_utc"], "text": it["text"], "accounts": []}
            order.append(k)
        if it["account"] not in groups[k]["accounts"]:
            groups[k]["accounts"].append(it["account"])
    rows = [groups[k] for k in order]

    L = [INSTRUCTION.format(h=hours)]
    L.append(f"# ヘッドライン 直近{hours}時間")
    L.append(f"生成: {now.astimezone(JST).strftime('%Y-%m-%d %H:%M')} JST／対象: "
             f"{start.astimezone(JST).strftime('%m/%d %H:%M')} 〜 "
             f"{now.astimezone(JST).strftime('%m/%d %H:%M')} JST（時刻はすべて日本時間）")
    L.append("")
    L.append("## 取得状況")
    L.append("| アカウント | 件数 | 最古 | 最新 | 最大空白 |")
    L.append("|---|---|---|---|---|")
    for acc in X_ACCOUNTS:
        # X と Telegram で同文が二重に入るので、アカウント内でも同文は1件に数える
        seen, ts = set(), []
        for it in win:
            if it["account"] != acc:
                continue
            k = norm_key(it["text"]) or it["id"]
            if k in seen:
                continue
            seen.add(k)
            ts.append(datetime.fromisoformat(it["time_utc"]))
        ts.sort()
        if not ts:
            L.append(f"| {acc} | 0 | - | - | - |")
            continue
        gap_txt = "-"
        if len(ts) >= 2:
            gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
            g = max(gaps)
            gi = gaps.index(g)
            mins = int(g.total_seconds() // 60)
            gap_txt = (f"{mins}分（{ts[gi].astimezone(JST).strftime('%H:%M')}→"
                       f"{ts[gi + 1].astimezone(JST).strftime('%H:%M')}）")
            if acc in HIGH_FREQ and mins > GAP_WARN_MIN:
                gap_txt = "⚠ " + gap_txt
        L.append(f"| {acc} | {len(ts)} | {ts[0].astimezone(JST).strftime('%m/%d %H:%M')} | "
                 f"{ts[-1].astimezone(JST).strftime('%m/%d %H:%M')} | {gap_txt} |")
    src = status.get("sources", {})
    ok_x = sum(1 for k, v in src.items() if k.startswith("x:") and v.get("ok"))
    ok_tg = sum(1 for k, v in src.items() if k.startswith("tg:") and v.get("ok"))
    errs = [k for k, v in src.items() if not v.get("ok")]
    routes = {}
    for k, v in src.items():
        if k.startswith("x:") and v.get("ok"):
            rname = str(v.get("route", "?")).split(":")[0]
            routes[rname] = routes.get(rname, 0) + 1
    route_txt = "・".join(f"{k} {n}" for k, n in routes.items())
    L.append("")
    L.append(f"- 今回の取得: X {ok_x}/{len(X_ACCOUNTS)} 成功" + (f"（経路: {route_txt}）" if route_txt else "")
             + f"、Telegramミラー {ok_tg}/{len(TG_CHANNELS)} 成功"
             + (f"。エラー: {'、'.join(errs)}" if errs else ""))
    L.append(f"- 統合後 {len(rows)} 行（統合前 {len(win)} 件）。同文は1行にまとめ、アカウントを併記しています")
    L.append(f"- ⚠ は {GAP_WARN_MIN} 分超の空白（欠落の可能性）。週末・米国夜間は自然に空きます")
    L.append("")
    L.append("## COMEX金先物 建玉")
    L.extend(cme_lines(cme))
    L.append("")
    L.append("## ヘッドライン（時刻順）")
    if not rows:
        L.append("（この時間帯の取得なし）")
    for g in rows:
        text = g["text"]
        if len(text) > TEXT_MAX:
            text = text[:TEXT_MAX] + "…"
        L.append(f"- {fmt_jst(g['time_utc'])} [{'/'.join(g['accounts'])}] {text}")
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- main
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    store = load_store()
    known = set(store.keys())
    status = {"run_utc": now_utc().isoformat(timespec="seconds"), "sources": {}}

    deadline = time.time() + X_PHASE_BUDGET_SEC
    order = [h for h in X_FETCH_ORDER if h in X_ACCOUNTS] + [h for h in X_ACCOUNTS if h not in X_FETCH_ORDER]
    last_failed_429 = False
    for i, h in enumerate(order):
        if i:
            time.sleep(X_SPACING_SEC + (30 if last_failed_429 else 0))
        try:
            items, route = fetch_x(h, deadline)
            status["sources"][f"x:{h}"] = {"ok": True, "route": route, "fetched": len(items), "new": add_items(store, items)}
            last_failed_429 = False
        except Exception as e:
            msg = str(e)[:200]
            status["sources"][f"x:{h}"] = {"ok": False, "error": msg}
            last_failed_429 = "429" in msg

    for ch, acc in TG_CHANNELS.items():
        try:
            items = fetch_tg(ch, acc, known_ids=known)
            status["sources"][f"tg:{ch}"] = {"ok": True, "fetched": len(items), "new": add_items(store, items)}
        except Exception as e:
            status["sources"][f"tg:{ch}"] = {"ok": False, "error": str(e)[:200]}
        time.sleep(1)

    kept = save_store(store)
    status["stored"] = len(kept)

    cme = None
    try:
        cme = fetch_cme()
        if cme:
            try:
                cme["price"] = fetch_gold_direction(cme["trade_date"])
            except Exception as e:
                cme["price"] = None
                cme["price_error"] = str(e)[:120]
            with open(CME_FILE, "w", encoding="utf-8") as f:
                json.dump(cme, f, ensure_ascii=False, indent=1)
        elif os.path.exists(CME_FILE):
            with open(CME_FILE, encoding="utf-8") as f:
                cme = json.load(f)
            cme["stale"] = True
    except Exception as e:
        status["cme_error"] = str(e)[:200]
    status["cme"] = None if not cme else {"trade_date": cme["trade_date"], "type": cme["type"],
                                          "stale": bool(cme.get("stale"))}
    status["cme_detail"] = CME_DETAIL["last"]

    for h in DIGEST_HOURS:
        with open(os.path.join(DATA_DIR, f"latest_{h}h.md"), "w", encoding="utf-8") as f:
            f.write(build_digest(kept, h, status, cme))

    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)
    print(json.dumps(status, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
