#!/usr/bin/env python3
"""
F&F 브랜드 이미지 모니터링 시스템
===================================
네이버 뉴스에서 특정 키워드 기사를 수집하고,
기사 이미지를 Claude Vision API로 분석하여
MLB/F&F 브랜드 로고 노출 여부를 감지합니다.

GitHub Actions에서 1분 간격으로 실행됩니다.
"""

import os
import json
import time
import base64
import hashlib
import logging
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── 설정 ────────────────────────────────────────────────
SEARCH_KEYWORDS = ["박왕열", "마약왕", "마약왕 박왕열"]  # 모니터링 키워드 (여러 개 가능)
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# 알림 설정 (둘 다 가능)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
GMAIL_SENDER = os.environ.get("GMAIL_SENDER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT", "")

# 이미 분석한 기사를 추적하는 파일
HISTORY_FILE = "monitoring_history.json"
RESULTS_FILE = "detection_results.json"
MAX_ARTICLES_PER_RUN = 20
DISPLAY_COUNT = 100  # 네이버 API 한번에 가져올 수

KST = timezone(timedelta(hours=9))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("brand-monitor")


# ─── 1. 네이버 뉴스 검색 ─────────────────────────────────
def search_naver_news(keyword: str, display: int = 100, sort: str = "date") -> list:
    """네이버 뉴스 검색 API로 기사 목록을 가져옵니다."""
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        log.error("네이버 API 키가 설정되지 않았습니다.")
        return []

    url = "https://openapi.naver.com/v1/search/news.json"
    params = urllib.parse.urlencode({
        "query": keyword,
        "display": display,
        "start": 1,
        "sort": sort,  # date=최신순, sim=정확도순
    })
    full_url = f"{url}?{params}"

    req = urllib.request.Request(full_url)
    req.add_header("X-Naver-Client-Id", NAVER_CLIENT_ID)
    req.add_header("X-Naver-Client-Secret", NAVER_CLIENT_SECRET)

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            articles = data.get("items", [])
            log.info(f"네이버 뉴스 {len(articles)}건 수집 (키워드: {keyword})")
            return articles
    except Exception as e:
        log.error(f"네이버 뉴스 검색 실패: {e}")
        return []


