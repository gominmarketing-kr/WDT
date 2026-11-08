"""
W.D (Worry Marketing Data Trap) 데이터 수집기
- GitHub Actions가 2시간마다 실행해서 data.json을 새로 만듭니다.
- 필요한 비밀값: YOUTUBE_API_KEY (GitHub > Settings > Secrets 에 저장)
- 바깥 라이브러리 없이 파이썬 기본 기능만 씁니다.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
OUT = os.environ.get("WD_OUT", "data.json")

# ───────── 분야 설정: 여기만 고치면 수집 대상이 바뀝니다 ─────────
# videoCategoryId: 23=코미디, 24=엔터테인먼트, 25=뉴스/정치
# 한 번 실행할 때 분야마다 검색어 2개씩 돌아가며 씁니다 (검색 1번 = 할당량 100, 하루 무료 10,000)
CATS = {
    "fun": {"label": "유머", "searches": [
        {"q": "#shorts", "videoCategoryId": "23"},
        {"q": "웃긴 쇼츠"},
        {"q": "상황극 쇼츠"},
        {"q": "밈 쇼츠"},
    ], "news": ["밈 OR 챌린지 OR 바이럴 OR 화제의", "TOPIC:ENTERTAINMENT"]},
    "pol": {"label": "정치/시사", "searches": [
        {"q": "#shorts", "videoCategoryId": "25"},
        {"q": "시사 정리 쇼츠"},
        {"q": "정치 쇼츠"},
        {"q": "뉴스 요약 쇼츠"},
    ], "news": ["TOPIC:NATION", "국회 OR 정부 OR 여야"]},
    "ad": {"label": "광고/마케팅", "searches": [
        {"q": "광고 쇼츠"},
        {"q": "마케팅 쇼츠"},
        {"q": "광고 리뷰 쇼츠"},
        {"q": "브랜드 광고"},
        {"q": "바이럴 광고"},
    ], "news": ["마케팅 OR 광고 캠페인 OR 브랜드 협업", "팝업스토어 OR 신제품 출시 OR 광고모델"]},
}
SEARCHES_PER_CAT = 2
HOURS_BACK = 72          # 최근 몇 시간 안에 올라온 영상만
MAX_SECONDS = 180        # 쇼츠로 볼 최대 길이(초)
KEEP_PER_CAT = 40        # 분야별로 남길 영상 수
HANGUL = re.compile(r"[가-힣]")
NEWS_BASE = {"hl": "ko", "gl": "KR", "ceid": "KR:ko"}


def get(url, params=None, timeout=20):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (WD-dashboard)"})
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


def load_prev():
    try:
        with open(OUT, encoding="utf-8") as f:
            d = json.load(f)
        return d if not d.get("sample") else {}
    except Exception:
        return {}


def child(el, name):
    """네임스페이스와 상관없이 자식 태그 찾기"""
    for c in el:
        if c.tag == name or c.tag.endswith("}" + name):
            return c
    return None


def ctext(el, name):
    c = child(el, name)
    return (c.text or "").strip() if c is not None else ""


# ───────── 1. 유튜브 쇼츠 ─────────
def collect_videos(prev):
    after = (NOW - timedelta(hours=HOURS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run = int(NOW.timestamp() // 7200)           # 실행마다 다른 검색어 조합
    found = {}  # id -> cat
    # 지난번에 찾은 영상도 이어서 추적 (조회수 갱신은 할당량이 거의 안 듭니다)
    for v in prev.get("videos", []):
        found[v["id"]] = v["cat"]
    for cat, cfg in CATS.items():
        qs = cfg["searches"]
        for k in range(min(SEARCHES_PER_CAT, len(qs))):
            s = qs[(run * SEARCHES_PER_CAT + k) % len(qs)]
            params = dict(part="id", type="video", maxResults=50, order="viewCount",
                          regionCode="KR", relevanceLanguage="ko", videoDuration="short",
                          publishedAfter=after, **s)
            try:
                res = yt("search", **params)
            except Exception as e:
                print(f"[경고] 검색 실패 {cat} {s}: {e}", file=sys.stderr)
                continue
            for it in res.get("items", []):
                vid = it["id"].get("videoId")
                if vid and vid not in found:
                    found[vid] = cat
    print(f"추적 중인 영상: {len(found)}개")

    ids = list(found)
    vids = []
    for i in range(0, len(ids), 50):
        res = yt("videos", part="snippet,statistics,contentDetails", id=",".join(ids[i:i + 50]))
        vids += res.get("items", [])

    ch_ids = list({v["snippet"]["channelId"] for v in vids})
    subs = {}
    for i in range(0, len(ch_ids), 50):
        res = yt("channels", part="statistics", id=",".join(ch_ids[i:i + 50]))
        for c in res.get("items", []):
            st = c.get("statistics", {})
            subs[c["id"]] = 0 if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))

    out = []
    for v in vids:
        sn, st = v["snippet"], v.get("statistics", {})
        dur = iso_dur(v["contentDetails"].get("duration"))
        title = sn.get("title", "")
        if not dur or dur > MAX_SECONDS or not HANGUL.search(title):
            continue
        pub = datetime.fromisoformat(sn["publishedAt"].replace("Z", "+00:00"))
        hours = (NOW - pub).total_seconds() / 3600
        if hours > HOURS_BACK:
            continue
        hours = max(hours, 0.5)
        views = int(st.get("viewCount", 0))
        if views < 1000:
            continue
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
        out.append({
            "id": v["id"], "title": title, "ch": sn.get("channelTitle", ""),
            "subs": max(subs.get(sn["channelId"], 0), 1), "views": views,
            "hours": round(hours, 1), "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)), "cat": found[v["id"]],
            "dur": dur, "thumb": thumb, "kw": [],
        })
    out.sort(key=lambda x: x["views"] / x["hours"], reverse=True)
    kept, per = [], Counter()
    for v in out:
        if per[v["cat"]] < KEEP_PER_CAT:
            kept.append(v)
            per[v["cat"]] += 1
    print("분야별 영상:", dict(per))
    return kept


# ───────── 2. 지금 뜨는 검색어 (구글 트렌드) + 관련 기사 ─────────
def collect_google_trends():
    try:
        raw = get("https://trends.google.com/trending/rss", {"geo": "KR"})
    except Exception as e:
        print(f"[경고] 구글 트렌드 실패: {e}", file=sys.stderr)
        return []
    root = ET.fromstring(raw)
    kws = []
    for item in root.iter("item"):
        word = ctext(item, "title")
        if not word:
            continue
        news = []
        for el in item:
            if el.tag.endswith("news_item"):
                t = ctext(el, "news_item_title")
                u = ctext(el, "news_item_url")
                if t and u:
                    news.append({"title": t, "url": u, "source": ctext(el, "news_item_source")})
        kws.append({"word": word, "src": ["G"], "traffic": ctext(item, "approx_traffic"), "news": news[:5]})
    return kws[:15]


# ───────── 3. 요즘 뜨는 이슈 (구글 뉴스) ─────────
def news_feed(spec, limit=15):
    if spec == "TOP":
        url, params = "https://news.google.com/rss", dict(NEWS_BASE)
    elif spec.startswith("TOPIC:"):
        url = f"https://news.google.com/rss/headlines/section/topic/{spec[6:]}"
        params = dict(NEWS_BASE)
    else:
        url, params = "https://news.google.com/rss/search", dict(NEWS_BASE, q=f"{spec} when:1d")
    try:
        root = ET.fromstring(get(url, params))
    except Exception as e:
        print(f"[경고] 뉴스 실패 {spec}: {e}", file=sys.stderr)
        return []
    items = []
    for it in root.iter("item"):
        title, link = ctext(it, "title"), ctext(it, "link")
        source = ctext(it, "source")
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


def collect_issues():
    issues = {"all": news_feed("TOP", 20)}
    for cat, cfg in CATS.items():
        seen, merged = set(), []
        for spec in cfg["news"]:
            for it in news_feed(spec, 12):
                key = it["title"][:20]
                if key not in seen:
                    seen.add(key)
                    merged.append(it)
        merged.sort(key=lambda x: x["time"], reverse=True)
        issues[cat] = merged[:15]
    return issues


PARTICLES = ("에서는", "으로는", "에게서", "까지", "부터", "에서", "으로", "에게", "보다", "처럼",
             "은", "는", "을", "를", "에")
STOP = {"단독", "속보", "종합", "포토", "영상", "사진", "기자", "뉴스", "오늘", "내일", "어제", "올해",
        "지난", "이번", "관련", "대한", "위해", "통해", "가운데", "있다", "없다", "한다", "했다", "된다",
        "것으로", "그리고", "하지만", "때문", "이후", "이상", "만에", "등", "및", "더", "첫", "전", "후",
        "shorts", "Shorts", "쇼츠", "진짜", "이거"}


def tokens(text):
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|#\S+", " ", text)
    out = []
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", text):
        for p in PARTICLES:
            if len(w) > len(p) + 1 and w.endswith(p):
                w = w[: -len(p)]
                break
        if len(w) >= 2 and w not in STOP and not w.isdigit():
            out.append(w)
    return out


def issue_keywords(issues):
    """여러 언론사 헤드라인에 반복해서 나온 단어 = 지금 대세 이슈"""
    res = {}
    for cat, items in issues.items():
        c = Counter()
        sources = {}
        for it in items:
            for w in set(tokens(it["title"])):
                if w == it.get("source"):
                    continue
                c[w] += 1
                sources.setdefault(w, set()).add(it.get("source"))
        res[cat] = [{"word": w, "n": n} for w, n in c.most_common(30)
                    if n >= 2 and len(sources[w]) >= 2][:10]
    return res


# ───────── 4. 형식 분석 (AI 연결 전, 제목 패턴 기반) ─────────
FORMATS = [
    ("비교형 (A vs B)", r"vs|VS|비교|차이"),
    ("리스트·랭킹형", r"TOP\s?\d|\d+\s?(가지|유형|개)|유형|랭킹|순위"),
    ("질문형 제목", r"\?|？|왜 |뭘까|일까"),
    ("요약·정리형", r"정리|요약|\d+\s?(초|분)\s?(만에|안에|설명|정리)"),
    ("실험·반응형", r"해봤|해봄|해보았|반응|실험|도전|몰카"),
    ("이슈·속보형", r"속보|단독|논란|근황|결국|충격"),
]


def analyze(videos):
    if not videos:
        return {"lead": "아직 수집된 영상이 없어요.", "paras": [], "tips": [], "words": []}, []
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

    top = sorted(videos, key=vph, reverse=True)[:15]
    avg_dur = round(sum(v["dur"] for v in top) / len(top))
    chs = {v["ch"] for v in videos}
    words = Counter()
    for v in top:
        for w in set(tokens(v["title"])):
            if w not in chs:
                words[w] += 1
    common = [w for w, c in words.most_common(8) if c >= 2][:5]
    by_cat = Counter(v["cat"] for v in top)
    best_cat = CATS[by_cat.most_common(1)[0][0]]["label"]
    small = [v for v in videos if v["views"] / v["subs"] >= 10]

    lead = f"상위 쇼츠 15개 중 {by_cat.most_common(1)[0][1]}개가 {best_cat} 분야예요."
    paras = [f"상위권 평균 길이는 {avg_dur}초예요."]
    if small:
        paras.append(f"구독자 수의 10배 넘게 조회된 영상이 {len(small)}개 있어요. ‘작은 채널인데 터진 영상’에서 확인해 보세요.")
    tips = []
    if formats:
        tips.append(f"지금 가장 잘 되는 제목 형식: {formats[0]['name']} (평균보다 {formats[0]['lift']})")
    tips.append(f"길이는 {max(avg_dur - 10, 15)}~{avg_dur + 10}초 사이를 노려보세요")
    return {"lead": lead, "paras": paras, "tips": tips, "words": common,
            "top": [v["id"] for v in top], "auto": True}, formats


# ───────── 실행 ─────────
def main():
    if not API_KEY:
        sys.exit("YOUTUBE_API_KEY가 없어요. GitHub > Settings > Secrets and variables > Actions 에 추가하세요.")
    prev = load_prev()
    prev_views = {v["id"]: v["views"] for v in prev.get("videos", [])}
    prev_rank = {k["word"]: i for i, k in enumerate(prev.get("keywords", []))}

    videos = collect_videos(prev)
    for v in videos:
        if v["id"] in prev_views:
            v["gain"] = max(v["views"] - prev_views[v["id"]], 0)

    keywords = collect_google_trends()
    for i, k in enumerate(keywords):
        k["chg"] = "new" if k["word"] not in prev_rank else prev_rank[k["word"]] - i
        hits = [v for v in videos if k["word"].replace(" ", "") in v["title"].replace(" ", "")]
        for v in hits:
            v["kw"].append(k["word"])
        k["cat"] = Counter(v["cat"] for v in hits).most_common(1)[0][0] if hits else None

    issues = collect_issues()
    summary, formats = analyze(videos)
    data = {
        "updatedAt": NOW.astimezone(KST).isoformat(timespec="minutes"),
        "cats": {k: {"label": c["label"]} for k, c in CATS.items()},
        "keywords": keywords,
        "issues": issues,
        "issueWords": issue_keywords(issues),
        "videos": videos,
        "summary": summary,
        "formats": formats,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"완료: 영상 {len(videos)}개, 검색어 {len(keywords)}개, 이슈 {sum(len(v) for v in issues.values())}건 → {OUT}")


if __name__ == "__main__":
    main()
