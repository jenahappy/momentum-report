#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매일 오전 8시(KST)에 실행되어 momentum-report 보고서의
'오늘의 뉴스 브리핑' 섹션을 갱신한다.

흐름:
  1) 종목/시장별 최신 뉴스 헤드라인을 구글뉴스 RSS(무료)에서 수집
  2) 수집한 뉴스를 Groq API(무료 등급)에 보내 '오늘의 브리핑' HTML 조각 생성
  3) template.html 의 DAILY_BRIEFING 마커 사이에 끼워 넣고 날짜를 갱신해 index.html 저장

전체 보고서를 매번 재생성하지 않는 이유: Groq 무료 등급은 분당 12,000 토큰
한도가 있어 27KB 문서 전체(약 28,600 토큰)는 한 번에 처리할 수 없다. 따라서
무거운 분석 본문은 안정적으로 유지하고, 뉴스 브리핑 섹션만 매일 새로 쓴다.

표준 라이브러리만 사용한다(추가 pip 설치 불필요).
환경변수 GROQ_API_KEY 필요. GROQ_MODEL 로 모델 변경 가능.
"""

import os
import re
import sys
import json
import time
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")

# template.html 에 박혀 있는 기준일(이 문자열을 오늘 날짜로 치환)
BASELINE_DATE = "2026-06-02"

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "template.html")
OUTPUT_PATH = os.path.join(HERE, "index.html")

MARKER_START = "<!-- DAILY_BRIEFING_START -->"
MARKER_END = "<!-- DAILY_BRIEFING_END -->"

# 뉴스를 수집할 대상 (보고서의 핵심 5종목 + 시장)
NEWS_TARGETS = [
    ("시장(코스피)", "코스피 증시 전망"),
    ("SK하이닉스", "SK하이닉스 HBM"),
    ("알테오젠", "알테오젠"),
    ("한화에어로스페이스", "한화에어로스페이스 수주"),
    ("두산에너빌리티", "두산에너빌리티 SMR 가스터빈"),
    ("KB금융", "KB금융 주주환원"),
]

HEADLINES_PER_TARGET = 5


# ---------------------------------------------------------------------------
# 1) 뉴스 수집 (구글뉴스 RSS)
# ---------------------------------------------------------------------------
def fetch_news(query, limit=HEADLINES_PER_TARGET):
    q = urllib.parse.quote(query)
    url = (
        f"https://news.google.com/rss/search?q={q}"
        f"+when:7d&hl=ko&gl=KR&ceid=KR:ko"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    items = []
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        root = ET.fromstring(data)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            src_el = item.find("source")
            src = (src_el.text.strip() if src_el is not None and src_el.text else "")
            if title:
                items.append({"title": title, "date": pub, "source": src})
            if len(items) >= limit:
                break
    except Exception as e:  # 한 종목 실패가 전체를 막지 않도록
        print(f"[warn] 뉴스 수집 실패 ({query}): {e}", file=sys.stderr)
    return items


def collect_all_news():
    blocks = []
    for label, query in NEWS_TARGETS:
        items = fetch_news(query)
        lines = [f"### {label}"]
        if not items:
            lines.append("- (최근 뉴스 없음)")
        for it in items:
            lines.append(f"- {it['title']} ({it['source']})")
        blocks.append("\n".join(lines))
        time.sleep(1)  # RSS 서버 예의상 간격
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# 2) Groq 호출 — '오늘의 브리핑' HTML 조각 생성
# ---------------------------------------------------------------------------
BRIEFING_SYSTEM = """너는 한국 증시 애널리스트다. 제공된 '오늘의 뉴스 헤드라인'만 근거로,
모멘텀 포트폴리오 5종목(SK하이닉스·알테오젠·한화에어로스페이스·두산에너빌리티·KB금융)과
시장(코스피)에 대한 '오늘의 브리핑'을 간결한 한국어 HTML 조각으로 작성한다.