# ─── 2. 기사 본문에서 이미지 URL 추출 ────────────────────
def extract_images_from_article(article_url: str) -> list:
    """기사 페이지를 가져와서 이미지 URL을 추출합니다."""
    try:
        req = urllib.request.Request(article_url)
        req.add_header("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.warning(f"기사 페이지 접근 실패: {article_url} — {e}")
        return []

    # 간단한 이미지 URL 추출 (정규식 사용)
    import re
    images = []

    # og:image 메타태그 (가장 대표적인 기사 이미지)
    og_match = re.search(
        r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    )
    if not og_match:
        og_match = re.search(
            r'content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']',
            html, re.IGNORECASE
        )
    if og_match:
        images.append(og_match.group(1))

    # 네이버 뉴스 기사 본문 내 이미지
    # img_desc 클래스 (네이버 뉴스 본문 이미지)
    for m in re.finditer(
        r'<img[^>]+(?:id=["\']img_a\d+["\']|class=["\'][^"\']*(?:nbd_a|img_desc|photo_bx|newsimg)[^"\']*["\'])[^>]+src=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    ):
        images.append(m.group(1))

    # data-src 패턴 (lazy loading)
    for m in re.finditer(
        r'data-src=["\']([^"\']+\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?)["\']',
        html, re.IGNORECASE
    ):
        images.append(m.group(1))

    # 일반 img 태그에서 뉴스 관련 이미지
    for m in re.finditer(
        r'<img[^>]+src=["\']([^"\']+(?:imgnews|image\.news|photo|upload|article)[^"\']*\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?)["\']',
        html, re.IGNORECASE
    ):
        images.append(m.group(1))

    # 중복 제거 및 필터링
    seen = set()
    filtered = []
    for img_url in images:
        # 상대경로 처리
        if img_url.startswith("//"):
            img_url = "https:" + img_url
        elif img_url.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(article_url)
            img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"

        # 너무 작은 이미지(아이콘 등) 제외
        if any(x in img_url.lower() for x in ["icon", "logo_", "btn_", "1x1", "blank", "spacer", "ad_"]):
            continue

        if img_url not in seen:
            seen.add(img_url)
            filtered.append(img_url)

    log.info(f"  이미지 {len(filtered)}개 추출: {article_url[:60]}...")
    return filtered[:5]  # 기사당 최대 5개 이미지


# ─── 3. 이미지 다운로드 → base64 ─────────────────────────
def download_image_as_base64(image_url: str) -> tuple:
    """이미지를 다운로드하여 base64로 변환합니다. (url, base64, media_type) 반환"""
    try:
        req = urllib.request.Request(image_url)
        req.add_header("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
        req.add_header("Referer", "https://news.naver.com/")
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            data = resp.read()

            # 파일 크기 체크 (10KB 미만이면 아이콘일 가능성)
            if len(data) < 10_000:
                return None

            # media_type 결정
            if "png" in content_type:
                media_type = "image/png"
            elif "webp" in content_type:
                media_type = "image/webp"
            elif "gif" in content_type:
                media_type = "image/gif"
            else:
                media_type = "image/jpeg"

            b64 = base64.b64encode(data).decode("utf-8")
            return (image_url, b64, media_type)
    except Exception as e:
        log.warning(f"  이미지 다운로드 실패: {image_url[:60]}... — {e}")
        return None


# ─── 4. Claude Vision API로 브랜드 로고 분석 ─────────────
ANALYSIS_PROMPT = """이 뉴스 기사 이미지를 분석하여 다음 브랜드 로고나 마크가 보이는지 확인해주세요:

1. MLB (메이저리그 베이스볼) 로고 — 모자, 의류 등에 있는 MLB 공식 로고
2. NY (뉴욕 양키스) 로고 — 모자나 의류의 NY 엠블럼
3. LA (LA 다저스) 로고
4. 기타 MLB 팀 로고 (보스턴 레드삭스 B, SF 자이언츠 등)
5. F&F 브랜드: Discovery Expedition, Duvetica, Stretch Angels
6. 모자에 있는 팀 로고나 브랜드 마크 전반

특히 사람이 쓰고 있는 '모자'에 주목해주세요.

반드시 아래 JSON 형식으로만 응답하세요 (다른 텍스트 없이):
{
  "logo_detected": true/false,
  "confidence": "high/medium/low",
  "detected_brands": ["브랜드명1", "브랜드명2"],
  "cap_detected": true/false,
  "cap_description": "모자에 대한 설명",
  "description": "전체 이미지에 대한 간단한 설명",
  "risk_level": "high/medium/low/none",
  "recommendation": "조치 권고사항"
}"""


def analyze_image_with_claude(image_b64: str, media_type: str) -> dict:
    """Claude Vision API로 이미지를 분석합니다."""
    if not ANTHROPIC_API_KEY:
        log.error("Anthropic API 키가 설정되지 않았습니다.")
        return {"error": "API key missing"}

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64,
                    }
                },
                {
                    "type": "text",
                    "text": ANALYSIS_PROMPT,
                }
            ]
        }]
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("x-api-key", ANTHROPIC_API_KEY)
    req.add_header("anthropic-version", "2023-06-01")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            text = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text += block.get("text", "")

            # JSON 파싱
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            return json.loads(text)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        log.error(f"Claude API 에러 {e.code}: {body[:200]}")
        return {"error": f"API error {e.code}"}
    except json.JSONDecodeError as e:
        log.error(f"Claude 응답 JSON 파싱 실패: {e}")
        return {"error": "JSON parse error", "raw": text[:200] if text else ""}
    except Exception as e:
        log.error(f"Claude API 호출 실패: {e}")
        return {"error": str(e)}


