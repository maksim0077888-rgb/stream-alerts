#!/usr/bin/env python3
"""YouTube -> Telegram: оповещения о трансляциях.

Все стримы собираются в ОДИН пост, бот его редактирует
(онлайн, длительность, новые стримы, «никого нет в эфире»).
Новый пост с уведомлением — не чаще раза в POST_EVERY_HOURS часов.

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

# КД: новый пост (с уведомлением) не чаще, чем раз в столько часов.
# Всё, что происходит в это время, бот дописывает в старый пост.
POST_EVERY_HOURS = 336
ENDED_MAX = 15  # сколько последних эфиров держать в посте
DELETE_OLD = True  # удалять пост, когда он устарел (никого нет и прошли сутки)

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
            return {"ok": True, "same": True}
        print("Telegram error:", method, body)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "description": body}


# ---------- файлы ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        s = {}
    s.setdefault("ids", {})  # ссылка из channels.txt -> ID канала
    s.pop("live", None)      # формат старой версии, больше не нужен
    s.setdefault("board", None)
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
    v = re.search(r"(?:watch\?v=|youtu\.be/|/live/|/shorts/)([\w-]{11})", ref)
    if m:
        cid = m.group(1)
    elif v:
        # ссылка на видео/стрим: узнаём, чей это канал
        items = yt("videos", part="snippet", id=v.group(1)).get("items", [])
        if not items:
            print("Видео не найдено:", ref)
            return None
        cid = items[0]["snippet"]["channelId"]
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


# ---------- текст поста ----------

TEXT_MAX = 4000  # лимит длины сообщения в Telegram


def parse_t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_dur(sec):
    h, m = divmod(max(0, int(sec)) // 60, 60)
    return "%d ч %02d мин" % (h, m) if h else "%d мин" % m


def short(s, n):
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def best_thumb(sn):
    th = sn.get("thumbnails", {})
    for k in ("maxres", "standard", "high", "medium", "default"):
        if k in th:
            return th[k]["url"]
    return None


def live_sorted(board):
    return sorted(board["streams"].values(), key=lambda t: -t["viewers"])


def build_text(board, now):
    """Строгий текстовый пост: только те, кто сейчас в эфире."""
    e = html.escape
    live = live_sorted(board)
    if not live:
        return "⚫️ <b>Сейчас никого нет в эфире</b>"

    def render(items, title_len, more=0):
        parts = ["🔴 <b>СЕЙЧАС В ЭФИРЕ — %d</b>" % len(live)]
        for t in items:
            dur = fmt_dur((now - parse_t(t["start"])).total_seconds())
            line = "<b><a href=\"https://youtu.be/%s\">%s</a></b>" % (t["vid"], e(t["name"]))
            if t["has_viewers"]:
                line += " · %d 👁" % t["viewers"]
            line += " · %s" % dur
            if title_len and t.get("title"):
                line += "\n└ %s" % e(short(t["title"], title_len))
            parts.append(line)
        if more:
            parts.append("…и ещё %d" % more)
        return "\n\n".join(parts)

    for title_len in (50, 30, 0):
        text = render(live, title_len)
        if len(text) <= TEXT_MAX:
            return text
    for n in range(len(live) - 1, 0, -1):
        text = render(live[:n], 0, len(live) - n)
        if len(text) <= TEXT_MAX:
            return text
    return text[:TEXT_MAX]


def _gone(r):
    desc = (r.get("description") or "").lower()
    return ("not found" in desc or "can't be edited" in desc
            or "message_id_invalid" in desc)


def send_board(board, now):
    text = build_text(board, now)
    r = tg("sendMessage", chat_id=TG_CHAT, text=text, parse_mode="HTML",
           disable_web_page_preview="true")
    if r.get("ok"):
        board["msg"] = r["result"]["message_id"]
        board["text"] = text
        board["kind"] = "text"
        board["created"] = now.isoformat()
        return True
    return False


def edit_board(board, now):
    if board.get("kind") == "photo":
        # пост старого вида (с картинкой): удаляем, бот сделает новый текстовый
        tg("deleteMessage", chat_id=TG_CHAT, message_id=board["msg"])
        return None
    text = build_text(board, now)
    if text == board.get("text"):
        return True
    r = tg("editMessageText", chat_id=TG_CHAT, message_id=board["msg"], text=text,
           parse_mode="HTML", disable_web_page_preview="true")
    if r.get("ok"):
        board["text"] = text
        return True
    if _gone(r):
        return None  # пост удалили — начнём новый
    return False


# ---------- основная проверка ----------

def run_once():
    state = load_state()
    now = datetime.now(timezone.utc)
    board = state["board"]

    cand = {}  # ID видео -> (ник, ID канала)
    cids = set()
    for name, ref in load_channels():
        try:
            cid = resolve(ref, state["ids"])
            if not cid or cid in cids:
                continue  # канал уже есть в списке выше — берём первый ник
            cids.add(cid)
            for vid in recent_videos(cid):
                cand[vid] = (name, cid)
        except Exception as e:
            print("Ошибка канала", ref, e)

    if board:
        # стримы каналов, которые убрали из списка, просто убираем из поста
        for vid in list(board["streams"]):
            if board["streams"][vid]["cid"] not in cids:
                del board["streams"][vid]
        for vid, t in board["streams"].items():
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

    live_now = {}
    for vid, (name, cid) in cand.items():
        v = videos.get(vid)
        if not v:
            continue
        sn = v["snippet"]
        lsd = v.get("liveStreamingDetails", {})
        if (sn.get("liveBroadcastContent") == "live"
                and "actualStartTime" in lsd and "actualEndTime" not in lsd):
            live_now[vid] = (name, cid, sn, lsd)

    if board is None and not live_now:
        state["day"] = now.strftime("%Y-%m-%d")
        save_state(state)
        return

    if board is None:
        board = {"msg": None, "streams": {}, "ended": [], "text": ""}

    # обновляем идущие и добавляем новые
    for vid, (name, cid, sn, lsd) in live_now.items():
        has_v = "concurrentViewers" in lsd
        viewers = int(lsd.get("concurrentViewers") or 0)
        t = board["streams"].get(vid)
        if not t:
            t = board["streams"][vid] = {
                "vid": vid, "cid": cid,
                "name": name or sn.get("channelTitle", "?"),
                "start": lsd["actualStartTime"],
                "peak": viewers, "has_viewers": False,
            }
            print("Новый стрим:", t["name"], vid)
        t["name"] = name or sn.get("channelTitle", t["name"])  # ник из channels.txt, если его поменяли
        t["title"] = sn.get("title", "")
        t["thumb"] = best_thumb(sn) or t.get("thumb")
        t["viewers"] = viewers
        t["peak"] = max(t["peak"], viewers)
        t["has_viewers"] = t["has_viewers"] or has_v

    # закончившиеся переносим вниз поста
    for vid in list(board["streams"]):
        if vid in live_now:
            continue
        t = board["streams"].pop(vid)
        lsd = videos.get(vid, {}).get("liveStreamingDetails", {})
        end = parse_t(lsd["actualEndTime"]) if "actualEndTime" in lsd else now
        t["dur"] = fmt_dur((end - parse_t(t["start"])).total_seconds())
        board["ended"].append(t)
        board["ended"] = board["ended"][-ENDED_MAX:]
        print("Стрим закончился:", t["name"], vid)

    if board["msg"] is None:
        ok = send_board(board, now) if board["streams"] else True
    else:
        ok = edit_board(board, now)
        if ok is None:
            board["msg"] = None
            board["ended"] = []
            ok = send_board(board, now) if board["streams"] else True

    if not board["streams"] and ok:
        # никого нет: пост висит «никого нет в эфире»
        created = board.get("created")
        if not created or (now - parse_t(created)).total_seconds() >= POST_EVERY_HOURS * 3600:
            # КД прошёл: пост устарел, удаляем его; следующий эфир начнёт новый
            if DELETE_OLD and board.get("msg"):
                r = tg("deleteMessage", chat_id=TG_CHAT, message_id=board["msg"])
                print("Старый пост удалён" if r.get("ok") else "Не смог удалить старый пост")
            board = None

    state["board"] = board
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
