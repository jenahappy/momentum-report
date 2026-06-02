#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
매일 오전 8시(KST)에 실행되어 momentum-report 보고서를 갱신한다.

흐름:
  1) 종목/시장별 최신 뉴스 헤드라인을 구글뉴스 RSS(무료)에서 수집
  2) template.html(원본 보고서 틀)과 수집된 뉴스를 Groq API(무료 등급)에 전달
  3) Groq가 오늘 날짜 기준으로 갱신한 완전한 HTML을 받아 index.html에 저장

표준 라이브러리만 사용한다(추가 pip 설치 불필요).
환경변수 GROQ_API_KEY 필요. GROQ_MODEL 로 모델 변경 가능.
"""

import os
import sys
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
KST = timezone(timedelta(hours=9))
TODAY = datetime.now(KST).strftime("%Y-%m-%d")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "template.html")
OUTPUT_PATH = os.path.join(HERE, "index.html")

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
    except Exception as e:  # 뉴스 한 종목 실패가 전체를 막지 않도록
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
            lines.append(f"- {it['title']} ({it['source']}, {it['date']})")
        blocks.append("\n".join(lines))
        time.sleep(1)  # RSS 서버 예의상 간격
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# 2) Groq 호출
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """너는 한국 주식 '6개월 모멘텀 집중 포트폴리오 보고서'를 매일 갱신하는 전문 금융 에디터다.
주어진 HTML 템플릿의 구조·CSS·스타일·표 형식을 100% 그대로 유지하면서,
제공된 '오늘의 최신 뉴스'를 반영해 본문을 오늘 날짜 기준으로 갱신한다.

반드시 지킬 규칙:
1. 정확한 실시간 주가·목표가를 지어내지 마라. 템플릿의 숫자는 그대로 두고, 뉴스로 확인된 정성적 변화(모멘텀 강도, 이슈)만 반영하라. 시세가 바뀌었을 가능성이 있으면 '매매 전 시세 재확인' 표기를 유지/강화하라.
2. 제공되지 않은 뉴스나 사실을 절대 날조하지 마라. 근거는 제공된 헤드라인 범위 안에서만 사용하라.
3. 상단 제목과 분석 기준일을 오늘 날짜로 갱신하라.
4. '시장 국면 진단', 각 종목 '최근 모멘텀', '한 줄 결론'을 최신 뉴스 흐름에 맞게 업데이트하라.
5. 투자 유의(면책) 문구는 반드시 유지하고, 자동 생성물임을 한 줄 덧붙여라.
6. 출력은 완전한 단일 HTML 문서 하나만 출력하라. 코드펜스(```), 설명, 머리말 없이 <!DOCTYPE html> 부터 </html> 까지만 출력하라."""


def build_user_prompt(news_text, template_html):
    return (
        f"오늘 날짜(KST): {TODAY}\n\n"
        f"=== 오늘의 최신 뉴스 (구글뉴스 RSS, 최근 7일) ===\n"
        f"{news_text}\n\n"
        f"=== 갱신할 HTML 템플릿 (이 구조와 스타일을 그대로 유지) ===\n"
        f"{template_html}\n\n"
        f"위 뉴스를 반영해 오늘 날짜({TODAY}) 기준으로 갱신한 완전한 HTML 문서만 출력하라."
    )


def call_groq(system_prompt, user_prompt, retries=3):
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.4,
        "max_tokens": 16000,
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
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
            # 429(요청 과다) 등은 잠시 후 재시도
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
        # 첫 줄(``` 또는 ```html) 제거
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

    print(f"[info] {TODAY} 뉴스 수집 중...")
    news_text = collect_all_news()
    print(f"[info] 뉴스 수집 완료 ({len(news_text)}자). Groq({GROQ_MODEL}) 호출 중...")

    output = call_groq(SYSTEM_PROMPT, build_user_prompt(news_text, template_html))
    output = clean_html(output)

    if "<html" not in output.lower():
        print("[error] 모델 출력이 올바른 HTML이 아닙니다. index.html 갱신을 건너뜁니다.",
              file=sys.stderr)
        print(output[:500], file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"[info] index.html 갱신 완료 ({len(output)}자).")


if __name__ == "__main__":
    main()