# ─── 5. 알림 발송 ────────────────────────────────────────
def send_slack_alert(article: dict, analysis: dict, image_url: str):
    """슬랙 웹훅으로 알림을 보냅니다."""
    if not SLACK_WEBHOOK_URL:
        return

    brands = ", ".join(analysis.get("detected_brands", []))
    risk = analysis.get("risk_level", "unknown")
    risk_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(risk, "⚪")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚨 브랜드 로고 감지 — {risk_emoji} {risk.upper()}"}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*기사:* {article.get('title', 'N/A')}\n"
                    f"*출처:* {article.get('source', 'N/A')}\n"
                    f"*감지 브랜드:* {brands}\n"
                    f"*모자:* {analysis.get('cap_description', 'N/A')}\n"
                    f"*권고:* {analysis.get('recommendation', 'N/A')}\n"
                    f"*기사 링크:* <{article.get('link', '#')}|원문 보기>"
                )
            }
        },
        {
            "type": "image",
            "image_url": image_url,
            "alt_text": "감지된 이미지",
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"_F&F Brand Monitor · {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}_"}
            ]
        }
    ]

    payload = json.dumps({"blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")

    try:
        urllib.request.urlopen(req, timeout=10)
        log.info("슬랙 알림 발송 완료")
    except Exception as e:
        log.error(f"슬랙 알림 실패: {e}")


def send_email_alert(article: dict, analysis: dict, image_url: str):
    """Gmail SMTP로 이메일 알림을 보냅니다."""
    if not GMAIL_SENDER or not GMAIL_APP_PASSWORD or not GMAIL_RECIPIENT:
        return

    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    brands = ", ".join(analysis.get("detected_brands", []))
    risk = analysis.get("risk_level", "unknown")

    subject = f"🚨 [F&F 브랜드 모니터] 로고 감지 — {risk.upper()} — {brands}"

    html = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 600px; margin: 0 auto;">
        <div style="background: #D32F2F; color: white; padding: 16px 24px; border-radius: 8px 8px 0 0;">
            <h2 style="margin: 0;">🚨 브랜드 로고 감지</h2>
            <p style="margin: 4px 0 0; opacity: 0.9;">위험도: {risk.upper()}</p>
        </div>
        <div style="border: 1px solid #eee; border-top: none; padding: 24px; border-radius: 0 0 8px 8px;">
            <table style="width: 100%; border-collapse: collapse;">
                <tr>
                    <td style="padding: 8px 0; color: #888; width: 100px;">기사 제목</td>
                    <td style="padding: 8px 0; font-weight: 600;">{article.get('title', 'N/A')}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #888;">출처</td>
                    <td style="padding: 8px 0;">{article.get('source', 'N/A')}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #888;">감지 브랜드</td>
                    <td style="padding: 8px 0; color: #D32F2F; font-weight: 700;">{brands}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #888;">모자 분석</td>
                    <td style="padding: 8px 0;">{analysis.get('cap_description', 'N/A')}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #888;">권고</td>
                    <td style="padding: 8px 0;">{analysis.get('recommendation', 'N/A')}</td>
                </tr>
            </table>
            <div style="margin-top: 16px;">
                <a href="{article.get('link', '#')}"
                   style="display: inline-block; background: #D32F2F; color: white; padding: 10px 24px;
                          border-radius: 6px; text-decoration: none; font-weight: 600;">
                    원문 기사 보기 →
                </a>
            </div>
            <p style="margin-top: 24px; font-size: 12px; color: #aaa;">
                F&F Communications Team · Brand Safety Monitor<br>
                {datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')}
            </p>
        </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_SENDER
    msg["To"] = GMAIL_RECIPIENT
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECIPIENT, msg.as_string())
        log.info("이메일 알림 발송 완료")
    except Exception as e:
        log.error(f"이메일 발송 실패: {e}")


# ─── 6. 이력 관리 ────────────────────────────────────────
def load_history() -> dict:
    """이미 분석한 기사 이력을 로드합니다."""
    if Path(HISTORY_FILE).exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"analyzed_urls": [], "last_run": None}


