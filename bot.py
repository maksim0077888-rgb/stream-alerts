#!/usr/bin/env python3
"""YouTube -> Telegram: оповещения о трансляциях.

Один запуск = одна проверка (так работает на GitHub Actions).
Для запуска на своём компе: python bot.py --loop 120  (проверка каждые 120 сек).
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ["TG_CHAT"]
YT_KEY = os.environ["YT_KEY"]

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")
CHANNELS_FILE = os.path.join(BASE, "channels.txt")


# ---------- сеть ----------

def http(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def yt(endpoint, **params):
    params["key"] = YT_KEY
    url = "https://www.googleapis.com/youtube/v3/%s?%s" % (endpoint, urllib.parse.urlencode(params))
    return json.loads(http(url))


def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    try:
        return json.loads(http("https://api.telegram.org/bot%s/%s" % (TG_TOKEN, method), data))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        if "message is not modified" in body:
            return {"ok": True}
        print("Telegram error:", method, body)
        return {"ok": False}


# ---------- файлы ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("ids", {})   # ссылка из channels.txt -> ID канала
    s.setdefault("live", {})  # ID видео -> данные поста
    return s


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def load_channels():
    out = []
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                name, ref = [x.strip() for x in line.split("|", 1)]
            else:
                name, ref = "", line
            out.append((name, urllib.parse.unquote(ref)))
    return out


# ---------- YouTube ----------

def resolve(ref, cache):
    if ref in cache:
        return cache[ref]
    m = re.search(r"(UC[\w-]{22})", ref)
    if m:
        cid = m.group(1)
    else:
        m = re.search(r"@([\w.\-]+)", ref)
        handle = m.group(1) if m else ref
        items = yt("channels", part="id", forHandle="@" + handle).get("items", [])
        if not items:
            print("Канал не найден:", ref)
            return None
        cid = items[0]["id"]
    cache[ref] = cid
    return cid


def recent_videos(cid):
    r = yt("playlistItems", part="contentDetails", playlistId="UU" + cid[2:], maxResults=10)
    return [it["contentDetails"]["videoId"] for it in r.get("items", [])]


def best_thumb(sn):
    th = sn.get("thumbnails", {})
    for k in ("maxres", "standard", "high", "medium", "default"):
        if k in th:
            return th[k]["url"]
    return None


# ---------- текст постов ----------

def parse_t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_dur(sec):
    h, m = divmod(max(0, int(sec)) // 60, 60)
    return "%d ч %d мин" % (h, m) if h else "%d мин" % m


def caption_live(t, now):
    dur = fmt_dur((now - parse_t(t["start"])).total_seconds())
    stats = ("👁 %d · " % t["viewers"]) if t["has_viewers"] else ""
    return (
        "🔴 <b>%s</b> в эфире\n%s\n\n%sидёт %s\n<a href=\"https://youtu.be/%s\">Смотреть стрим</a>"
        % (html.escape(t["name"]), html.escape(t["title"]), stats, dur, t["vid"])
    )


def caption_end(t, end):
    dur = fmt_dur((end - parse_t(t["start"])).total_seconds())
    peak = (" · пик 👁 %d" % t["peak"]) if t["has_viewers"] else ""
    return (
        "⚫️ <b>%s</b>: стрим закончился\n%s\n\n⏱ %s%s\n<a href=\"https://youtu.be/%s\">Запись</a>"
        % (html.escape(t["name"]), html.escape(t["title"]), dur, peak, t["vid"])
    )


def post(t, cap, thumb):
    if thumb:
        r = tg("sendPhoto", chat_id=TG_CHAT, photo=thumb, caption=cap, parse_mode="HTML")
        if r.get("ok"):
            return r["result"]["message_id"], True
    r = tg("sendMessage", chat_id=TG_CHAT, text=cap, parse_mode="HTML")
    if r.get("ok"):
        return r["result"]["message_id"], False
    return None, False


def edit(t, cap):
    if cap == t.get("cap"):
        return
    if t["photo"]:
        r = tg("editMessageCaption", chat_id=TG_CHAT, message_id=t["msg"], caption=cap, parse_mode="HTML")
    else:
        r = tg("editMessageText", chat_id=TG_CHAT, message_id=t["msg"], text=cap,
               parse_mode="HTML", disable_web_page_preview="true")
    if r.get("ok"):
        t["cap"] = cap


# ---------- основная проверка ----------

def run_once():
    state = load_state()
    now = datetime.now(timezone.utc)

    cand = {}  # ID видео -> (ник, ID канала)
    cids = set()
    for name, ref in load_channels():
        try:
            cid = resolve(ref, state["ids"])
            if not cid:
                continue
            cids.add(cid)
            for vid in recent_videos(cid):
                cand[vid] = (name, cid)
        except Exception as e:
            print("Ошибка канала", ref, e)

    # стримы каналов, которые убрали из списка, просто забываем
    for vid in list(state["live"]):
        if state["live"][vid]["cid"] not in cids:
            del state["live"][vid]
    for vid, t in state["live"].items():
        cand.setdefault(vid, (t["name"], t["cid"]))

    videos = {}
    ids = list(cand)
    try:
        for i in range(0, len(ids), 50):
            r = yt("videos", part="snippet,liveStreamingDetails", id=",".join(ids[i:i + 50]))
            for it in r.get("items", []):
                videos[it["id"]] = it
    except Exception as e:
        print("YouTube не ответил, пропускаю проверку:", e)
        return

    for vid, (name, cid) in cand.items():
        v = videos.get(vid)
        t = state["live"].get(vid)
        sn = v["snippet"] if v else {}
        lsd = v.get("liveStreamingDetails", {}) if v else {}
        is_live = (
            bool(v)
            and sn.get("liveBroadcastContent") == "live"
            and "actualStartTime" in lsd
            and "actualEndTime" not in lsd
        )

        if is_live:
            has_v = "concurrentViewers" in lsd
            viewers = int(lsd.get("concurrentViewers") or 0)
            if not t:
                t = {
                    "vid": vid, "cid": cid,
                    "name": name or sn.get("channelTitle", "?"),
                    "title": sn.get("title", ""),
                    "start": lsd["actualStartTime"],
                    "viewers": viewers, "peak": viewers, "has_viewers": has_v,
                }
                cap = caption_live(t, now)
                msg, photo = post(t, cap, best_thumb(sn))
                if msg:
                    t.update(msg=msg, photo=photo, cap=cap)
                    state["live"][vid] = t
                    print("Новый стрим:", t["name"], vid)
            else:
                t["title"] = sn.get("title", t["title"])
                t["viewers"] = viewers
                t["peak"] = max(t["peak"], viewers)
                t["has_viewers"] = t["has_viewers"] or has_v
                edit(t, caption_live(t, now))
        elif t:
            end = parse_t(lsd["actualEndTime"]) if "actualEndTime" in lsd else now
            edit(t, caption_end(t, end))
            del state["live"][vid]
            print("Стрим закончился:", t["name"], vid)

    state["day"] = now.strftime("%Y-%m-%d")  # раз в сутки коммит, чтобы GitHub не отключил расписание
    save_state(state)


if __name__ == "__main__":
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        sec = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 120
        while True:
            try:
                run_once()
            except Exception as e:
                print("Ошибка:", e)
            time.sleep(sec)
    else:
        run_once()
