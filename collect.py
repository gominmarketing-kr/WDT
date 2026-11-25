"""
W.D.T (Worry Data Tracker) 데이터 수집기 v3
- GitHub Actions가 2시간마다 실행해서 data.json(화면용)과 history.json(7일 기록)을 만듭니다.
- 필요한 비밀값: YOUTUBE_API_KEY (필수), GEMINI_API_KEY (있으면 AI 브리핑·댓글 요약)
- 바깥 라이브러리 없이 파이썬 기본 기능만 씁니다.
"""
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
OUT = os.environ.get("WD_OUT", "data.json")
HIST = os.environ.get("WD_HIST", "history.json")

# ───────── 분야 설정 (마케팅 우선 순서) ─────────
# searches: 유튜브 쇼츠 검색어 — 실행마다 1개씩 돌아가며 씁니다 (검색 1번 = 할당량 100, 하루 무료 10,000)
# news: 구글 뉴스 검색어 (무료, 제한 없음). "TOPIC:NATION" 같은 건 구글 뉴스 주제 섹션
CATS = {
    "meme": {"label": "밈·바이럴", "desc": "요즘 퍼지는 밈·챌린지·유행어 쇼츠와 SNS 화제·신조어 기사",
             "searches": ["밈", "챌린지", "유행어", "요즘 유행", "밈 모음"],
             "news": ["밈 OR 챌린지 OR 유행어", "SNS 화제 OR 신조어 OR 바이럴"]},
    "product": {"label": "신상·팝업 소식", "desc": "신상 리뷰·편의점 신상·팝업스토어·콜라보·한정판 쇼츠와 신제품 출시 기사",
                "searches": ["신상 리뷰", "팝업스토어", "콜라보", "편의점 신상", "한정판"],
                "news": ["신제품 출시 OR 한정판", "팝업스토어 OR 콜라보"]},
    "ad": {"label": "광고 레퍼런스", "desc": "광고 영상·광고 리뷰·CF 메이킹 쇼츠와 광고 캠페인·광고모델·마케팅 사례 기사",
           "searches": ["광고", "웃긴 광고", "광고 리뷰", "광고 비하인드", "CF 메이킹"],
           "news": ["광고 캠페인 OR 광고모델", "브랜드 캠페인 OR 마케팅 사례"]},
    "issue": {"label": "시사·이슈", "desc": "뉴스·속보·논란을 다룬 쇼츠와 오늘의 주요 뉴스 (브랜드가 올라탈 만한 이슈 찾기용)",
              "searches": [{"q": "#shorts", "videoCategoryId": "25"}, {"q": "뉴스", "videoCategoryId": "25"},
                           "이슈", {"q": "속보", "videoCategoryId": "25"}, "논란"],
              "news": ["TOPIC:NATION", "TOP"]},
}
# 시즌·캘린더는 2026-11-25부터 ⏰ 시의성 탭으로 옮겼어요

# 채널 워치: 한눈에 따로 지켜볼 유튜브 채널 (handle = 주소의 @ 뒤 부분)
WATCH_CHANNELS = [
    {"handle": "sabanfam", "label": "사건반장"},
]
WATCH_RECENT = 50         # 채널마다 가져올 최근 영상 수 (쇼츠+롱폼)
WATCH_COMMENTS = 6        # 채널마다 인기 댓글을 가져올 영상 수

# 시즌 캘린더 (월, 일, 이름, 검색어). 매년 같은 날짜
FIXED_DAYS = [
    (1, 1, "새해", "새해"), (2, 14, "발렌타인데이", "발렌타인데이"), (3, 14, "화이트데이", "화이트데이"),
    (4, 14, "블랙데이", "블랙데이"), (5, 5, "어린이날", "어린이날"), (5, 8, "어버이날", "어버이날"),
    (5, 15, "스승의날", "스승의날"), (10, 31, "할로윈", "할로윈"), (11, 11, "빼빼로데이", "빼빼로데이"),
    (12, 25, "크리스마스", "크리스마스"), (12, 31, "연말", "연말"),
    (3, 1, "삼일절", "삼일절"), (5, 1, "근로자의날", "근로자의날"), (6, 6, "현충일", "현충일"),
    (8, 15, "광복절", "광복절"), (10, 3, "개천절", "개천절"), (10, 9, "한글날", "한글날"),
]
# 해마다 날짜가 바뀌는 날 — 필요하면 여기에 추가
EXTRA_DAYS = [
    ("2026-11-19", "수능", "수능"), ("2027-02-06", "설날", "설날"), ("2027-09-15", "추석", "추석"),
    ("2027-11-18", "수능", "수능"), ("2026-12-22", "동지", "동지"), ("2027-12-22", "동지", "동지"),
]
# 민감 이슈: 브랜드가 올라타면 역풍 맞기 쉬운 말 (시의성 탭의 🔴 주의 표시)
SENSITIVE = re.compile(r"사망|숨져|숨진|참사|사고|화재|추락|살인|살해|폭행|성범죄|성폭|실종|지진|전쟁|테러|유족|피해자|극단|흉기|붕괴|폭발|침몰|학대|자살|별세|부고|추모|희생")
WEATHER_CITY = {"name": "서울", "lat": 37.5665, "lon": 126.978}

# 대세 기준
HOT_VIEWS, HOT_COMMENTS = 1_000_000, 1_000        # 🔥 대세
RISE_VIEWS, RISE_COMMENTS = 100_000, 100          # 📈 떠오르는
HOURS_BACK = 168          # 최근 7일 업로드
MAX_SECONDS = 180         # 쇼츠로 볼 최대 길이(초)
KEEP_PER_CAT = 40
COMMENT_VIDEOS = 12       # 인기 댓글을 가져올 영상 수
HISTORY_DAYS = 7
HANGUL = re.compile(r"[가-힣]")
NEWS_BASE = {"hl": "ko", "gl": "KR", "ceid": "KR:ko"}


