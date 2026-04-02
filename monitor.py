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
            else:
                log.info(f"  ✅ 안전 — {analysis.get('description', '')[:60]}")

            time.sleep(1)  # API Rate limit 방지

        analyzed_urls.add(url_hash)

    # 4) 결과 저장
    history["analyzed_urls"] = list(analyzed_urls)[-500:]  # 최근 500건만 유지
    history["last_run"] = datetime.now(KST).isoformat()
    save_history(history)
    save_results(results[-200:])  # 최근 200건 결과만 유지

    # 5) 대시보드 HTML 생성
    generate_dashboard(results, history)

    # 6) 요약 로그
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


# ─── 대시보드 HTML 생성 ──────────────────────────────────
def generate_dashboard(results: list, history: dict):
    """분석 결과를 회장님 보고용 프리미엄 대시보드 HTML로 생성합니다."""
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    today = datetime.now(KST).strftime("%Y.%m.%d")
    
    total = len(results)
    detected = [r for r in results if r.get("logo_detected")]
    safe = [r for r in results if not r.get("logo_detected") and not r.get("error")]
    
    # 기사별로 그룹핑
    articles_map = {}
    for r in results:
        url = r.get("article_url", "")
        if url not in articles_map:
            articles_map[url] = {
                "title": r.get("article_title", ""),
                "url": url,
                "images": [],
                "has_detection": False,
            }
        articles_map[url]["images"].append(r)
        if r.get("logo_detected"):
            articles_map[url]["has_detection"] = True

    # 감지된 이미지 카드 HTML
    detected_cards = ""
    for idx, r in enumerate(detected):
        brands = ", ".join(r.get("detected_brands", []))
        risk = r.get("risk_level", "unknown")
        risk_label = {{"high": "위험", "medium": "주의", "low": "낮음"}}.get(risk, "미정")
        risk_color = {{"high": "#E53935", "medium": "#FB8C00", "low": "#43A047"}}.get(risk, "#78909C")
        risk_bg = {{"high": "rgba(229,57,53,0.08)", "medium": "rgba(251,140,0,0.08)", "low": "rgba(67,160,71,0.08)"}}.get(risk, "rgba(120,144,156,0.08)")
        ts = r.get('timestamp', '')[:10]
        detected_cards += f'''
        <div class="alert-card" style="animation-delay: {{idx * 0.08}}s">
            <div class="alert-image-wrap">
                <img src="{{r.get('image_url', '')}}" alt="" onerror="this.parentElement.innerHTML='<div class=\\'img-fallback\\'>이미지 로드 불가</div>'" />
                <div class="alert-risk" style="background: {{risk_color}}">{{risk_label}}</div>
            </div>
            <div class="alert-content">
                <div class="alert-brands" style="color: {{risk_color}}">{{brands}}</div>
                <a class="alert-title" href="{{r.get('article_url', '#')}}" target="_blank">{{r.get('article_title', '제목 없음')[:55]}}</a>
                <p class="alert-cap">{{r.get('cap_description', '')}}</p>
                <div class="alert-action" style="background: {{risk_bg}}; border-color: {{risk_color}}20">
                    <strong>조치 권고</strong> {{r.get('recommendation', '')}}
                </div>
                <div class="alert-meta">{{ts}}</div>
            </div>
        </div>'''

    # 전체 분석 테이블
    table_rows = ""
    for r in reversed(results[-100:]):
        is_det = r.get("logo_detected")
        status_html = '<span class="badge badge-danger">감지</span>' if is_det else '<span class="badge badge-safe">안전</span>'
        brands = ", ".join(r.get("detected_brands", [])) if r.get("detected_brands") else "—"
        desc = r.get("description", "")[:45]
        table_rows += f'''
        <tr class="{{"row-danger" if is_det else ""}}">
            <td class="td-time">{{r.get('timestamp', '')[:16]}}</td>
            <td>{{status_html}}</td>
            <td class="td-title"><a href="{{r.get('article_url', '#')}}" target="_blank">{{r.get('article_title', '')[:38]}}…</a></td>
            <td class="td-brand">{{brands}}</td>
            <td class="td-desc">{{desc}}</td>
            <td class="td-img"><a href="{{r.get('image_url', '#')}}" target="_blank">보기</a></td>
        </tr>'''

    html = f'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>F&amp;F Brand Safety Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;600;700;900&display=swap" rel="stylesheet">
<style>
@charset "utf-8";
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
:root {{
  --bg: #05060a;
  --surface: #0c0d14;
  --surface2: #12131c;
  --border: rgba(255,255,255,0.05);
  --text: #c8c6c2;
  --text2: #7a7872;
  --accent: #c9a96e;
  --accent2: #b8944f;
  --danger: #d44040;
  --danger-soft: rgba(212,64,64,0.07);
  --safe: #3d9;
}}
html {{ scroll-behavior: smooth; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Noto Sans KR', -apple-system, sans-serif;
  line-height: 1.65;
  -webkit-font-smoothing: antialiased;
}}