def save_history(history: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def load_results() -> list:
    if Path(RESULTS_FILE).exists():
        with open(RESULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_results(results: list):
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


# ─── 7. 네이버 뉴스 링크 정규화 ──────────────────────────
def get_naver_link(article: dict) -> str:
    """네이버 뉴스 링크를 우선 반환, 없으면 originallink 반환"""
    # 네이버 뉴스 링크가 있으면 우선
    link = article.get("link", "")
    orig = article.get("originallink", link)

    # 네이버 뉴스 링크(n.news.naver.com)면 이미지 추출이 더 안정적
    if "news.naver.com" in link:
        return link
    return orig


def clean_title(title: str) -> str:
    """HTML 태그 제거"""
    import re
    return re.sub(r'<[^>]+>', '', title).strip()


# ─── 메인 실행 ───────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("F&F 브랜드 이미지 모니터링 시작")
    log.info(f"키워드: {', '.join(SEARCH_KEYWORDS)}")
    log.info(f"시각: {datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S KST')}")
    log.info("=" * 60)

    history = load_history()
    results = load_results()
    analyzed_urls = set(history.get("analyzed_urls", []))
    new_detections = []

    # 1) 모든 키워드로 네이버 뉴스 검색
    all_articles = []
    seen_links = set()
    for keyword in SEARCH_KEYWORDS:
        articles = search_naver_news(keyword, display=DISPLAY_COUNT)
        for art in articles:
            link = get_naver_link(art)
            if link not in seen_links:
                seen_links.add(link)
                all_articles.append(art)
    
    if not all_articles:
        log.warning("수집된 기사가 없습니다. 종료합니다.")
        return
    
    log.info(f"전체 키워드에서 중복 제거 후 {len(all_articles)}건 수집")

    # 2) 새로운 기사만 필터링
    new_articles = []
    for art in all_articles:
        url = get_naver_link(art)
        url_hash = hashlib.md5(url.encode()).hexdigest()
        if url_hash not in analyzed_urls:
            new_articles.append(art)

    log.info(f"새 기사 {len(new_articles)}건 발견 (기존 분석: {len(analyzed_urls)}건)")

    if not new_articles:
        log.info("새로운 기사가 없습니다. 종료합니다.")
        history["last_run"] = datetime.now(KST).isoformat()
        save_history(history)
        return

    # 최대 처리 제한
    new_articles = new_articles[:MAX_ARTICLES_PER_RUN]

    # 3) 각 기사 처리
    for i, art in enumerate(new_articles, 1):
        title = clean_title(art.get("title", ""))
        source = art.get("source", "알 수 없음")  # 네이버 API는 source 필드 없음
        link = get_naver_link(art)
        url_hash = hashlib.md5(link.encode()).hexdigest()

        log.info(f"\n[{i}/{len(new_articles)}] {title[:50]}...")
        log.info(f"  URL: {link[:80]}...")

        # 이미지 추출
        images = extract_images_from_article(link)
        if not images:
            log.info("  → 이미지 없음, 스킵")
            analyzed_urls.add(url_hash)
            continue

        # 각 이미지 분석
        article_detected = False
        for img_url in images:
            # 이미지 다운로드
            result = download_image_as_base64(img_url)
            if not result:
                continue
            _, img_b64, media_type = result

            # Claude Vision 분석
            log.info(f"  → Claude Vision 분석 중: {img_url[:60]}...")
            analysis = analyze_image_with_claude(img_b64, media_type)

            if analysis.get("error"):
                log.error(f"  → 분석 에러: {analysis['error']}")
                time.sleep(2)  # Rate limit 방지
                continue

            logo_detected = analysis.get("logo_detected", False)
            confidence = analysis.get("confidence", "low")
            detected_brands = analysis.get("detected_brands", [])
            risk_level = analysis.get("risk_level", "none")

            # 결과 기록
            record = {
                "timestamp": datetime.now(KST).isoformat(),
                "article_title": title,
                "article_url": link,
                "image_url": img_url,
                "logo_detected": logo_detected,
                "confidence": confidence,
                "detected_brands": detected_brands,
                "risk_level": risk_level,
                "cap_detected": analysis.get("cap_detected", False),
                "cap_description": analysis.get("cap_description", ""),
                "description": analysis.get("description", ""),
                "recommendation": analysis.get("recommendation", ""),
            }
            results.append(record)

            if logo_detected and confidence in ("high", "medium"):
                article_detected = True
                log.warning(f"  🚨 로고 감지! 브랜드: {', '.join(detected_brands)} / 위험도: {risk_level}")
                log.warning(f"     모자: {analysis.get('cap_description', 'N/A')}")
                log.warning(f"     권고: {analysis.get('recommendation', 'N/A')}")

                new_detections.append({
                    "article": {"title": title, "source": source, "link": link},
                    "analysis": analysis,
                    "image_url": img_url,
                })

                # 즉시 알림 발송
                send_slack_alert(
                    {"title": title, "source": source, "link": link},
                    analysis, img_url
                )
                send_email_alert(
                    {"title": title, "source": source, "link": link},
                    analysis, img_url
                )
            else:
                log.info(f"  ✅ 안전 — {analysis.get('description', '')[:60]}")

            time.sleep(1)  # API Rate limit 방지

        analyzed_urls.add(url_hash)

    # 4) 결과 저장
    history["analyzed_urls"] = list(analyzed_urls)[-500:]  # 최근 500건만 유지
    history["last_run"] = datetime.now(KST).isoformat()
    save_history(history)
    save_results(results[-200:])  # 최근 200건 결과만 유지

    # 5) 요약 로그
    log.info("\n" + "=" * 60)
    log.info("모니터링 완료 요약")
    log.info(f"  분석 기사: {len(new_articles)}건")
    log.info(f"  로고 감지: {len(new_detections)}건")
    if new_detections:
        log.info("  감지 목록:")
        for det in new_detections:
            brands = ", ".join(det["analysis"].get("detected_brands", []))
            log.info(f"    🚨 [{brands}] {det['article']['title'][:40]}...")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