def get(url, params=None, timeout=25, data=None, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    h = {"User-Agent": "Mozilla/5.0 (WD-dashboard)"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def yt(path, **params):
    params["key"] = API_KEY
    return json.loads(get("https://www.googleapis.com/youtube/v3/" + path, params))


def iso_dur(s):
    m = re.fullmatch(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if not d.get("sample") else {}
    except Exception:
        return {}


def child(el, name):
    for c in el:
        if c.tag == name or c.tag.endswith("}" + name):
            return c
    return None


def ctext(el, name):
    c = child(el, name)
    return (c.text or "").strip() if c is not None else ""


# ───────── 시즌 ─────────
def upcoming_days(within=45, limit=5):
    today = NOW.astimezone(KST).date()
    out = []
    for y in (today.year, today.year + 1):
        for m, d, name, q in FIXED_DAYS:
            out.append((date(y, m, d), name, q))
        # 블랙프라이데이: 11월 넷째 목요일 다음 날
        nov1 = date(y, 11, 1)
        thu = nov1 + timedelta(days=(3 - nov1.weekday()) % 7 + 21)
        out.append((thu + timedelta(days=1), "블랙프라이데이", "블랙프라이데이"))
    for s, name, q in EXTRA_DAYS:
        out.append((date.fromisoformat(s), name, q))
    res = []
    for dt, name, q in sorted(set(out)):
        dd = (dt - today).days
        if 0 <= dd <= within:
            res.append({"name": name, "q": q, "date": dt.isoformat(), "dday": dd})
    return res[:limit]


def season_action(dd):
    if dd <= 3:
        return "지금 올리기"
    if dd <= 13:
        return "업로드 준비"
    if dd <= 29:
        return "제작 들어갈 때"
    return "기획 시작"


# ───────── 1. 유튜브 쇼츠 + 인기 차트 ─────────
def video_items(ids):
    vids = []
    for i in range(0, len(ids), 50):
        res = yt("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
        vids += res.get("items", [])
    return vids


def channel_subs(vids):
    ch_ids = list({v["snippet"]["channelId"] for v in vids})
    subs = {}
    for i in range(0, len(ch_ids), 50):
        res = yt("channels", part="statistics", id=",".join(ch_ids[i:i + 50]))
        for c in res.get("items", []):
            st = c.get("statistics", {})
            subs[c["id"]] = 0 if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    return subs


def to_record(v, cat, subs, shorts_only=True):
    sn, st = v["snippet"], v.get("statistics", {})
    dur = iso_dur(v["contentDetails"].get("duration"))
    title = sn.get("title", "")
    if not dur or not HANGUL.search(title):
        return None
    if shorts_only and dur > MAX_SECONDS:
        return None
    pub = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
    hours = (NOW - pub).total_seconds() / 3600
    if shorts_only and hours > HOURS_BACK:
        return None
    views = int(st.get("viewCount", 0))
    if views < 1000:
        return None
    comments = int(st.get("commentCount", 0))
    thumbs = sn.get("thumbnails", {})
    thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    tier = "hot" if views >= HOT_VIEWS and comments >= HOT_COMMENTS else \
           "rise" if views >= RISE_VIEWS and comments >= RISE_COMMENTS else ""
    return {
        "id": v["id"], "title": title, "ch": sn.get("channelTitle", ""),
        "subs": max(subs.get(sn["channelId"], 0), 1), "views": views,
        "hours": round(max(hours, 0.5), 1), "likes": int(st.get("likeCount", 0)),
        "comments": comments, "cat": cat, "dur": dur, "thumb": thumb, "kw": [], "tier": tier,
    }


def collect_videos(prev, season_days):
    after = (NOW - timedelta(hours=HOURS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run = int(NOW.timestamp() // 7200)
    found = {v["id"]: v["cat"] for v in prev.get("videos", []) if v.get("cat") in CATS}
    searches = {k: list(c["searches"]) for k, c in CATS.items()}
    for cat, qs in searches.items():
        s = qs[run % len(qs)]
        s = s if isinstance(s, dict) else {"q": s}
        params = dict(part="id", type="video", maxResults=50, order="viewCount", regionCode="KR",
                      relevanceLanguage="ko", videoDuration="short", publishedAfter=after, **s)
        try:
            for it in yt("search", **params).get("items", []):
                vid = it["id"].get("videoId")
                if vid and vid not in found:
                    found[vid] = cat
        except Exception as e:
            print(f"[경고] 검색 실패 {cat} {s}: {e}", file=sys.stderr)
    print(f"추적 중인 쇼츠: {len(found)}개")

    vids = video_items(list(found))
    # 유튜브 한국 인기 급상승 차트 (할당량 1)
    chart = []
    try:
        chart = yt("videos", part="snippet,statistics,contentDetails", chart="mostPopular",
                   regionCode="KR", maxResults=50).get("items", [])
    except Exception as e:
        print(f"[경고] 인기 차트 실패: {e}", file=sys.stderr)
    subs = channel_subs(vids + chart)

    out = [r for r in (to_record(v, found[v["id"]], subs) for v in vids) if r]
    out.sort(key=lambda x: x["views"] / x["hours"], reverse=True)
    kept, per = [], Counter()
    for v in out:
        if per[v["cat"]] < KEEP_PER_CAT:
            kept.append(v)
            per[v["cat"]] += 1
    have = {v["id"] for v in kept}
    charts = []
    for rank, v in enumerate(chart, 1):
        r = to_record(v, None, subs, shorts_only=False)
        if r and r["id"] not in have:
            r["chart"] = rank
            charts.append(r)
    print("분야별 쇼츠:", dict(per), "/ 인기 차트:", len(charts))
    return kept, charts


def collect_comments(videos):
    """댓글 많은 순 상위 영상의 인기 댓글 (영상당 할당량 1)"""
    pick = sorted([v for v in videos if v["tier"]] or videos, key=lambda v: v["comments"], reverse=True)[:COMMENT_VIDEOS]
    for v in pick:
        try:
            res = yt("commentThreads", part="snippet", videoId=v["id"], order="relevance",
                     maxResults=5, textFormat="plainText")
            v["topComments"] = [{
                "text": it["snippet"]["topLevelComment"]["snippet"]["textDisplay"][:220],
                "likes": it["snippet"]["topLevelComment"]["snippet"].get("likeCount", 0),
            } for it in res.get("items", [])]
        except Exception as e:
            print(f"[경고] 댓글 실패 {v['id']}: {e}", file=sys.stderr)
    return [v["id"] for v in pick if v.get("topComments")]


# ───────── 1-2. 채널 워치 ─────────
def collect_watch(prev):
    prev_views = {v["id"]: v["views"] for ch in prev.get("channels", []) for v in ch.get("videos", [])}
    out = []
    for w in WATCH_CHANNELS:
        try:
            res = yt("channels", part="snippet,statistics,contentDetails", forHandle=w["handle"]).get("items", [])
            if not res:
                print(f"[경고] 채널을 찾지 못함: @{w['handle']}", file=sys.stderr)
                continue
            c = res[0]
            st, sn = c.get("statistics", {}), c["snippet"]
            uploads = c["contentDetails"]["relatedPlaylists"]["uploads"]
            items = yt("playlistItems", part="contentDetails", playlistId=uploads,
                       maxResults=WATCH_RECENT).get("items", [])
            ids = [it["contentDetails"]["videoId"] for it in items]
            subs = {c["id"]: int(st.get("subscriberCount", 0))}
            vids = []
            for v in video_items(ids):
                r = to_record(v, "watch", subs, shorts_only=False)
                if r:
                    r["watch"] = w["handle"]
                    if r["id"] in prev_views:
                        r["gain"] = max(r["views"] - prev_views[r["id"]], 0)
                    vids.append(r)
            vids.sort(key=lambda v: v["hours"])
            # 최근 3일 영상 중 댓글 많은 것의 인기 댓글
            recent = [v for v in vids if v["hours"] <= 72] or vids
            for v in sorted(recent, key=lambda v: v["comments"], reverse=True)[:WATCH_COMMENTS]:
                try:
                    r2 = yt("commentThreads", part="snippet", videoId=v["id"], order="relevance",
                            maxResults=5, textFormat="plainText")
                    v["topComments"] = [{
                        "text": it["snippet"]["topLevelComment"]["snippet"]["textDisplay"][:220],
                        "likes": it["snippet"]["topLevelComment"]["snippet"].get("likeCount", 0),
                    } for it in r2.get("items", [])]
                except Exception as e:
                    print(f"[경고] 채널 댓글 실패 {v['id']}: {e}", file=sys.stderr)
            words = Counter()
            for v in vids:
                for t in set(tokens(v["title"], {w["label"], sn.get("title", ""), "JTBC"})):
                    words[t] += 1
            day = [v for v in vids if v["hours"] <= 24]
            week = [v for v in vids if v["hours"] <= 168]
            th = (sn.get("thumbnails", {}).get("medium") or sn.get("thumbnails", {}).get("default") or {}).get("url", "")
            out.append({
                "handle": w["handle"], "label": w["label"], "id": c["id"], "title": sn.get("title", ""), "thumb": th,
                "subs": int(st.get("subscriberCount", 0)), "totalViews": int(st.get("viewCount", 0)),
                "videoCount": int(st.get("videoCount", 0)),
                "uploads24h": len(day), "uploads7d": len(week),
                "avgViews": round(sum(v["views"] for v in vids[:10]) / max(len(vids[:10]), 1)),
                "gainSum": sum(v.get("gain", 0) for v in vids),
                "words": [{"word": t, "n": n} for t, n in words.most_common(12) if n >= 2][:10],
                "videos": vids,
                # 이 채널 보도를 인용한 기사 (구글 뉴스, 최근 3일)
                "news": news_feed(f'"{w["label"]}"', 20, "3d"),
            })
            print(f"채널 워치 @{w['handle']}: 최근 영상 {len(vids)}개")
        except Exception as e:
            print(f"[경고] 채널 워치 실패 @{w['handle']}: {e}", file=sys.stderr)
    return out


def channel_trend(hist, ch):
    """구독자·총조회수 변화 (24시간, 7일)"""
    pts = [(datetime.fromisoformat(r["t"]), r["ch"][ch["handle"]]) for r in hist["runs"] if ch["handle"] in r.get("ch", {})]
    res = {}
    for key, hrs in (("24h", 24), ("7d", 168)):
        old = [p for t, p in pts if NOW - t <= timedelta(hours=hrs)]
        if len(old) >= 2:
            res[key] = {"subs": ch["subs"] - old[0][0], "views": ch["totalViews"] - old[0][1]}
    return res


# ───────── 2. 지금 뜨는 검색어 (구글 트렌드) + 관련 기사 ─────────
def collect_google_trends():
    try:
        root = ET.fromstring(get("https://trends.google.com/trending/rss", {"geo": "KR"}))
    except Exception as e:
        print(f"[경고] 구글 트렌드 실패: {e}", file=sys.stderr)
        return []
    kws = []
    for item in root.iter("item"):
        word = ctext(item, "title")
        if not word:
            continue
        news = []
        for el in item:
            if el.tag.endswith("news_item"):
                t, u = ctext(el, "news_item_title"), ctext(el, "news_item_url")
                if t and u:
                    news.append({"title": t, "url": u, "source": ctext(el, "news_item_source")})
        if keep_keyword(word):
            kws.append({"word": word, "traffic": ctext(item, "approx_traffic"), "news": news[:5]})
    return kws[:15]


def keep_keyword(w):
    """한국 마케팅에 의미 없는 외국어 검색어(태국어·베트남어 경기명 등) 거르기"""
    if HANGUL.search(w):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9 .&'-]+", w)) and len(w.split()) <= 2


def keyword_news(words, have=None, limit=6):
    """검색어마다 최근 3일 기사 (구글 뉴스, 무료). 화면에서 검색어를 누르면 이 기사가 나와요"""
    import time
    have = have or {}
    out = {}
    for w in words:
        items = list(have.get(w, []))
        if len(items) < 3:
            try:
                for it in news_feed(f'"{w}"', limit, "3d"):
                    if all(it["url"] != x["url"] and it["title"][:20] != x["title"][:20] for x in items):
                        items.append(it)
            except Exception as e:
                print(f"[경고] 검색어 기사 실패 {w}: {e}", file=sys.stderr)
            time.sleep(0.2)
        if items:
            out[w] = items[:limit]
    return out


# ───────── 3. 요즘 뜨는 이슈 (구글 뉴스) ─────────
def news_feed(spec, limit=15, when="1d"):
    if spec == "TOP":
        url, params = "https://news.google.com/rss", dict(NEWS_BASE)
    elif spec.startswith("TOPIC:"):
        url, params = f"https://news.google.com/rss/headlines/section/topic/{spec[6:]}", dict(NEWS_BASE)
    else:
        url, params = "https://news.google.com/rss/search", dict(NEWS_BASE, q=f"{spec} when:{when}")
    try:
        root = ET.fromstring(get(url, params))
    except Exception as e:
        print(f"[경고] 뉴스 실패 {spec}: {e}", file=sys.stderr)
        return []
    items = []
    for it in root.iter("item"):
        title, link, source = ctext(it, "title"), ctext(it, "link"), ctext(it, "source")
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            pub = parsedate_to_datetime(ctext(it, "pubDate")).astimezone(KST).isoformat(timespec="minutes")
        except Exception:
            pub = ""
        if title and link:
            items.append({"title": title, "url": link, "source": source, "time": pub})
        if len(items) >= limit:
            break
    return items


def collect_issues(season_days):
    specs = {k: list(c["news"]) for k, c in CATS.items()}
    issues = {"all": news_feed("TOP", 20)}
    for cat, sp in specs.items():
        seen, merged = set(), []
        for spec in sp:
            for it in news_feed(spec, 12):
                key = it["title"][:20]
                if key not in seen:
                    seen.add(key)
                    merged.append(it)
        merged.sort(key=lambda x: x["time"], reverse=True)
        issues[cat] = merged[:15]
    return issues


PARTICLES = ("에서는", "으로는", "에게서", "까지", "부터", "에서", "으로", "에게", "보다", "처럼", "은", "는", "을", "를", "에")
STOP = set("""단독 속보 종합 포토 영상 사진 기자 뉴스 오늘 내일 어제 올해 지난 이번 관련 대한 위해 통해 가운데 있다 없다 한다 했다
된다 것으로 그리고 하지만 때문 이후 이상 만에 등 및 더 첫 전 후 요구 지원 역대 강력 요청 적용 증가 확대 발표 추진 예정 진행
계획 결과 상황 모습 공개 확인 논의 개최 이유 가능 최대 최초 사실 직접 전국 주요 국내 서울 한국 대표 정부 정책 시장 대통령
shorts Shorts SHORTS 쇼츠 진짜 이거 this This is the end The and of to in for you""".split())


def tokens(text, banned=()):
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|#\S+", " ", text)
    out = []
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", text):
        for p in PARTICLES:
            if len(w) > len(p) + 1 and w.endswith(p):
                w = w[: -len(p)]
                break
        if len(w) < 2 or w in STOP or w in banned or w.isdigit():
            continue
        if re.fullmatch(r"[A-Za-z]+", w) and (len(w) < 3 or w.lower() in STOP):
            continue
        out.append(w)
    return out


def issue_keywords(issues):
    res = {}
    pool = {}
    for items in issues.values():
        for it in items:
            pool.setdefault(it["title"][:20], it)
    groups = dict(issues, all=list(pool.values()))
    for cat, items in groups.items():
        c, srcs = Counter(), defaultdict(set)
        sources = {it.get("source") for it in items}
        for it in items:
            for w in set(tokens(it["title"], sources)):
                c[w] += 1
                srcs[w].add(it.get("source"))
        res[cat] = [{"word": w, "n": n} for w, n in c.most_common(40) if n >= 2 and len(srcs[w]) >= 2][:10]
    return res


# ───────── 4. 형식 분석 ─────────
FORMATS = [
    ("비교형 (A vs B)", r"vs|VS|비교|차이"),
    ("리스트·랭킹형", r"TOP\s?\d|\d+\s?(가지|유형|개)|유형|랭킹|순위"),
    ("질문형 제목", r"\?|？|왜 |뭘까|일까"),
    ("요약·정리형", r"정리|요약|\d+\s?(초|분)\s?(만에|안에|설명|정리)"),
    ("실험·반응형", r"해봤|해봄|해보았|반응|실험|도전|몰카"),
    ("이슈·속보형", r"속보|단독|논란|근황|결국|충격"),
    ("꿀팁·방법형", r"꿀팁|방법|하는 법|팁|노하우"),
]


def analyze_formats(videos):
    if not videos:
        return []
    vph = lambda v: v["views"] / v["hours"]
    avg = sum(vph(v) for v in videos) / len(videos)
    formats = []
    for name, pat in FORMATS:
        hit = sorted([v for v in videos if re.search(pat, v["title"])], key=vph, reverse=True)
        if len(hit) >= 2:
            lift = (sum(vph(v) for v in hit) / len(hit)) / avg - 1
            names = []
            for h in hit:
                t = re.sub(r"#\S+", "", h["title"]).strip()[:20]
                if t and t not in names:
                    names.append(t)
            formats.append({"name": name, "lift": f"{lift:+.0%}", "n": len(hit), "ex": ", ".join(names[:2]),
                            "ids": [h["id"] for h in hit[:20]], "_l": lift})
    formats.sort(key=lambda f: f["_l"], reverse=True)
    for f in formats:
        f.pop("_l")
    return formats


def length_stats(videos):
    buckets = [("15초 이하", 0, 15), ("16~30초", 16, 30), ("31~60초", 31, 60), ("61초 이상", 61, 9999)]
    vph = lambda v: v["views"] / v["hours"]
    avg = (sum(vph(v) for v in videos) / len(videos)) if videos else 1
    out = []
    for name, lo, hi in buckets:
        b = [v for v in videos if lo <= v["dur"] <= hi]
        if b:
            out.append({"name": name, "n": len(b), "lift": f"{(sum(vph(v) for v in b) / len(b)) / avg - 1:+.0%}"})
    return out


# ───────── 4-2. 시의성: 날씨 + 이슈 타이밍 ─────────
WMO = {0: ("☀️", "맑음"), 1: ("🌤️", "대체로 맑음"), 2: ("⛅", "구름 조금"), 3: ("☁️", "흐림"), 45: ("🌫️", "안개"), 48: ("🌫️", "안개")}


def wmo(code):
    if code in WMO:
        return WMO[code]
    if 51 <= code <= 67 or 80 <= code <= 82:
        return ("🌧️", "비")
    if 71 <= code <= 77 or 85 <= code <= 86:
        return ("🌨️", "눈")
    if code >= 95:
        return ("⛈️", "뇌우")
    return ("☁️", "흐림")


def collect_weather():
    c = WEATHER_CITY
    try:
        f = json.loads(get("https://api.open-meteo.com/v1/forecast", {
            "latitude": c["lat"], "longitude": c["lon"], "timezone": "Asia/Seoul", "forecast_days": 7,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum"}))
    except Exception as e:
        print(f"[경고] 날씨 실패: {e}", file=sys.stderr)
        return None
    pm = {}
    try:
        a = json.loads(get("https://air-quality-api.open-meteo.com/v1/air-quality", {
            "latitude": c["lat"], "longitude": c["lon"], "timezone": "Asia/Seoul", "forecast_days": 5,
            "hourly": "pm10,pm2_5"}))
        h = a.get("hourly", {})
        for t, p10, p25 in zip(h.get("time", []), h.get("pm10", []), h.get("pm2_5", [])):
            d = t[:10]
            cur = pm.setdefault(d, [0, 0])
            cur[0] = max(cur[0], p10 or 0)
            cur[1] = max(cur[1], p25 or 0)
    except Exception as e:
        print(f"[경고] 미세먼지 실패: {e}", file=sys.stderr)
    d = f.get("daily", {})
    days = []
    for i, day in enumerate(d.get("time", [])):
        code = int(d["weather_code"][i] or 0)
        icon, desc = wmo(code)
        days.append({"date": day, "icon": icon, "desc": desc,
                     "tmax": round(d["temperature_2m_max"][i]), "tmin": round(d["temperature_2m_min"][i]),
                     "rain": round(d["precipitation_sum"][i] or 0, 1), "snow": round(d["snowfall_sum"][i] or 0, 1),
                     "pm10": round(pm.get(day, [0, 0])[0]), "pm25": round(pm.get(day, [0, 0])[1])})
    signals = []
    for i, x in enumerate(days):
        when = x["date"]
        if x["tmin"] <= -10:
            signals.append({"date": when, "type": "cold", "label": "🥶 한파", "detail": f"아침 {x['tmin']}°C", "q": "한파"})
        elif x["tmin"] <= -5:
            signals.append({"date": when, "type": "cold", "label": "🧣 강추위", "detail": f"아침 {x['tmin']}°C", "q": "추위"})
        if i > 0 and days[i - 1]["tmin"] - x["tmin"] >= 7:
            signals.append({"date": when, "type": "drop", "label": "📉 기온 뚝", "detail": f"하루 새 {days[i-1]['tmin'] - x['tmin']}°C 떨어짐", "q": "기온 급강하"})
        if x["snow"] > 0:
            signals.append({"date": when, "type": "snow", "label": "❄️ 눈 소식", "detail": f"적설 {x['snow']}cm 예상", "q": "첫눈"})
        if x["rain"] >= 10:
            signals.append({"date": when, "type": "rain", "label": "☔ 많은 비", "detail": f"{x['rain']}mm", "q": "비 소식"})
        if x["tmax"] >= 33:
            signals.append({"date": when, "type": "heat", "label": "🔥 폭염", "detail": f"낮 {x['tmax']}°C", "q": "폭염"})
        if x["tmax"] - x["tmin"] >= 12:
            signals.append({"date": when, "type": "gap", "label": "🌡️ 큰 일교차", "detail": f"{x['tmax'] - x['tmin']}°C 차이", "q": "일교차"})
        if x["pm10"] >= 81 or x["pm25"] >= 36:
            lv = "매우 나쁨" if x["pm10"] >= 151 or x["pm25"] >= 76 else "나쁨"
            signals.append({"date": when, "type": "dust", "label": "😷 미세먼지 " + lv, "detail": f"PM10 {x['pm10']} · PM2.5 {x['pm25']}", "q": "미세먼지"})
    # 같은 종류는 첫날 하나로 묶기 (예: 미세먼지 나쁨 3일 → "11/15 외 2일")
    merged = {}
    for sg in signals:
        k = sg["type"]
        if k not in merged:
            merged[k] = dict(sg, more=0)
        else:
            merged[k]["more"] += 1
    return {"city": c["name"], "days": days, "signals": sorted(merged.values(), key=lambda x: x["date"])}


def first_seen(hist):
    """검색어·이슈 키워드가 기록에 처음 나타난 시각"""
    seen = {}
    for r in hist.get("runs", []):
        t = r["t"]
        words = list(r.get("kw", [])) + [w for ws in r.get("iw", {}).values() for w in ws]
        for w in words:
            seen.setdefault(w, t)
    return seen


def timely_issues(keywords, issue_words, hist, kw_news, videos):
    seen = first_seen(hist)
    runs24 = [r for r in hist.get("runs", []) if NOW - datetime.fromisoformat(r["t"]) <= timedelta(hours=24)]
    cands = {}
    for k in keywords:
        cands[k["word"]] = {"word": k["word"], "traffic": k.get("traffic", ""), "src": "검색 급상승"}
    for w in (issue_words.get("all") or [])[:10]:
        cands.setdefault(w["word"], {"word": w["word"], "traffic": "", "src": "언론 동시 보도"})["press"] = w["n"]
    out = []
    for w, c in cands.items():
        t0 = seen.get(w)
        hrs = round((NOW - datetime.fromisoformat(t0)).total_seconds() / 3600, 1) if t0 else 0
        news = kw_news.get(w, [])
        key = w.replace(" ", "")
        shorts = sum(1 for v in videos if key in v["title"].replace(" ", ""))
        hits24 = sum(1 for r in runs24 if w in r.get("kw", []) or any(w in ws for ws in r.get("iw", {}).values()))
        text = w + " " + " ".join(n["title"] for n in news[:4])
        if SENSITIVE.search(text):
            status = "caution"
        elif hrs <= 24 and shorts <= 3:
            status = "go"
        elif hrs > 48 or shorts > 5:
            status = "late"
        else:
            status = "go" if hrs <= 36 else "late"
        out.append(dict(c, firstSeen=t0, hours=hrs, news=len(news), shorts=shorts, hits24=hits24, status=status))
    rank = {"go": 0, "late": 1, "caution": 2}
    out.sort(key=lambda x: (rank[x["status"]], x["hours"]))
    return out[:14]


# ───────── 5. 7일 기록 + 기간별 집계 ─────────
def update_history(hist, keywords, issue_words, videos, charts, channels=()):
    runs = [r for r in hist.get("runs", [])
            if NOW - datetime.fromisoformat(r["t"]) <= timedelta(days=HISTORY_DAYS)]
    top = sorted(videos + charts, key=lambda v: v["views"] / v["hours"], reverse=True)[:40]
    runs.append({
        "t": NOW.isoformat(timespec="minutes"),
        "kw": [k["word"] for k in keywords],
        "iw": {c: [w["word"] for w in ws] for c, ws in issue_words.items()},
        "v": [[v["id"], v["views"], v["comments"], v.get("cat"), v["title"][:80], v["ch"], v["thumb"], v["dur"]] for v in top],
        "ch": {c["handle"]: [c["subs"], c["totalViews"]] for c in channels},
    })
    return {"runs": runs}


def periods(hist):
    res = {}
    for key, hrs in (("12h", 12), ("24h", 24), ("3d", 72), ("7d", 168)):
        rs = [r for r in hist["runs"] if NOW - datetime.fromisoformat(r["t"]) <= timedelta(hours=hrs)]
        n = max(len(rs), 1)
        kw_runs, kw_days = Counter(), defaultdict(set)
        iw = defaultdict(Counter)
        iw_days = defaultdict(lambda: defaultdict(set))
        vids = {}
        for r in rs:
            day = datetime.fromisoformat(r["t"]).astimezone(KST).date().isoformat()
            for w in r["kw"]:
                kw_runs[w] += 1
                kw_days[w].add(day)
            for c, ws in r["iw"].items():
                for w in ws:
                    iw[c][w] += 1
                    iw_days[c][w].add(day)
            for vid, views, comments, cat, title, ch, thumb, dur in r["v"]:
                if vid not in vids or views > vids[vid]["views"]:
                    vids[vid] = {"id": vid, "views": views, "comments": comments, "cat": cat,
                                 "title": title, "ch": ch, "thumb": thumb, "dur": dur}
        res[key] = {
            "runs": len(rs),
            "keywords": [{"word": w, "runs": c, "days": len(kw_days[w]),
                          "steady": c >= max(3, n * 0.5) or len(kw_days[w]) >= 2}
                         for w, c in kw_runs.most_common(40) if keep_keyword(w)][:15],
            "issueWords": {c: [{"word": w, "n": k, "days": len(iw_days[c][w]),
                                "steady": k >= max(3, n * 0.5) or len(iw_days[c][w]) >= 2}
                               for w, k in cnt.most_common(10)] for c, cnt in iw.items()},
            "videos": sorted(vids.values(), key=lambda v: v["views"], reverse=True)[:20],
        }
    return res


# ───────── 6. 브리핑 + Gemini ─────────
def gemini_models():
    """쓸 수 있는 Gemini 모델 순서 (구글이 모델 이름을 자주 바꿔서 목록에서 직접 찾아요)"""
    names = ["gemini-flash-latest", "gemini-flash-lite-latest"]
    try:
        res = json.loads(get("https://generativelanguage.googleapis.com/v1beta/models",
                             {"key": GEMINI_KEY, "pageSize": 200}))
        found = [m["name"].split("/")[-1] for m in res.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        good = [n for n in found if "flash" in n and not re.search(r"image|tts|live|audio|embed|exp|preview", n)]
        good.sort(key=lambda n: ("lite" in n, n), reverse=False)
        names += [n for n in reversed(sorted(good)) if n not in names][:3]
    except Exception as e:
        print(f"[경고] Gemini 모델 목록 실패: {e}", file=sys.stderr)
    return names[:4]


def gemini(prompt):
    if not GEMINI_KEY:
        return None
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                       "generationConfig": {"responseMimeType": "application/json", "temperature": 0.6}}).encode()
    for model in gemini_models():
        try:
            raw = get(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      {"key": GEMINI_KEY}, data=body, headers={"Content-Type": "application/json"}, timeout=150)
            parts = json.loads(raw)["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            text = re.sub(r"^```(?:json)?|```$", "", text.strip()).strip()
            out = json.loads(text)
            if isinstance(out, list) and out:
                out = out[0]
            print(f"Gemini 사용 모델: {model}")
            return out
        except Exception as e:
            print(f"[경고] Gemini {model} 실패: {e}", file=sys.stderr)
    return None


def build_briefing(videos, charts, issue_words, keywords, formats, season_days, comment_ids, channels=(), timely=None):
    by_id = {v["id"]: v for v in videos + charts}
    for ch in channels:
        for v in ch["videos"]:
            by_id.setdefault(v["id"], v)
    vph = lambda v: v["views"] / v["hours"]
    best = sorted(videos, key=lambda v: (v["tier"] == "hot", v["tier"] == "rise", v["comments"] * 0.6 + vph(v) * 0.4), reverse=True)[:3]
    br = {
        "issues": (issue_words.get("all") or [])[:3],
        "shorts": [v["id"] for v in best],
        "format": formats[0] if formats else None,
        "season": season_days[:3],
        "hotCount": sum(1 for v in videos if v["tier"] == "hot"),
        "riseCount": sum(1 for v in videos if v["tier"] == "rise"),
    }
    # 자동 문장 (AI 없을 때)
    lines = []
    if br["issues"]:
        lines.append("오늘 여러 언론이 같이 다룬 주제는 " + ", ".join(f"‘{w['word']}’" for w in br["issues"]) + "이에요.")
    if best:
        t0 = re.sub(r"#\S+", "", best[0]["title"]).strip()[:30]
        lines.append(f"댓글 반응이 가장 뜨거운 쇼츠는 ‘{t0}’(댓글 {best[0]['comments']:,}개)예요.")
    if br["format"]:
        lines.append(f"제목 형식은 {br['format']['name']}이 평균보다 {br['format']['lift']} 빨리 퍼지고 있어요.")
    if season_days:
        d = season_days[0]
        lines.append(f"{d['name']}까지 {d['dday']}일 남았어요." if d["dday"] else f"오늘은 {d['name']}이에요.")
    br["auto"] = lines

    # AI 브리핑 + 댓글 반응 요약
    cmt_block = []
    for vid in comment_ids[:10]:
        v = by_id[vid]
        cs = " / ".join(c["text"].replace("\n", " ")[:120] for c in v.get("topComments", []))
        cmt_block.append(f'- id={vid} | 제목: {v["title"][:60]} | 조회 {v["views"]:,} | 댓글 {v["comments"]:,} | 인기댓글: {cs}')
    ch_lines = []
    for ch in channels:
        ch_lines.append(f"[지켜보는 채널: {ch['label']}] 최근 영상 제목: " + " / ".join(v["title"][:50] for v in ch["videos"][:15]))
        for v in ch["videos"]:
            if v.get("topComments"):
                cs = " / ".join(c["text"].replace("\n", " ")[:100] for c in v["topComments"])
                ch_lines.append(f'- id={v["id"]} | 제목: {v["title"][:60]} | 조회 {v["views"]:,} | 댓글 {v["comments"]:,} | 인기댓글: {cs}')
    ch_block = "\n".join(ch_lines)
    tm = timely or {}
    tm_lines = []
    for x in tm.get("issues", [])[:12]:
        tm_lines.append(f'- {x["word"]} | 처음 뜬 지 {x["hours"]}시간 | 기사 {x["news"]} | 관련 쇼츠 {x["shorts"]} | 출처 {x["src"]}')
    for d in tm.get("seasons", [])[:8]:
        tm_lines.append(f'- [시즌] {d["name"]} D-{d["dday"]}')
    for w in ((tm.get("weather") or {}).get("signals") or [])[:5]:
        tm_lines.append(f'- [날씨] {w["date"]} {w["label"]} {w["detail"]}')
    tm_block = "\n".join(tm_lines)
    prompt = f"""너는 한국 마케팅 회사의 트렌드 분석가야. 아래 오늘 데이터로 마케터용 브리핑을 써줘.
과장하지 말고 데이터에 있는 것만 근거로 써. 정치적으로는 중립을 지켜. 문장은 짧고 쉽게, 존댓말(~해요)로.

[여러 언론이 같이 다룬 이슈 키워드] {", ".join(w["word"] for w in (issue_words.get("all") or [])[:8])}
[구글 급상승 검색어] {", ".join(k["word"] for k in keywords[:10])}
[분야별 이슈 키워드] {json.dumps({CATS[c]["label"]: [w["word"] for w in ws[:5]] for c, ws in issue_words.items() if c in CATS}, ensure_ascii=False)}
[반응 좋은 쇼츠] {json.dumps([{"제목": v["title"][:60], "분야": CATS.get(v["cat"], {}).get("label"), "조회": v["views"], "댓글": v["comments"]} for v in best], ensure_ascii=False)}
[잘 되는 제목 형식] {json.dumps([{"형식": f["name"], "평균대비": f["lift"]} for f in formats[:3]], ensure_ascii=False)}
[다가오는 시즌] {json.dumps([{"이름": d["name"], "D-day": d["dday"]} for d in season_days], ensure_ascii=False)}
[댓글 많은 영상과 인기 댓글]
{chr(10).join(cmt_block)}
{ch_block}
[시의성 후보 (이슈·시즌·날씨)]
{tm_block}

JSON으로만 답해:
{{"headline": "오늘을 한 문장으로 (40자 이내)",
 "points": ["마케터가 알아야 할 핵심 3가지, 각 60자 이내"],
 "ideas": ["바로 써볼 콘텐츠/캠페인 아이디어 3가지, 각 60자 이내"],
 "mood": "댓글 전체 분위기 한두 문장",
 "comments": {{"영상id": "그 영상 댓글 반응 한 줄 요약 (긍정/부정/많이 나온 말)"}},
 "channels": {{"채널 이름": "그 채널이 요즘 다루는 주제와 시청자 반응 2문장 + 마케터가 참고할 점 1문장"}},
 "timely": {{"headline": "시의성 관점에서 오늘 한 문장 (40자 이내)",
            "issues": {{"이슈 단어": {{"status": "go(지금 타이밍) / late(이미 늦음) / caution(사고·참사·정치 등 브랜드가 피해야 함) 중 하나", "idea": "브랜드 활용 아이디어 40자 이내 (caution이면 피해야 하는 이유)"}}}},
            "seasons": {{"시즌 이름": "그 시즌에 해볼 콘텐츠 아이디어 40자 이내"}},
            "weather": "이번 주 날씨로 해볼 콘텐츠 아이디어 1~2문장"}}}}"""
    ai = gemini(prompt)
    if isinstance(ai, dict):
        br["ai"] = {k: ai.get(k) for k in ("headline", "points", "ideas", "mood")}
        for vid, s in (ai.get("comments") or {}).items():
            if vid in by_id and isinstance(s, str):
                by_id[vid]["commentSummary"] = s[:160]
        ta = ai.get("timely") or {}
        if isinstance(ta, dict) and timely:
            timely["headline"] = ta.get("headline") if isinstance(ta.get("headline"), str) else None
            iss = ta.get("issues") or {}
            for x in timely.get("issues", []):
                a = iss.get(x["word"])
                if isinstance(a, dict):
                    if a.get("status") in ("go", "late", "caution") and x["status"] != "caution":
                        x["status"] = a["status"]   # 규칙상 주의(🔴)는 AI가 풀지 못하게
                    if isinstance(a.get("idea"), str):
                        x["idea"] = a["idea"][:120]
            si = ta.get("seasons") or {}
            for d in timely.get("seasons", []):
                if isinstance(si.get(d["name"]), str):
                    d["idea"] = si[d["name"]][:120]
            if timely.get("weather") and isinstance(ta.get("weather"), str):
                timely["weather"]["idea"] = ta["weather"][:240]
        chs = ai.get("channels") or {}
        for ch in channels:
            txt = chs.get(ch["label"]) or chs.get(ch["title"]) or chs.get(ch["handle"])
            if isinstance(txt, str):
                ch["aiSummary"] = txt[:400]
    return br


# ───────── 실행 ─────────
def main():
    if not API_KEY:
        sys.exit("YOUTUBE_API_KEY가 없어요. GitHub > Settings > Secrets and variables > Actions 에 추가하세요.")
    prev = load_json(OUT)
    hist = load_json(HIST)
    prev_views = {v["id"]: v["views"] for v in prev.get("videos", [])}
    prev_rank = {k["word"]: i for i, k in enumerate(prev.get("keywords", []))}
    season_days = upcoming_days()

    videos, charts = collect_videos(prev, season_days)
    for v in videos + charts:
        if v["id"] in prev_views:
            v["gain"] = max(v["views"] - prev_views[v["id"]], 0)
    comment_ids = collect_comments(videos + charts)
    channels = collect_watch(prev)

    keywords = collect_google_trends()
    for i, k in enumerate(keywords):
        k["chg"] = "new" if k["word"] not in prev_rank else prev_rank[k["word"]] - i
        hits = [v for v in videos if k["word"].replace(" ", "") in v["title"].replace(" ", "")]
        for v in hits:
            v["kw"].append(k["word"])

    issues = collect_issues(season_days)
    issue_words = issue_keywords(issues)
    formats = analyze_formats(videos)
    hist = update_history(hist, keywords, issue_words, videos, charts, channels)
    for ch in channels:
        ch["trend"] = channel_trend(hist, ch)
    per = periods(hist)
    seasons = upcoming_days(within=75, limit=10)
    for d in seasons:
        d["action"] = season_action(d["dday"])
    words = [k["word"] for k in keywords]
    for p in per.values():
        words += [k["word"] for k in p["keywords"]]
        for ws in p["issueWords"].values():
            words += [w["word"] for w in ws[:6]]
    for ws in issue_words.values():
        words += [w["word"] for w in ws[:6]]
    words = list(dict.fromkeys(words))[:70] + [d["q"] for d in seasons]
    kw_news = keyword_news(list(dict.fromkeys(words)), {k["word"]: k.get("news", []) for k in keywords})
    timely = {"issues": timely_issues(keywords, issue_words, hist, kw_news, videos),
              "seasons": seasons, "weather": collect_weather()}
    briefing = build_briefing(videos, charts, issue_words, keywords, formats, season_days, comment_ids, channels, timely)

    data = {
        "updatedAt": NOW.astimezone(KST).isoformat(timespec="minutes"),
        "cats": {k: {"label": c["label"], "desc": c.get("desc", "")} for k, c in CATS.items()},
        "thresholds": {"hot": [HOT_VIEWS, HOT_COMMENTS], "rise": [RISE_VIEWS, RISE_COMMENTS]},
        "briefing": briefing,
        "keywords": keywords,
        "issues": issues,
        "issueWords": issue_words,
        "periods": per,
        "kwNews": kw_news,
        "videos": videos,
        "charts": charts,
        "commentIds": comment_ids,
        "formats": formats,
        "lengths": length_stats(videos),
        "season": season_days,
        "channels": channels,
        "timely": timely,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))
    print(f"완료: 쇼츠 {len(videos)}개, 차트 {len(charts)}개, 댓글 영상 {len(comment_ids)}개, "
          f"이슈 {sum(len(v) for v in issues.values())}건, 채널 워치 {len(channels)}개, 검색어 기사 {len(kw_news)}개, 날씨 {'O' if timely.get('weather') else 'X'}, AI {'O' if briefing.get('ai') else 'X'}")


if __name__ == "__main__":
    main()