/* ── HEADER ── */
.masthead {{
  position: relative;
  padding: 48px 56px 40px;
  border-bottom: 1px solid var(--border);
  overflow: hidden;
}}
.masthead::before {{
  content: '';
  position: absolute; inset: 0;
  background: radial-gradient(ellipse 70% 50% at 0% 0%, rgba(201,169,110,0.06) 0%, transparent 60%),
              radial-gradient(ellipse 50% 60% at 100% 100%, rgba(212,64,64,0.04) 0%, transparent 60%);
  pointer-events: none;
}}
.masthead-inner {{ position: relative; display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 24px; }}
.brand {{ display: flex; align-items: center; gap: 18px; }}
.brand-mark {{
  width: 48px; height: 48px;
  border: 2px solid var(--accent);
  border-radius: 12px;
  display: grid; place-items: center;
  font-size: 22px;
  background: linear-gradient(135deg, rgba(201,169,110,0.12), transparent);
}}
.brand h1 {{
  font-size: 22px; font-weight: 700; letter-spacing: -0.3px;
  color: #f0ede8;
}}
.brand .sub {{
  font-size: 12px; font-weight: 400; color: var(--text2);
  margin-top: 3px; letter-spacing: 0.3px;
}}
.meta-pill {{
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11px; color: var(--text2);
  background: var(--surface2);
  border: 1px solid var(--border);
  padding: 7px 14px; border-radius: 8px;
}}
.meta-pill .dot {{
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--safe);
  animation: blink 2.4s infinite;
}}
@keyframes blink {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: .25; }} }}

/* ── KPI STRIP ── */
.kpi-strip {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1px;
  background: var(--border);
  border-bottom: 1px solid var(--border);
}}
.kpi {{
  background: var(--bg);
  padding: 28px 32px;
  text-align: center;
}}
.kpi.highlight {{ background: var(--danger-soft); }}
.kpi-value {{
  font-size: 38px; font-weight: 900;
  letter-spacing: -1px;
  line-height: 1;
}}
.kpi-value.gold {{ color: var(--accent); }}
.kpi-value.red {{ color: var(--danger); }}
.kpi-value.green {{ color: var(--safe); }}
.kpi-value.muted {{ color: var(--text2); }}
.kpi-label {{
  font-size: 11px; font-weight: 500;
  color: var(--text2);
  margin-top: 8px;
  letter-spacing: 1px;
  text-transform: uppercase;
}}

/* ── SECTION ── */
.section {{ padding: 36px 56px; }}
.section + .section {{ border-top: 1px solid var(--border); }}
.section-head {{
  display: flex; align-items: center; gap: 10px;
  margin-bottom: 24px;
}}
.section-head h2 {{
  font-size: 15px; font-weight: 700; color: #e8e5e0;
  letter-spacing: -0.2px;
}}
.section-head .count {{
  font-size: 11px; font-weight: 600;
  color: var(--danger);
  background: var(--danger-soft);
  padding: 3px 10px; border-radius: 20px;
}}