반드시 지킬 규칙:
1. 제공된 헤드라인 밖의 사실을 지어내지 마라. 구체적 주가·목표가·수치는 날조 금지.
2. 시장 한 줄 요약 + 종목별 한 줄 코멘트(뉴스가 있는 종목 위주) 형식으로 짧게.
3. 기존 페이지의 CSS 클래스만 사용: 컨테이너는 <div class="card">, 긍정 <span class="pos">, 부정 <span class="neg">, 출처/부가설명 <span class="src">. 인라인 style 쓰지 마라.
4. 새 <h2>/<h1> 제목을 만들지 마라(제목은 외부에서 붙인다). <div class="card"> 한두 개만 출력.
5. 출력은 HTML 조각만. 코드펜스(```), 머리말, 설명 없이 <div 로 시작해 </div> 로 끝내라.
6. 맨 끝 카드에 <span class="src">자동 생성(뉴스 기반)·투자 참고용·매매 전 시세 재확인 필수</span> 한 줄을 포함하라."""


def build_briefing_prompt(news_text):
    return (
        f"오늘 날짜(KST): {TODAY}\n\n"
        f"=== 오늘의 최신 뉴스 헤드라인 (구글뉴스, 최근 7일) ===\n"
        f"{news_text}\n\n"
        f"위 뉴스만 근거로 '오늘의 브리핑' HTML 조각을 작성하라."
    )


def call_groq(system_prompt, user_prompt, retries=3):
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 3000,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        # User-Agent가 없으면 Cloudflare가 봇으로 보고 차단(error 1010)함
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(GROQ_URL, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return result["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            last_err = f"HTTP {e.code}: {detail}"
            # 일시적 한도 초과/서버 오류만 재시도
            if e.code in (429, 500, 502, 503):
                wait = 20 * attempt
                print(f"[warn] {last_err} → {wait}s 후 재시도 ({attempt}/{retries})",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            break
        except Exception as e:
            last_err = str(e)
            print(f"[warn] 호출 실패 → 재시도 ({attempt}/{retries}): {e}", file=sys.stderr)
            time.sleep(10 * attempt)
    raise RuntimeError(f"Groq 호출 실패: {last_err}")


def clean_html(text):
    """모델이 혹시 코드펜스를 붙였을 때 제거."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    return t.strip()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    if not GROQ_API_KEY:
        print("[error] 환경변수 GROQ_API_KEY 가 설정되지 않았습니다.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(TEMPLATE_PATH):
        print(f"[error] template.html 이 없습니다: {TEMPLATE_PATH}", file=sys.stderr)
        sys.exit(1)

    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template_html = f.read()

    if MARKER_START not in template_html or MARKER_END not in template_html:
        print("[error] template.html 에 DAILY_BRIEFING 마커가 없습니다.", file=sys.stderr)
        sys.exit(1)

    print(f"[info] {TODAY} 뉴스 수집 중...")
    news_text = collect_all_news()
    print(f"[info] 뉴스 수집 완료 ({len(news_text)}자). Groq({GROQ_MODEL}) 호출 중...")

    briefing = clean_html(call_groq(BRIEFING_SYSTEM, build_briefing_prompt(news_text)))

    if "<div" not in briefing.lower():
        print("[error] 브리핑 출력이 올바른 HTML 조각이 아닙니다. 갱신을 건너뜁니다.",
              file=sys.stderr)
        print(briefing[:500], file=sys.stderr)
        sys.exit(1)

    # 마커 사이를 새 브리핑으로 교체 (마커 자체는 유지)
    new_block = (
        f"{MARKER_START}\n"
        f'<h2>📰 오늘의 뉴스 브리핑 <span class="src">({TODAY} 자동 갱신)</span></h2>\n'
        f"{briefing}\n"
        f"{MARKER_END}"
    )
    page = re.sub(
        re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END),
        lambda _: new_block,            # 치환문에 백슬래시/그룹기호가 섞여도 안전
        template_html,
        flags=re.DOTALL,
    )

    # 보고서 상단/하단의 기준일을 오늘로 갱신
    page = page.replace(BASELINE_DATE, TODAY)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"[info] index.html 갱신 완료 (브리핑 {len(briefing)}자, 전체 {len(page)}자).")


if __name__ == "__main__":
    main()
