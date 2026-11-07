"""
W.D (Worry Data Trap) 데이터 수집기
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

KST = timezone(timedelta(hours=9))
NOW = datetime.now(timezone.utc)
API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
OUT = os.environ.get("WD_OUT", "data.json")

# ───────── 분야 설정: 여기만 고치면 수집 대상이 바뀝니다 ─────────
# videoCategoryId: 23=코미디, 24=엔터테인먼트, 25=뉴스/정치
# 검색 1번 = 할당량 100 사용 (하루 무료 10,000). 지금 설정: 6번 × 12회/일 ≈ 7,300
CATS = {
    "fun": {"label": "유머", "searches": [
        {"q": "#shorts", "videoCategoryId": "23"},
        {"q": "웃긴 쇼츠"},
    ], "suggest": ["웃긴", "상황극", "밈"]},
    "pol": {"label": "정치/시사", "searches": [
        {"q": "#shorts", "videoCategoryId": "25"},
        {"q": "시사 정리 쇼츠"},
    ], "suggest": ["시사", "국회", "뉴스 정리"]},
    "ad": {"label": "광고/마케팅", "searches": [
        {"q": "광고 쇼츠"},
        {"q": "마케팅 쇼츠"},
    ], "suggest": ["광고", "마케팅", "브랜드"]},
}
HOURS_BACK = 48          # 최근 몇 시간 안에 올라온 영상만
MAX_SECONDS = 180        # 쇼츠로 볼 최대 길이(초)
HANGUL = re.compile(r"[가-힣]")


def get(url, params=None, timeout=20):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "WD-dashboard/1.0"})
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


# ───────── 1. 유튜브 쇼츠 ─────────
def collect_videos():
    after = (NOW - timedelta(hours=HOURS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = {}  # id -> cat
    for cat, cfg in CATS.items():
        for s in cfg["searches"]:
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
    print(f"검색으로 찾은 영상: {len(found)}개")

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
        hours = max((NOW - pub).total_seconds() / 3600, 0.5)
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
    return out[:60]


# ───────── 2. 검색어: 구글 트렌드(한국) + 유튜브 자동완성 ─────────
def collect_google_trends():
    try:
        raw = get("https://trends.google.com/trending/rss", {"geo": "KR"})
    except Exception as e:
        print(f"[경고] 구글 트렌드 실패: {e}", file=sys.stderr)
        return []
    ns = {"ht": "https://trends.google.com/trending/rss"}
    root = ET.fromstring(raw)
    kws = []
    for item in root.iter("item"):
        word = (item.findtext("title") or "").strip()
        traffic = ""
        for el in item:
            if el.tag.endswith("approx_traffic"):
                traffic = (el.text or "").strip()
        if word:
            kws.append({"word": word, "src": ["G"], "traffic": traffic})
    return kws[:15]


def collect_suggest():
    res = {}
    for cat, cfg in CATS.items():
        words = []
        for seed in cfg["suggest"]:
            try:
                raw = get("https://suggestqueries.google.com/complete/search",
                          {"client": "firefox", "ds": "yt", "hl": "ko", "q": seed})
                for w in json.loads(raw.decode("utf-8", "ignore"))[1]:
                    if w != seed and w not in words:
                        words.append(w)
            except Exception as e:
                print(f"[경고] 자동완성 실패 {seed}: {e}", file=sys.stderr)
        res[cat] = words[:8]
    return res


# ───────── 3. 형식 분석 (AI 연결 전, 제목 패턴 기반) ─────────
FORMATS = [
    ("비교형 (A vs B)", r"vs|VS|비교|차이"),
    ("리스트·유형형", r"TOP\s?\d|\d+\s?(가지|유형|개)|유형"),
    ("질문형 제목", r"\?|？|왜 |뭘까|일까"),
    ("요약·정리형", r"정리|요약|\d+\s?(초|분)\s?(만에|안에|설명|정리)"),
    ("실험·반응형", r"해봤|해봄|해보았|반응|실험|도전|몰카"),
    ("이슈·속보형", r"속보|단독|논란|근황|결국"),
]


def analyze(videos):
    if not videos:
        return {"lead": "아직 수집된 영상이 없어요.", "paras": [], "tips": []}, []
    vph = lambda v: v["views"] / v["hours"]
    avg = sum(vph(v) for v in videos) / len(videos)
    formats = []
    for name, pat in FORMATS:
        hit = [v for v in videos if re.search(pat, v["title"])]
        if len(hit) >= 2:
            lift = (sum(vph(v) for v in hit) / len(hit)) / avg - 1
            names = []
            for h in sorted(hit, key=vph, reverse=True):
                t = re.sub(r"#\S+", "", h["title"]).strip()[:20]
                if t and t not in names:
                    names.append(t)
            ex = ", ".join(names[:2])
            formats.append({"name": name, "lift": f"{lift:+.0%}", "n": len(hit), "ex": ex, "_l": lift})
    formats.sort(key=lambda f: f["_l"], reverse=True)
    for f in formats:
        f.pop("_l")

    top = sorted(videos, key=vph, reverse=True)[:15]
    avg_dur = round(sum(v["dur"] for v in top) / len(top))
    words = Counter()
    for v in top:
        for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", re.sub(r"#\S+", "", v["title"])):
            if w not in {"쇼츠", "shorts", "Shorts", "그리고", "진짜", "이거", "ㅋㅋ"}:
                words[w] += 1
    common = [w for w, c in words.most_common(6) if c >= 2][:4]
    by_cat = Counter(v["cat"] for v in top)
    best_cat = CATS[by_cat.most_common(1)[0][0]]["label"]
    small = [v for v in videos if v["views"] / v["subs"] >= 10]

    lead = f"상위 쇼츠 15개 중 {by_cat.most_common(1)[0][1]}개가 {best_cat} 분야예요."
    paras = [f"상위권 평균 길이는 {avg_dur}초예요."
             + (f" 제목에 자주 나온 말은 ‘{'’, ‘'.join(common)}’이에요." if common else "")]
    if small:
        paras.append(f"구독자 수의 10배 넘게 조회된 영상이 {len(small)}개 있어요. ‘작은 채널인데 터진 영상’에서 확인해 보세요.")
    tips = []
    if formats:
        tips.append(f"지금 가장 잘 되는 제목 형식: {formats[0]['name']} (평균보다 {formats[0]['lift']})")
    tips.append(f"길이는 {max(avg_dur - 10, 15)}~{avg_dur + 10}초 사이를 노려보세요")
    return {"lead": lead, "paras": paras, "tips": tips, "auto": True}, formats


# ───────── 실행 ─────────
def main():
    if not API_KEY:
        sys.exit("YOUTUBE_API_KEY가 없어요. GitHub > Settings > Secrets and variables > Actions 에 추가하세요.")
    prev = load_prev()
    prev_views = {v["id"]: v["views"] for v in prev.get("videos", [])}
    prev_rank = {k["word"]: i for i, k in enumerate(prev.get("keywords", []))}

    videos = collect_videos()
    for v in videos:
        if v["id"] in prev_views:
            v["gain"] = v["views"] - prev_views[v["id"]]

    keywords = collect_google_trends()
    for i, k in enumerate(keywords):
        k["chg"] = "new" if k["word"] not in prev_rank else prev_rank[k["word"]] - i
        # 검색어가 들어간 영상 연결 → 분야 추정
        hits = [v for v in videos if k["word"].replace(" ", "") in v["title"].replace(" ", "")]
        for v in hits:
            v["kw"].append(k["word"])
        k["cat"] = Counter(v["cat"] for v in hits).most_common(1)[0][0] if hits else None

    summary, formats = analyze(videos)
    data = {
        "updatedAt": NOW.astimezone(KST).isoformat(timespec="minutes"),
        "cats": {k: {"label": c["label"]} for k, c in CATS.items()},
        "keywords": keywords,
        "suggest": collect_suggest(),
        "videos": videos,
        "summary": summary,
        "formats": formats,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"완료: 영상 {len(videos)}개, 검색어 {len(keywords)}개 → {OUT}")


if __name__ == "__main__":
    main()