/* ── ALERT CARDS ── */
.alert-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
  gap: 20px;
}}
.alert-card {{
  display: flex;
  border-radius: 14px;
  overflow: hidden;
  background: var(--surface);
  border: 1px solid rgba(212,64,64,0.12);
  transition: transform 0.25s, box-shadow 0.25s;
  animation: cardIn 0.5s ease both;
}}
@keyframes cardIn {{ from {{ opacity: 0; transform: translateY(12px); }} }}
.alert-card:hover {{
  transform: translateY(-3px);
  box-shadow: 0 12px 40px rgba(0,0,0,0.4);
}}
.alert-image-wrap {{
  position: relative;
  width: 160px; min-height: 200px;
  flex-shrink: 0;
  background: #111;
  overflow: hidden;
}}
.alert-image-wrap img {{
  width: 100%; height: 100%;
  object-fit: cover;
  display: block;
}}
.img-fallback {{
  width: 100%; height: 100%;
  display: grid; place-items: center;
  font-size: 11px; color: #555;
  background: var(--surface2);
}}
.alert-risk {{
  position: absolute; top: 10px; left: 10px;
  padding: 3px 10px; border-radius: 5px;
  font-size: 10px; font-weight: 700;
  color: #fff;
  letter-spacing: 0.5px;
}}
.alert-content {{
  flex: 1;
  padding: 18px 20px;
  display: flex; flex-direction: column; gap: 6px;
}}
.alert-brands {{
  font-size: 11px; font-weight: 700;
  letter-spacing: 0.5px;
  text-transform: uppercase;
}}
.alert-title {{
  font-size: 14px; font-weight: 600;
  color: #e0ddd8;
  text-decoration: none;
  line-height: 1.5;
}}
.alert-title:hover {{ color: #fff; }}
.alert-cap {{
  font-size: 12px; color: #b0746e;
  line-height: 1.55;
}}
.alert-action {{
  font-size: 11px; line-height: 1.6;
  color: var(--text);
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid;
  margin-top: auto;
}}
.alert-action strong {{
  display: block;
  font-size: 10px; font-weight: 700;
  color: var(--text2);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-bottom: 2px;
}}
.alert-meta {{ font-size: 10px; color: var(--text2); margin-top: 4px; }}

/* ── TABLE ── */
.table-wrap {{
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--border);
}}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th {{
  padding: 11px 16px;
  text-align: left;
  font-size: 10px; font-weight: 600;
  color: var(--text2);
  background: var(--surface2);
  letter-spacing: 0.8px;
  text-transform: uppercase;
  border-bottom: 1px solid var(--border);
}}
td {{
  padding: 9px 16px;
  border-bottom: 1px solid var(--border);
  color: var(--text);
}}
.row-danger td {{ background: var(--danger-soft); }}
.badge {{
  display: inline-block;
  padding: 2px 8px; border-radius: 4px;
  font-size: 10px; font-weight: 700;
  letter-spacing: 0.3px;
}}
.badge-danger {{ background: var(--danger); color: #fff; }}
.badge-safe {{ background: rgba(51,221,153,0.1); color: var(--safe); }}
.td-time {{ color: var(--text2); white-space: nowrap; }}
.td-title a {{ color: #a0b4d0; text-decoration: none; }}
.td-title a:hover {{ text-decoration: underline; }}
.td-brand {{ color: var(--text2); }}
.td-desc {{ color: var(--text2); font-size: 11px; }}
.td-img a {{ color: var(--accent2); text-decoration: none; font-weight: 500; }}

/* ── EMPTY ── */
.empty {{ text-align: center; padding: 64px; color: #333; }}
.empty .ico {{ font-size: 44px; margin-bottom: 12px; opacity: 0.4; }}

/* ── FOOTER ── */
.foot {{
  padding: 24px 56px;
  border-top: 1px solid var(--border);
  display: flex; justify-content: space-between;
  font-size: 10px; color: #2a2a2a;
  letter-spacing: 0.3px;
}}

@media (max-width: 900px) {{
  .masthead, .section, .foot {{ padding-left: 24px; padding-right: 24px; }}
  .kpi-strip {{ grid-template-columns: repeat(2, 1fr); }}
  .alert-grid {{ grid-template-columns: 1fr; }}
  .alert-image-wrap {{ width: 120px; min-height: 160px; }}
  .kpi-value {{ font-size: 28px; }}
}}
</style>
</head>
<body>

<header class="masthead">
  <div class="masthead-inner">
    <div class="brand">
      <div class="brand-mark">F</div>
      <div>
        <h1>Brand Safety Monitor</h1>
        <div class="sub">F&amp;F Communications · 뉴스 이미지 브랜드 로고 자동 감지 시스템</div>
      </div>
    </div>
    <div style="display:flex; gap:10px; flex-wrap:wrap;">
      <div class="meta-pill"><span class="dot"></span> 실시간 모니터링 · 5분 주기</div>
      <div class="meta-pill">업데이트 {now}</div>
    </div>
  </div>
</header>

<div class="kpi-strip">
  <div class="kpi">
    <div class="kpi-value gold">{len(articles_map)}</div>
    <div class="kpi-label">분석 기사</div>
  </div>
  <div class="kpi">
    <div class="kpi-value muted">{total}</div>
    <div class="kpi-label">분석 이미지</div>
  </div>
  <div class="kpi highlight">
    <div class="kpi-value red">{len(detected)}</div>
    <div class="kpi-label">로고 감지</div>
  </div>
  <div class="kpi">
    <div class="kpi-value green">{len(safe)}</div>
    <div class="kpi-label">안전 확인</div>
  </div>
</div>

<div class="section">
  <div class="section-head">
    <h2>로고 감지 이미지</h2>
    <span class="count">{len(detected)}건</span>
  </div>
  {"<div class='alert-grid'>" + detected_cards + "</div>" if detected_cards else "<div class='empty'><div class='ico'>✓</div><p>감지된 브랜드 로고가 없습니다</p></div>"}
</div>

<div class="section">
  <div class="section-head">
    <h2>전체 분석 내역</h2>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>시각</th><th>상태</th><th>기사</th><th>브랜드</th><th>설명</th><th>이미지</th>
      </tr></thead>
      <tbody>
        {table_rows if table_rows else "<tr><td colspan='6' style='text-align:center;padding:48px;color:#333'>분석 결과 없음</td></tr>"}
      </tbody>
    </table>
  </div>
</div>

<footer class="foot">
  <span>F&amp;F Communications Team — Brand Safety Monitoring System</span>
  <span>Powered by Claude Vision API · {today}</span>
</footer>

</body>
</html>'''

    Path("docs").mkdir(exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"📊 대시보드 생성 완료: docs/index.html (감지 {len(detected)}건 / 전체 {total}건)")


if __name__ == "__main__":
    main()
