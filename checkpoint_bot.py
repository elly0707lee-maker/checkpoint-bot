"""
Morning Broadcast CheckPoint Bot 🌅
방송 전 뉴스 → 섹터/종목 자동 분류 텔레그램 봇
+ 지표 텍스트 태그 지원
+ 이미지 캡쳐 → Claude Vision 자동 인식

핵심 변경 (사용자 편집 보존):
- 매 메시지마다 대시보드에서 fresh fetch
- 봇 메모리는 캐시에 불과 — 대시보드가 진실의 근원
- fetch_fresh_state() 헬퍼로 5군데 통일
"""

import logging
import os
import re
import asyncio
import aiohttp
import base64
from bs4 import BeautifulSoup
from telegram import Update, BotCommand
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
from datetime import datetime

# ── 설정 ──────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")
DASHBOARD_API_SECRET = os.environ.get("API_SECRET", "anchoryen")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── 대시보드 전송/복원 ─────────────────────────────────
_last_dashboard_error = ""

def convert_links_to_html(text: str) -> str:
    import re as _re
    links = {}
    def replacer(m):
        key = f"__LINKPH{len(links)}__"
        safe_url = m.group(1).replace("&", "&amp;")
        links[key] = f'<a href="{safe_url}">🔗</a>'
        return " " + key
    text = _re.sub(r"\[\[LINK:([^\]]+)\]\]", replacer, text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for key, val in links.items():
        text = text.replace(key, val)
    return text

async def restore_checkpoint_from_dashboard() -> str:
    """대시보드에서 현재 체크포인트 본문 가져오기."""
    if not DASHBOARD_URL:
        return ""
    fetch_url = DASHBOARD_URL.rstrip("/") + "/api/post/checkpoint"
    headers = {"X-API-Secret": DASHBOARD_API_SECRET}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(fetch_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                return data.get("content", "")
    except Exception as e:
        logger.error(f"체크포인트 복원 오류: {e}")
        return ""


async def restore_sector_links_from_dashboard() -> dict:
    """대시보드 본문에서 섹터 링크 재구성 (봇 재시작 대비)."""
    if not DASHBOARD_URL:
        return {}
    fetch_url = DASHBOARD_URL.rstrip("/") + "/api/post/checkpoint"
    headers = {"X-API-Secret": DASHBOARD_API_SECRET}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(fetch_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                checkpoint_text = data.get("content", "")
                if not checkpoint_text:
                    return {}
                sls = {}
                for m in re.finditer(r"✔️(\S+)[^\n]*\[\[LINK:([^\]]+)\]\]", checkpoint_text):
                    sector = m.group(1)
                    url_val = m.group(2)
                    if sector not in sls:
                        sls[sector] = []
                    if url_val not in sls[sector]:
                        sls[sector].append(url_val)
                return sls
    except Exception as e:
        logger.error(f"대시보드 복원 오류: {e}")
        return {}


# ───────────────────────────────────────────────────────
# 🆕 매 메시지마다 대시보드에서 최신 상태 fetch
# 이게 핵심 — 사용자의 ✏️ 편집을 항상 반영
# ───────────────────────────────────────────────────────
async def fetch_fresh_state(user_id: int):
    """매 호출 — dashboard에서 최신 체크포인트 본문을 가져와 user_state 갱신.
    
    사용자가 대시보드에서 ✏️ 편집한 내용을 봇이 항상 반영하도록 보장.
    last_checkpoint는 매번 새로, sector_link_store는 처음에만.
    """
    fresh_cp = await restore_checkpoint_from_dashboard()
    user_state[user_id]["last_checkpoint"] = fresh_cp or ""
    if "sector_link_store" not in user_state[user_id]:
        user_state[user_id]["sector_link_store"] = await restore_sector_links_from_dashboard()


async def send_to_dashboard(content: str, date_str: str) -> bool:
    global _last_dashboard_error
    logger.info(f"📤 send_to_dashboard 호출: URL={DASHBOARD_URL!r}, len={len(content)}")
    if not DASHBOARD_URL:
        _last_dashboard_error = "DASHBOARD_URL 미설정"
        logger.error("📤 ❌ DASHBOARD_URL이 빈 문자열! 환경변수 누락 또는 봇 재시작 필요")
        return False
    url = DASHBOARD_URL.rstrip("/") + "/api/post/checkpoint"
    payload = {"content": content, "date": date_str}
    headers = {"Content-Type": "application/json", "X-API-Secret": DASHBOARD_API_SECRET}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                body = await resp.text()
                if resp.status == 200:
                    _last_dashboard_error = ""
                    logger.info(f"📤 ✅ 대시보드 전송 성공 ({len(content)}자)")
                    return True
                _last_dashboard_error = f"HTTP {resp.status}: {body[:150]}"
                logger.error(f"📤 ❌ 대시보드 전송 실패: HTTP {resp.status}: {body[:200]}")
                return False
    except Exception as e:
        _last_dashboard_error = f"{type(e).__name__}: {str(e)}"[:200]
        logger.error(f"📤 ❌ 대시보드 전송 예외: {type(e).__name__}: {e}")
        return False

# ── URL 크롤링 ──────────────────────────────────────────
async def fetch_url_text(url: str) -> str | None:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text(errors="ignore")
                soup = BeautifulSoup(html, "html.parser")
                for selector in ["article", ".article-body", ".article_body", "#articleBody",
                                  ".news-content", ".content-article", "main"]:
                    el = soup.select_one(selector)
                    if el:
                        text = el.get_text(separator="\n", strip=True)
                        if len(text) > 100:
                            return text[:2000]
                og_desc = soup.find("meta", property="og:description")
                og_title = soup.find("meta", property="og:title")
                parts = []
                if og_title:
                    parts.append(og_title.get("content", ""))
                if og_desc:
                    parts.append(og_desc.get("content", ""))
                if parts:
                    return "\n".join(parts)
                return None
    except Exception as e:
        logger.info(f"URL 크롤링 실패 ({url}): {e}")
        return None

def extract_urls(text: str) -> list:
    return re.findall(r'https?://[^\s]+', text)

async def enrich_text_with_url(text: str) -> tuple[str, list[str]]:
    urls = extract_urls(text)
    if not urls:
        return text, []
    enriched = text
    found_urls = []
    for url in urls:
        fetched = await fetch_url_text(url)
        if fetched:
            enriched = enriched.replace(url, fetched)
        else:
            enriched = enriched.replace(url, "")
        found_urls.append(url)
    markers = "".join(f"\n[[LINK:{u}]]" for u in found_urls)
    return enriched.strip() + markers, found_urls

# ── Vision 추출 함수들 ────────────────────────────────────
async def extract_indicators_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
    try:
        image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_data}},
                    {"type": "text", "text": (
                        "이 이미지에서 시장 지표 수치만 추출해줘.\n"
                        "형식: 항목명 현재값 (등락%)\n"
                        "등락률은 반드시 괄호 안에 넣을 것.\n"
                        "예시:\nSOX 7,773.13 (+1.34%)\nVIX 23.95 (-1.45%)\nEWY 133.81 (+6.38%)\n"
                        "WTI 90.70 (+2.92%)\nDXY 99.17 (+0.23%)\nUS10Y 4.362% (+0.026)\n\n"
                        "수치가 없는 항목은 제외. 설명 없이 수치만 나열."
                    )}
                ],
            }]
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"이미지 분석 오류: {e}")
        return None


async def extract_sector_content_from_image(image_bytes: bytes, tag_type: str, tag_value: str, mime_type: str = "image/jpeg") -> str | None:
    try:
        image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
        if tag_type == "NXT":
            prompt = (
                "이 이미지는 NXT 괴리율 표야.\n"
                "표에서 종목명, KRX 종가, NXT 종가, 괴리율(%), 이유를 모두 추출해줘.\n"
                "형식: 종목명 KRX가 NXT가 괴리율% [이유]\n"
                "예시:\n넥스틸 12,290 14,420 +17.33% [걸프 송유관 수출 기대]\n한올바이오파마 54,400 44,050 -19.03%\n\n"
                "이유 없으면 괄호 생략. 설명 없이 목록만 나열."
            )
        else:
            if tag_type == "SECTOR":
                section = f"{tag_value} 섹터"
            elif tag_type == "KOSPI":
                section = f"코스피 종목 {tag_value}"
            elif tag_type == "KOSDAQ":
                section = f"코스닥 종목 {tag_value}"
            else:
                section = "시장"
            prompt = (
                f"이 이미지는 {section} 관련 자료야.\n\n"
                "이미지 타입에 따라 아래 중 하나로 처리해줘:\n\n"
                "▶ 신문 기사 / 텍스트 스크린샷이면:\n"
                "  - 핵심 내용을 bullet 2~3개로 요약\n"
                "  - 형식: - 핵심 내용\n"
                "  - 관련 종목이 언급되면 마지막에 '관련 종목: 종목A, 종목B' 추가\n\n"
                "▶ 주가 테이블 / 차트이면:\n"
                "  - 종목명(티커)과 현재가, 등락률 추출\n"
                "  - 형식: 종목명 현재가 (등락률)\n\n"
                "설명 없이 내용만 출력할 것."
            )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_data}},
                    {"type": "text", "text": prompt}
                ],
            }]
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"섹터 이미지 분석 오류: {e}")
        return None


# ── 사용자별 상태 ────────────────────────────────────
user_state = {}

# ── Claude 프롬프트 ────────────────────────────────────────
SYSTEM_PROMPT = """너는 한국 경제방송 앵커의 방송 전 브리핑을 도와주는 전문 어시스턴트야.

입력 데이터는 아래 형식으로 구조화되어 들어와:

[태그유형: 값]
내용

태그 유형:
- SECTOR: 해당 내용의 섹터명 (사용자가 직접 지정한 것. 반드시 이 이름 그대로 사용)
- KOSPI: 코스피 종목명 (사용자가 직접 지정)
- KOSDAQ: 코스닥 종목명 (사용자가 직접 지정)
- US_MARKET: 미증시 마감 내용 (사용자가 직접 타이핑한 것만)
- INDICATOR: 시장 지표 (야간선물, VIX, SOX 등 수치 그대로 유지)
- AUTO: 태그 없이 들어온 일반 기사 (섹터를 네가 판단해서 분류)

규칙:
1. SECTOR 태그가 있으면 → 반드시 그 섹터명 그대로 📌Sector 아래 ✔️섹터명 으로 표시
2. KOSPI 태그가 있으면 → 반드시 📌코스피 칸에만 표시. 섹터 칸에 절대 넣지 말 것.
3. KOSDAQ 태그가 있으면 → 반드시 📌코스닥 칸에만 표시. 섹터 칸에 절대 넣지 말 것.
4. US_MARKET 태그가 있으면 → 🇺🇸美증시 마감 칸에 표시
5. US_MARKET 태그가 하나도 없으면 → 🇺🇸美증시 마감 섹션 절대 생성하지 말 것.
6. INDICATOR 태그가 있으면 → 📊지표 섹션으로 체크포인트 맨 위(날짜 헤더 바로 아래)에 배치. 수치 절대 수정하지 말 것.
7. INDICATOR 태그가 없으면 → 📊지표 섹션 생성하지 말 것.
6. [[LINK:url]] 마커가 있으면 반드시 원문 그대로 해당 내용 끝에 보존. 절대 수정·삭제 금지.
7. 📡시장 시그널 섹션은 절대 생성하지 말 것. 코드에서 별도 처리함.
7. AUTO 태그 내용은 네가 섹터 판단해서 분류
9. ** 볼드 표시 절대 금지
10. 섹터 중분류는 ✔️ 사용
11. 기사에 언급된 종목은 해당 섹터 안 "관련 종목:" 줄에만. 코스피/코스닥 칸에 중복 금지.
12. 최종 출력은 하나의 체크포인트로 통합
13. KOSPI/KOSDAQ 태그로 들어온 내용은 절대로 섹터로 승격하거나 섹터를 추가로 만들지 말 것.
14. 각 섹터(✔️) 아래 불릿은 반드시 정확히 3줄:
    - 1줄: 헤드라인 스타일 핵심 요약 (기사 제목처럼)
    - 2줄: 부연설명 1
    - 3줄: 부연설명 2
15. 모든 불릿은 음슴체로 끝맺음 ('~함', '~됨', '~임', '~옴', '~짐' 등)
16. 각 불릿 25~40자 내외로 간결하게. 수식어·군더더기 X, 팩트만.

출력 형식:
{날짜} Check Point✨

📊지표
[INDICATOR 내용. 없으면 이 섹션 통째로 생략]

🇺🇸美증시 마감
[US_MARKET 태그 내용만. 없으면 이 섹션 통째로 생략]

📌Sector
✔️[섹터명]
- 핵심 내용
- 핵심 내용
- 관련 종목: 종목A, 종목B

📌코스피
[종목명]
- 관련 내용

📌코스닥
[종목명]
- 관련 내용"""

EDIT_PROMPT = """너는 체크포인트 문서를 수정하는 어시스턴트야.

규칙:
1. ** 볼드 표시 절대 금지
2. 섹터 중분류는 ✔️ 사용
3. 원본 형식과 구조를 그대로 유지하면서 해당 항목만 수정
4. 수정 지시가 없는 부분은 절대 건드리지 말 것
5. 전체 체크포인트를 그대로 출력 (수정된 부분 포함)"""

AFTER_MARKET_PROMPT = """너는 시간외 특이종목 데이터를 체크포인트용으로 요약하는 어시스턴트야.

규칙:
1. ** 볼드 표시 절대 금지
2. 같은 이슈/테마로 움직이는 종목은 하나의 ✔️ 항목으로 묶을 것
3. 개별 이슈 종목은 따로 표시
4. 상승/하락 구분해서 정리
5. 각 ✔️ 항목의 불릿은 최대 2개
6. 핵심 종목만 선별 (전체 나열 금지)
7. 등락률 반드시 표시

출력 형식:
📌시간외 특이종목

▶ 상승
✔️ [테마/이슈명]
- 핵심 내용 (등락률 포함)
- 관련 종목: 종목A(+X%), 종목B(+X%)

▶ 하락
✔️ [테마/이슈명]
- 핵심 내용"""

NXT_PROMPT = """너는 NXT 괴리율 데이터를 체크포인트용으로 요약하는 어시스턴트야.

규칙:
1. ** 볼드 표시 절대 금지
2. 같은 이슈/테마로 움직이는 종목은 묶을 것
3. 상위(괴리율 양수)/하위(괴리율 음수) 구분
4. 괴리율 수치 반드시 표시
5. 이유가 있는 종목 우선 표시
6. 핵심만 선별 (전체 나열 금지)

출력 형식:
📌NXT 괴리율

▶ 상위
✔️ [테마/이슈명]
- 핵심 내용
- 관련 종목: 종목A(+X%), 종목B(+X%)

▶ 하위
✔️ [테마/이슈명]
- 핵심 내용
- 관련 종목: 종목A(-X%), 종목B(-X%)"""


async def summarize_after_market(content: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=AFTER_MARKET_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()


async def summarize_nxt(content: str) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=NXT_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()

def parse_multi_tag(text: str) -> list:
    TAG_START = re.compile(
        r"^(섹터|코스피|코스닥|지표|시간외|NXT|시그널)\s*/",
        re.IGNORECASE | re.MULTILINE
    )
    matches = list(TAG_START.finditer(text))
    if not matches:
        return [parse_user_tag(text)]
    blocks = []
    if matches[0].start() > 0:
        prefix = text[:matches[0].start()].strip()
        if prefix:
            blocks.append(("AUTO", "", prefix))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        block_text = text[m.start():end].strip()
        blocks.append(parse_user_tag(block_text))
    return blocks


def parse_user_tag(text: str):
    if re.match(r"^지표\s*/\s*", text, re.IGNORECASE):
        content = re.sub(r"^지표\s*/\s*", "", text, flags=re.IGNORECASE).strip()
        return "INDICATOR", "", content
    sector_match = re.match(r"^섹터\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if sector_match:
        return "SECTOR", sector_match.group(1).strip(), text[sector_match.end():].strip()
    kospi_match = re.match(r"^코스피\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if kospi_match:
        return "KOSPI", kospi_match.group(1).strip(), text[kospi_match.end():].strip()
    kosdaq_match = re.match(r"^코스닥\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if kosdaq_match:
        return "KOSDAQ", kosdaq_match.group(1).strip(), text[kosdaq_match.end():].strip()
    if re.match(r"^시간외\s*/\s*", text, re.IGNORECASE):
        content = re.sub(r"^시간외\s*/\s*", "", text, flags=re.IGNORECASE).strip()
        return "AFTER_MARKET", "", content
    signal_match = re.match(r"^시그널\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if signal_match:
        return "SIGNAL", signal_match.group(1).strip(), text[signal_match.end():].strip()
    if re.match(r"^시그널\s*/\s*", text, re.IGNORECASE):
        content = re.sub(r"^시그널\s*/\s*", "", text, flags=re.IGNORECASE).strip()
        return "SIGNAL", "", content
    if re.match(r"^NXT\s*/\s*", text, re.IGNORECASE):
        content = re.sub(r"^NXT\s*/\s*", "", text, flags=re.IGNORECASE).strip()
        return "NXT", "", content
    # 🆕 미증시/유가, 미증시/금 등 sub-value 태그 인식
    us_tag_match = re.match(r"^(?:미증시|美증시)\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if us_tag_match:
        return "US_MARKET", us_tag_match.group(1).strip(), text[us_tag_match.end():].strip()
    if re.match(r"^(?:미증시|美증시)\s*/\s*", text, re.IGNORECASE):
        content = re.sub(r"^(?:미증시|美증시)\s*/\s*", "", text, flags=re.IGNORECASE).strip()
        return "US_MARKET", "", content
    us_keywords = ["다우", "나스닥", "s&p", "S&P", "미증시", "美증시", "뉴욕증시", "월스트리트",
                   "미 증시", "미증시", "미국증시", "미국 증시"]
    if any(kw in text for kw in us_keywords):
        return "US_MARKET", "", text
    return "AUTO", "", text

def format_buffer_for_claude(buffer: list) -> str:
    parts = []
    us_market_lines = []
    indicator_lines = []
    for item in buffer:
        tag_type, tag_value, content = item
        if tag_type == "US_MARKET":
            us_market_lines.append(content.strip())
        elif tag_type == "INDICATOR":
            indicator_lines.append(content.strip())
        elif tag_type == "SECTOR":
            parts.append(f"[SECTOR: {tag_value}]\n{content}")
        elif tag_type == "AUTO":
            parts.append(f"[AUTO]\n{content}")
    if indicator_lines:
        combined_indicator = "\n".join(indicator_lines)
        parts.insert(0, f"[INDICATOR]\n{combined_indicator}")
    if us_market_lines:
        combined_us = "\n".join(us_market_lines)
        parts.insert(1 if indicator_lines else 0, f"[US_MARKET]\n{combined_us}")
    return "\n\n---\n\n".join(parts)


# ────────────────────────────────────────────────────────
# 즉시 대시보드 병합 (조각 즉시 반영)
# ────────────────────────────────────────────────────────

def _parse_stock_map(section_text: str) -> dict:
    result = {}
    current_name = None
    current_lines = []
    for line in section_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("-"):
            if current_name is not None:
                link_m = re.search(r"\[\[LINK:([^\]]+)\]\]", line)
                clean = re.sub(r"\[\[LINK:[^\]]+\]\]", "", line).strip()
                url = link_m.group(1) if link_m else None
                current_lines.append((clean, url))
        else:
            if current_name is not None:
                result[current_name] = current_lines
            current_name = line
            current_lines = []
    if current_name is not None:
        result[current_name] = current_lines
    return result

def _summarize_bullets(content: str) -> list:
    content = content.replace("[기사내용]", "").strip()
    seen = set()
    lines = []
    skip_keywords = ["기자 구독", "구독하기", "Forwarded from", "today at",
                     "naver.com", "hankyung.com", "zdnet", "2026.0", "2025.0",
                     "글자크기", "기사 스크랩", "스크랩", "인쇄", "공유", "댓글",
                     "로그인", "회원가입", "font", "Font"]
    for l in content.split("\n"):
        l = l.strip()
        if not l or l.startswith("http"):
            continue
        if any(sk in l for sk in skip_keywords):
            continue
        korean = sum(1 for c in l if "\uAC00" <= c <= "\uD7A3")
        if korean < 2 and len(l) < 20:
            continue
        key = l.replace("-", "").strip()
        if key in seen:
            continue
        seen.add(key)
        lines.append(l)
    bullets = [l for l in lines if len(l) > 5][:2]
    return [f"- {b}" if not b.startswith("-") else b for b in bullets]

def _build_stock_block(header: str, stock_map: dict) -> str:
    if not stock_map:
        return ""
    items = []
    for name, bullets in stock_map.items():
        item_lines = [str(name)]
        for entry in bullets:
            if isinstance(entry, tuple) and len(entry) == 2:
                text, url = str(entry[0]), entry[1]
                line = text if text.startswith("-") else "- " + text
                if url:
                    line += f" [[LINK:{url}]]"
            elif isinstance(entry, str):
                line = entry if entry.startswith("-") else "- " + entry
            else:
                continue
            item_lines.append(line)
        items.append("\n".join(item_lines))
    return header + "\n" + "\n\n".join(items)

async def _claude_summarize(text: str) -> str:
    """기사·링크 텍스트를 3줄 음슴체로 요약.
    
    형식:
    - [헤드라인 스타일 핵심 요약]
    - [부연설명 1]
    - [부연설명 2]
    """
    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
            messages=[{"role": "user", "content":
                f"""아래 기사를 정확히 3줄로 요약해줘.

⭐ 언어: 원문이 영어·중국어·일본어 등 어떤 언어든 **한국어로 번역해서 요약**.

형식 (반드시 이대로):
- [첫 줄: 기사 헤드라인 스타일 핵심 요약]
- [둘째 줄: 부연설명 1]
- [셋째 줄: 부연설명 2]

규칙:
- 각 줄 반드시 '- '로 시작
- 모두 음슴체로 끝맺음 ('~함', '~됨', '~임', '~옴', '~짐' 등)
- 각 줄 25~40자 내외로 간결하게
- 수식어·군더더기 X, 사실만
- 첫 줄은 기사 제목처럼 강한 한 줄
- 부연은 첫 줄이 담지 못한 팩트·수치·전망·이유
- 설명 없이 bullet 3개만 출력
- 원문에 포함된 이모지(🌙, ⏰, 🔥 등)는 종목명 뒤에 있으면 그대로 유지

예시 (영어 원문 → 한국어 요약):
원문: "SEMI forecasts 5-year growth streak, targeting $229.5B by 2028..."
요약:
- 반도체 장비 매출 5년 연속 성장 전망
- SEMI, 2028년 2,295억 달러 달성 예상
- AI 인프라 투자로 산업 성장 전망 강화됨

기사:
{text[:3000]}"""}]
        )
        return resp.content[0].text.strip()
    except Exception:
        return text

async def instant_merge(prev_cp: str, tag_type: str, tag_value: str, content: str,
                        sector_link_store: dict, date_str: str) -> str:
    if not prev_cp:
        prev_cp = f"{date_str} Check Point✨"
    link_urls = re.findall(r"\[\[LINK:([^\]]+)\]\]", content)
    clean_c = re.sub(r"\s*\[\[LINK:[^\]]+\]\]", "", content).strip()

    if tag_type in ("KOSPI", "KOSDAQ"):
        header = "📌코스피" if tag_type == "KOSPI" else "📌코스닥"
        m = re.search(rf"{re.escape(header)}\n(.*?)(?=\n📌|\n📡|\Z)", prev_cp, re.DOTALL)
        stock_map = _parse_stock_map(m.group(1)) if m else {}
        # 🆕 사용자가 이미 정리한 형식이면 그대로 사용 (요약 X)
        if clean_c.strip():
            lines_check = [l.strip() for l in clean_c.split("\n") if l.strip()]
            is_preformatted = (
                len(lines_check) >= 2
                and sum(1 for l in lines_check if l.startswith("-")) >= 2
                and len(clean_c) < 500
            )
            if is_preformatted:
                summary = clean_c   # 그대로 사용
            else:
                summary = await _claude_summarize(clean_c)
        else:
            summary = ""
        bullets_lines = [l.strip() for l in summary.split("\n") if l.strip().startswith("-")]
        link_url = link_urls[0] if link_urls else None
        if tag_value not in stock_map:
            stock_map[tag_value] = []
        if bullets_lines:
            # 첫 줄(헤드라인)에 링크 붙임, 나머지는 링크 없이
            for i, line in enumerate(bullets_lines):
                url_for_line = link_url if i == 0 else None
                stock_map[tag_value].append((line, url_for_line))
        elif link_url:
            stock_map[tag_value].append(("- 관련 기사", link_url))
        new_block = _build_stock_block(header, stock_map)
        if m:
            prev_cp = prev_cp[:m.start()] + new_block + prev_cp[m.end():]
        else:
            prev_cp = prev_cp.rstrip() + "\n" + new_block
        return prev_cp

    elif tag_type == "SECTOR":
        if tag_value and link_urls:
            if tag_value not in sector_link_store:
                sector_link_store[tag_value] = []
            for url in link_urls:
                if url not in sector_link_store[tag_value]:
                    sector_link_store[tag_value].append(url)
        # 🆕 사용자가 이미 정리한 형식이면 그대로 사용 (요약 X)
        # 판정: bullet 2개 이상 + 500자 미만 = pre-formatted
        if clean_c.strip():
            lines_check = [l.strip() for l in clean_c.split("\n") if l.strip()]
            is_preformatted = (
                len(lines_check) >= 2
                and sum(1 for l in lines_check if l.startswith("-")) >= 2
                and len(clean_c) < 500
            )
            if not is_preformatted:
                clean_c = await _claude_summarize(clean_c)
        title_line = f"✔️{tag_value}"
        for url in link_urls:
            title_line += f" [[LINK:{url}]]"
        new_entry = title_line + ("\n" + clean_c if clean_c else "")
        sec_m = re.search(r"(📌Sector\n?)(.*?)(?=\n📌코스피|\n📌코스닥|\n📌시간외|\n📌NXT|\Z)",
                          prev_cp, re.DOTALL)
        if sec_m:
            body = sec_m.group(2).rstrip()
            if f"✔️{tag_value}" in body:
                # 🆕 같은 섹터 chunk — 옛 줄 보존하고 새 bullets만 그 섹션 끝에 추가
                # (사용자가 칠한 색깔/하이라이트 유지)
                sec_start = body.find(f"✔️{tag_value}")
                # 다음 ✔️ 직전 위치 찾기
                rest = body[sec_start + 1:]
                next_sec_m = re.search(r"\n✔️", rest)
                insert_pos = sec_start + 1 + next_sec_m.start() if next_sec_m else len(body)
                # 새 bullets만 추출 (✔️ 헤더는 빼고 본문만)
                new_bullets_lines = [l for l in clean_c.split("\n") if l.strip()]
                # 중복 방지 — 기존 섹션 안에 이미 같은 bullet 있으면 skip
                existing_section = body[sec_start:insert_pos]
                new_bullets_to_add = []
                for line in new_bullets_lines:
                    stripped = line.strip().lstrip("-").strip()
                    if stripped and stripped not in existing_section:
                        new_bullets_to_add.append(line if line.startswith("-") else "- " + line)
                if new_bullets_to_add:
                    addition = "\n" + "\n".join(new_bullets_to_add)
                    body = body[:insert_pos] + addition + body[insert_pos:]
            else:
                body = body + ("\n\n" if body else "") + new_entry
            prev_cp = prev_cp[:sec_m.start()] + "📌Sector\n" + body + prev_cp[sec_m.end():]
        else:
            ins = re.search(r"\n📌코스피|\n📌코스닥|\Z", prev_cp)
            pos = ins.start() if ins and ins.group() != "" else len(prev_cp)
            prev_cp = prev_cp[:pos] + "\n📌Sector\n" + new_entry + prev_cp[pos:]
        for sname, urls in sector_link_store.items():
            for url in urls:
                marker = f"[[LINK:{url}]]"
                if marker not in prev_cp:
                    lines = prev_cp.split("\n")
                    for i, line in enumerate(lines):
                        if line.startswith(f"✔️{sname}") and marker not in line:
                            lines[i] = line + f" {marker}"
                            break
                    prev_cp = "\n".join(lines)
        return prev_cp

    elif tag_type == "SIGNAL":
        # 🆕 사용자가 이미 정리한 형식이면 그대로 사용 (요약 X)
        if clean_c.strip():
            lines_check = [l.strip() for l in clean_c.split("\n") if l.strip()]
            is_preformatted = (
                len(lines_check) >= 2
                and sum(1 for l in lines_check if l.startswith("-")) >= 2
                and len(clean_c) < 500
            )
            if not is_preformatted:
                clean_c = await _claude_summarize(clean_c)
        title_line = (f"☑️ {tag_value}" if tag_value else "")
        for url in link_urls:
            title_line += f" [[LINK:{url}]]"
        new_entry = (title_line + "\n" + clean_c) if title_line else clean_c
        sig_m = re.search(r"📡시장 시그널\n?(.*?)(?=\n📌Sector|\n📌코스피|\n📌코스닥|\Z)",
                          prev_cp, re.DOTALL)
        if sig_m:
            body = sig_m.group(1).rstrip()
            new_body = body + ("\n\n" if body else "") + new_entry
            prev_cp = prev_cp[:sig_m.start()] + "📡시장 시그널\n" + new_body + prev_cp[sig_m.end():]
        else:
            ins = re.search(r"\n📌Sector|\n📌코스피|\Z", prev_cp)
            pos = ins.start() if ins and ins.group() != "" else len(prev_cp)
            prev_cp = prev_cp[:pos] + "\n📡시장 시그널\n" + new_entry + prev_cp[pos:]
        return prev_cp

    elif tag_type == "INDICATOR":
        ind_m = re.search(r"📊지표\n?(.*?)(?=\n🇺🇸|\n📡|\n📌|\Z)", prev_cp, re.DOTALL)
        new_block = "📊지표\n" + clean_c
        if ind_m:
            prev_cp = prev_cp[:ind_m.start()] + new_block + prev_cp[ind_m.end():]
        else:
            prev_cp = new_block + "\n\n" + prev_cp
        return prev_cp

    elif tag_type == "US_MARKET":
        # 🆕 sub-value 있으면 (예: 미증시/유가) ☑️ 형식으로
        if tag_value:
            # pre-formatted 판정
            lines_check = [l.strip() for l in clean_c.split("\n") if l.strip()]
            is_preformatted = (
                len(lines_check) >= 2
                and sum(1 for l in lines_check if l.startswith("-")) >= 2
                and len(clean_c) < 500
            )
            if not is_preformatted and clean_c.strip():
                clean_c = await _claude_summarize(clean_c)
            title_line = f"☑️ {tag_value}"
            for url in link_urls:
                title_line += f" [[LINK:{url}]]"
            new_entry = title_line + ("\n" + clean_c if clean_c else "")
            usm_m = re.search(r"🇺🇸美증시 마감\n?(.*?)(?=\n📡|\n📌|\Z)", prev_cp, re.DOTALL)
            if usm_m:
                body = usm_m.group(1).rstrip()
                # 같은 sub-value 있으면 그 섹션에 append (기존 유지 + 새 bullets)
                pat_marker = f"☑️ {tag_value}"
                if pat_marker in body:
                    sec_start = body.find(pat_marker)
                    rest = body[sec_start + 1:]
                    next_m = re.search(r"\n☑️", rest)
                    insert_pos = sec_start + 1 + next_m.start() if next_m else len(body)
                    existing_section = body[sec_start:insert_pos]
                    new_lines = []
                    for line in clean_c.split("\n"):
                        stripped = line.strip().lstrip("-").strip()
                        if stripped and stripped not in existing_section:
                            new_lines.append(line if line.startswith("-") else "- " + line)
                    if new_lines:
                        body = body[:insert_pos] + "\n" + "\n".join(new_lines) + body[insert_pos:]
                else:
                    body = body + ("\n\n" if body else "") + new_entry
                prev_cp = prev_cp[:usm_m.start()] + "🇺🇸美증시 마감\n" + body + prev_cp[usm_m.end():]
            else:
                ins = re.search(r"\n📡|\n📌|\Z", prev_cp)
                pos = ins.start() if ins and ins.group() != "" else len(prev_cp)
                prev_cp = prev_cp[:pos] + "\n🇺🇸美증시 마감\n" + new_entry + prev_cp[pos:]
        else:
            # 옛 방식: sub-value 없으면 전체 교체
            usm_m = re.search(r"🇺🇸美증시 마감\n?(.*?)(?=\n📡|\n📌|\Z)", prev_cp, re.DOTALL)
            new_block = "🇺🇸美증시 마감\n" + clean_c
            if usm_m:
                prev_cp = prev_cp[:usm_m.start()] + new_block + prev_cp[usm_m.end():]
            else:
                ins = re.search(r"\n📡|\n📌|\Z", prev_cp)
                pos = ins.start() if ins and ins.group() != "" else len(prev_cp)
                prev_cp = prev_cp[:pos] + "\n" + new_block + prev_cp[pos:]
        return prev_cp

    elif tag_type == "AFTER_MARKET":
        am_m = re.search(r"📌시간외 특이종목\n?(.*?)(?=\n📌NXT|\Z)", prev_cp, re.DOTALL)
        new_block = "📌시간외 특이종목\n" + clean_c
        if am_m:
            prev_cp = prev_cp[:am_m.start()] + new_block + prev_cp[am_m.end():]
        else:
            prev_cp = prev_cp.rstrip() + "\n" + new_block
        return prev_cp

    elif tag_type == "NXT":
        nxt_m = re.search(r"📌NXT 괴리율\n?(.*?)(?=\Z)", prev_cp, re.DOTALL)
        new_block = "📌NXT 괴리율\n" + clean_c
        if nxt_m:
            prev_cp = prev_cp[:nxt_m.start()] + new_block + prev_cp[nxt_m.end():]
        else:
            prev_cp = prev_cp.rstrip() + "\n" + new_block
        return prev_cp

    return prev_cp

async def build_checkpoint(buffer: list, date_str: str, prev_checkpoint: str = None, sector_link_store: dict = None) -> str:
    claude_buffer = []
    kospi_items = []
    kosdaq_items = []
    after_market_items = []
    nxt_items = []
    signal_items = []
    for item in buffer:
        tag_type, tag_value, content = item
        if tag_type == "KOSPI":
            kospi_items.append((tag_value, content))
        elif tag_type == "KOSDAQ":
            kosdaq_items.append((tag_value, content))
        elif tag_type == "AFTER_MARKET":
            after_market_items.append(content)
        elif tag_type == "NXT":
            nxt_items.append(content)
        elif tag_type == "SIGNAL":
            signal_items.append((tag_value, content))
        else:
            claude_buffer.append(item)

    if sector_link_store is None:
        sector_link_store = {}

    if claude_buffer or prev_checkpoint:
        structured = format_buffer_for_claude(claude_buffer)
        for m in re.finditer(r"\[\[LINK:([^\]]+)\]\]", structured):
            url = m.group(1)
            for item in claude_buffer:
                tag_type, tag_value, item_content = item
                if tag_type == "SECTOR" and url in item_content:
                    if tag_value not in sector_link_store:
                        sector_link_store[tag_value] = []
                    if url not in sector_link_store[tag_value]:
                        sector_link_store[tag_value].append(url)
        structured_clean = re.sub(r"\s*\[\[LINK:[^\]]+\]\]", "", structured)
        if prev_checkpoint:
            cp_base = re.split(r"\n📌코스피|\n📌시간외|\n📌NXT", prev_checkpoint)[0]
            cp_base_clean = re.sub(r"\s*\[\[LINK:[^\]]+\]\]", "", cp_base)
            cp_base_clean = re.sub(r" *🔗", "", cp_base_clean)
            user_content = (
                f"날짜: {date_str}\n\n기존 체크포인트 (📌코스피/코스닥/시간외/NXT 섹션 제외):\n{cp_base_clean}\n\n"
                f"---\n\n추가 내용 (반영해서 업데이트해줘. 📌코스피/📌코스닥/📌시간외/📌NXT 섹션은 출력하지 말 것):\n\n{structured_clean}"
            )
        else:
            user_content = (
                f"날짜: {date_str}\n\n{structured_clean}\n\n"
                f"※ 📌코스피/📌코스닥 섹션은 출력하지 말 것. Sector와 美증시와 지표만 출력."
                if structured_clean.strip() else f"날짜: {date_str}"
            )
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        base = response.content[0].text.strip()
        for sector_name, urls in sector_link_store.items():
            if f"✔️{sector_name}" not in base:
                continue
            for url in urls:
                if f"[[LINK:{url}]]" in base:
                    continue
                lines = base.split("\n")
                for i, line in enumerate(lines):
                    if line.startswith(f"✔️{sector_name}"):
                        lines[i] = line + f" [[LINK:{url}]]"
                        base = "\n".join(lines)
                        break
    else:
        base = f"{date_str} Check Point✨"

    existing_kospi_map = {}
    existing_kosdaq_map = {}
    if prev_checkpoint:
        def parse_stock_section(section_text: str) -> dict:
            result = {}
            current_name = None
            current_lines = []
            for line in section_text.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("-"):
                    if current_name is not None:
                        link_m = re.search(r"\[\[LINK:([^\]]+)\]\]", line)
                        clean = re.sub(r"\[\[LINK:[^\]]+\]\]", "", line).strip()
                        url = link_m.group(1) if link_m else None
                        current_lines.append((clean, url))
                else:
                    if current_name is not None:
                        result[current_name] = current_lines
                    current_name = line
                    current_lines = []
            if current_name is not None:
                result[current_name] = current_lines
            return result
        kospi_m = re.search(r"📌코스피\n(.*?)(?=\n📌|\Z)", prev_checkpoint, re.DOTALL)
        kosdaq_m = re.search(r"📌코스닥\n(.*?)(?=\n📌|\Z)", prev_checkpoint, re.DOTALL)
        if kospi_m:
            existing_kospi_map = parse_stock_section(kospi_m.group(1))
        if kosdaq_m:
            existing_kosdaq_map = parse_stock_section(kosdaq_m.group(1))

    def summarize_content(content: str) -> list:
        content = content.replace("[기사내용]", "").strip()
        seen = set()
        lines = []
        for l in content.split("\n"):
            l = l.strip()
            if not l:
                continue
            if l.startswith("http"):
                continue
            skip_keywords = ["기자 구독", "구독하기", "Forwarded from", "today at",
                             "naver.com", "hankyung.com", "zdnet", "2026.0", "2025.0",
                             "글자크기", "기사 스크랩", "스크랩", "인쇄", "공유", "댓글",
                             "로그인", "회원가입", "뒤로가기", "font", "Font"]
            if any(skip in l for skip in skip_keywords):
                continue
            korean_chars = sum(1 for c in l if '\uAC00' <= c <= '\uD7A3')
            if korean_chars < 2 and len(l) < 20:
                continue
            key = l.replace("-", "").strip()
            if key in seen:
                continue
            seen.add(key)
            lines.append(l)
        bullets = [l for l in lines if len(l) > 5][:2]
        return [f"- {b}" if not b.startswith("-") else b for b in bullets]

    def add_to_stock_map(items_list, stock_map):
        for name, c in items_list:
            link_m = re.findall(r"\[\[LINK:([^\]]+)\]\]", c)
            clean_c = re.sub(r"\[\[LINK:[^\]]+\]\]", "", c).strip()
            bullets = summarize_content(clean_c)
            link_url = link_m[0] if link_m else None
            if name not in stock_map:
                stock_map[name] = []
            if bullets:
                stock_map[name].append((bullets[0], link_url))
            elif link_url:
                stock_map[name].append(("- 관련 기사", link_url))

    add_to_stock_map(kospi_items, existing_kospi_map)
    add_to_stock_map(kosdaq_items, existing_kosdaq_map)

    def build_stock_block(header: str, stock_map: dict) -> str:
        if not stock_map:
            return ""
        lines_out = [header]
        items = []
        for name, bullets in stock_map.items():
            item_lines = [str(name)]
            for entry in bullets:
                if isinstance(entry, tuple) and len(entry) == 2:
                    text, url = str(entry[0]), entry[1]
                    line = text if text.startswith("-") else "- " + text
                    if url:
                        line = line + f" [[LINK:{url}]]"
                elif isinstance(entry, str):
                    line = entry if entry.startswith("-") else "- " + entry
                else:
                    continue
                item_lines.append(line)
            items.append("\n".join(item_lines))
        lines_out.append("\n\n".join(items))
        return "\n".join(lines_out)

    kospi_block = build_stock_block("📌코스피", existing_kospi_map)
    kosdaq_block = build_stock_block("📌코스닥", existing_kosdaq_map)

    if signal_items and prev_checkpoint:
        sm = re.search(r"📡시장 시그널\n(.*?)(?=\n📌|\n📊|\n🇺🇸|\Z)", prev_checkpoint, re.DOTALL)
        if sm:
            existing_signal_text = sm.group(1).strip()
            if existing_signal_text:
                existing_lines = [l for l in existing_signal_text.split("\n") if l.strip()]
                signal_items = [("", "\n".join(existing_lines))] + signal_items
    if signal_items:
        sig_lines = ["📡시장 시그널"]
        for sig_title, sig_content in signal_items:
            if sig_title:
                link_markers = re.findall(r"\[\[LINK:[^\]]+\]\]", sig_content)
                clean_content = re.sub(r"\s*\[\[LINK:[^\]]+\]\]", "", sig_content).strip()
                title_line = sig_title
                for lm in link_markers:
                    title_line += f" {lm}"
                sig_lines.append("☑️ " + title_line)
                if clean_content.strip():
                    try:
                        summary = await _claude_summarize(clean_content)
                        for line in summary.split("\n"):
                            line = line.strip()
                            if line:
                                sig_lines.append(line if line.startswith("-") else "- " + line)
                    except Exception:
                        first = clean_content.split("\n")[0].strip()
                        if first:
                            sig_lines.append("- " + first[:100])
            else:
                for line in sig_content.split("\n"):
                    if line.strip():
                        sig_lines.append(line)
        signal_block = "\n".join(sig_lines)
    elif prev_checkpoint:
        sm = re.search(r"(📡시장 시그널.*?)(?=\n📌|\n📡|\Z)", prev_checkpoint, re.DOTALL)
        signal_block = sm.group(1).strip() if sm else ""
    else:
        signal_block = ""

    result = base.strip()
    if signal_block:
        sector_markers = ["\n📌Sector", "\n📌sector", "\n📌섹터"]
        inserted = False
        for marker in sector_markers:
            if marker in result:
                idx = result.index(marker)
                result = result[:idx] + "\n\n" + signal_block + "\n" + result[idx:]
                inserted = True
                break
        if not inserted:
            result += "\n\n" + signal_block
    if kospi_block:
        result += "\n\n" + kospi_block
    if kosdaq_block:
        result += "\n\n" + kosdaq_block

    if after_market_items:
        combined_am = "\n\n".join(after_market_items)
        after_market_block = "📌시간외 특이종목\n\n" + combined_am
        result += "\n\n" + after_market_block
    elif prev_checkpoint:
        am_m = re.search(r"(📌시간외 특이종목.*?)(?=\n📌NXT|\n📌코스피|\Z)", prev_checkpoint, re.DOTALL)
        if am_m:
            result += "\n\n" + am_m.group(1).strip()

    if nxt_items:
        combined_nxt = "\n\n".join(nxt_items)
        nxt_block = await summarize_nxt(combined_nxt)
        result += "\n\n" + nxt_block
    elif prev_checkpoint:
        nxt_m = re.search(r"(📌NXT 괴리율.*?)(?=\n📌코스피|\Z)", prev_checkpoint, re.DOTALL)
        if nxt_m:
            result += "\n\n" + nxt_m.group(1).strip()

    return result.strip()

async def apply_partial_edit(checkpoint: str, edit_type: str, target: str, new_content: str) -> str:
    if edit_type == "섹터":
        instruction = f"📌Sector 아래 ✔️{target} 섹션의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "코스피":
        instruction = f"📌코스피 아래 '{target}' 항목의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "코스닥":
        instruction = f"📌코스닥 아래 '{target}' 항목의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "미증시":
        instruction = f"🇺🇸美증시 마감 섹션 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "지표":
        instruction = f"📊지표 섹션 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "시간외":
        instruction = f"📌시간외 특이종목 섹션 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "NXT":
        instruction = f"📌NXT 괴리율 섹션 내용을 아래로 교체해줘:\n{new_content}"
    else:
        instruction = f"'{target}' 항목을 찾아서 내용을 아래로 교체해줘:\n{new_content}"
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        system=EDIT_PROMPT,
        messages=[{"role": "user", "content": f"아래 체크포인트에서 {instruction}\n\n체크포인트:\n{checkpoint}"}],
    )
    return response.content[0].text

# ── 메시지 핸들러 ─────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID != 0 and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    user_text = update.message.text or ""
    entity_urls = []
    msg = update.message
    all_entities = list(msg.entities or []) + list(msg.caption_entities or [])
    entity_text = user_text or msg.caption or ""
    for ent in all_entities:
        if ent.type in ("url", "text_link"):
            if ent.type == "text_link":
                entity_urls.append(ent.url)
            else:
                entity_urls.append(entity_text[ent.offset:ent.offset + ent.length])

    if not user_text.strip() and not entity_urls:
        return

    text = user_text.strip()

    # ── 1) 새 체크포인트 시작 ──
    new_session_match = re.search(
        r"(\d{1,2}/\d{1,2})일?\s*(체크포인트|checkpoint)\s*(생성|시작|열어|만들어|새로)",
        text, re.IGNORECASE
    )
    if new_session_match:
        date_str = new_session_match.group(1)
        user_state[user_id] = {"date": date_str, "buffer": [], "last_checkpoint": "", "pending_tag": None, "sector_link_store": {}}
        await update.message.reply_text(
            f"📅 {date_str} 체크포인트 새로 시작!\n"
            f"태그 예시:\n섹터/폴더블 + 기사내용\n코스닥/아크릴 + 기사내용\n"
            f"지표/\nSOX +1.34%\nVIX 23.95 -1.45%\n야간선물 +3.2%\n"
            f"수정/코스피/LG디스플레이 + 수정내용\n전체수정 + 체크포인트 전문\n"
            f"📸 지표 캡쳐 이미지 전송도 가능!"
        )
        return

    # 이후 처리에서 user_state가 보장돼야 함
    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None, "pending_tag": None}

    # ── 2) 전체수정 ──
    if text.startswith("전체수정"):
        new_checkpoint = text[4:].strip()
        if not new_checkpoint:
            await update.message.reply_text("전체수정 뒤에 체크포인트 내용을 붙여주세요!")
            return
        clean_cp = re.sub(r" *🔗", "", new_checkpoint)
        if not user_state[user_id].get("sector_link_store"):
            user_state[user_id]["sector_link_store"] = await restore_sector_links_from_dashboard()
        sls = user_state[user_id].get("sector_link_store", {})
        for sector_name, urls in sls.items():
            lines = clean_cp.split("\n")
            for i, line in enumerate(lines):
                if line.startswith(f"✔️{sector_name}"):
                    for url in urls:
                        if f"[[LINK:{url}]]" not in line:
                            line = line + f" [[LINK:{url}]]"
                    lines[i] = line
            clean_cp = "\n".join(lines)
        user_state[user_id]["last_checkpoint"] = clean_cp
        user_state[user_id]["buffer"] = []
        date_match = re.search(r"(\d{1,2}/\d{1,2})", clean_cp)
        date_str = date_match.group(1) if date_match else datetime.now().strftime("%-m/%-d")
        if date_match:
            user_state[user_id]["date"] = date_str
        asyncio.create_task(send_to_dashboard(clean_cp, date_str))
        await update.message.reply_text("✅ 전체수정 완료! 베이스로 저장했어요.", parse_mode="HTML")
        return

    # ── 3) 부분수정 ──
    edit_match = re.match(r"^수정\s*/\s*(.+?)\s*/\s*(.+?)[\n\r](.*)", text, re.DOTALL)
    if edit_match:
        edit_type = edit_match.group(1).strip()
        target = edit_match.group(2).strip()
        new_content = edit_match.group(3).strip()
        # 🆕 부분수정 전에도 fresh fetch — 사용자 편집 보존
        await fetch_fresh_state(user_id)
        if not user_state[user_id].get("last_checkpoint"):
            await update.message.reply_text("수정할 체크포인트가 없어요! 먼저 체크포인트를 만들어 주세요.")
            return
        processing_msg = await update.message.reply_text(f"⏳ {edit_type}/{target} 수정 중...")
        try:
            edit_urls = extract_urls(new_content)
            if edit_urls:
                fetched_parts = []
                for eu in edit_urls:
                    fetched = await fetch_url_text(eu)
                    if fetched:
                        fetched_parts.append(fetched)
                    new_content = new_content.replace(eu, "").strip()
                if fetched_parts:
                    new_content = new_content + "\n" + "\n".join(fetched_parts)
                if edit_type == "섹터" and target:
                    if "sector_link_store" not in user_state[user_id]:
                        user_state[user_id]["sector_link_store"] = {}
                    sls = user_state[user_id]["sector_link_store"]
                    if target not in sls:
                        sls[target] = []
                    for eu in edit_urls:
                        if eu not in sls[target]:
                            sls[target].append(eu)
                elif edit_type in ("코스피", "코스닥") and target:
                    new_content = new_content + "\n" + "\n".join(f"[[LINK:{eu}]]" for eu in edit_urls)
            result = await apply_partial_edit(user_state[user_id]["last_checkpoint"], edit_type, target, new_content)
            sls = user_state[user_id].get("sector_link_store", {})
            result_clean = re.sub(r" *🔗", "", result)
            for sector_name, urls in sls.items():
                lines = result_clean.split("\n")
                for i, line in enumerate(lines):
                    if line.startswith(f"✔️{sector_name}"):
                        for url in urls:
                            if f"[[LINK:{url}]]" not in line:
                                line = line + f" [[LINK:{url}]]"
                        lines[i] = line
                result_clean = "\n".join(lines)
            if edit_urls and edit_type in ("코스피", "코스닥") and target:
                lines = result_clean.split("\n")
                in_target = False
                last_bullet_idx = -1
                for i, line in enumerate(lines):
                    if line.strip() == target:
                        in_target = True
                    elif in_target and line.strip().startswith("-"):
                        last_bullet_idx = i
                    elif in_target and (line.startswith("📌") or (line.startswith("✔️"))):
                        break
                if last_bullet_idx >= 0:
                    for url in edit_urls:
                        if f"[[LINK:{url}]]" not in lines[last_bullet_idx]:
                            lines[last_bullet_idx] = lines[last_bullet_idx] + f" [[LINK:{url}]]"
                result_clean = "\n".join(lines)
            user_state[user_id]["last_checkpoint"] = result_clean
            # 부분수정 결과도 대시보드에 즉시 반영
            date_str_now = user_state[user_id].get("date", datetime.now().strftime("%-m/%-d"))
            asyncio.create_task(send_to_dashboard(result_clean, date_str_now))
            html_result = convert_links_to_html(result_clean)
            await processing_msg.delete()
            if len(html_result) <= 4000:
                await update.message.reply_text("✅ 수정 완료!\n\n" + html_result, parse_mode="HTML")
            else:
                await update.message.reply_text("✅ 수정 완료!")
                for i in range(0, len(html_result), 4000):
                    await update.message.reply_text(html_result[i:i+4000], parse_mode="HTML")
        except Exception as e:
            logger.error(f"수정 오류: {e}")
            await processing_msg.edit_text(f"❌ 오류: {str(e)[:100]}")
        return

    # ── 4) 정리해줘 ──
    trigger_words = ["정리해줘", "정리해", "정리 해줘", "뽑아줘"]
    is_trigger = any(word in text for word in trigger_words)
    if is_trigger:
        state = user_state.get(user_id)
        if not state or not state["buffer"]:
            await update.message.reply_text("아직 받은 내용이 없어요! 기사나 뉴스를 먼저 보내주세요 📋")
            return
        processing_msg = await update.message.reply_text("⏳ 통합 정리 중...")
        try:
            date_str = state.get("date", datetime.now().strftime("%-m/%-d"))
            # 🆕 매번 dashboard에서 fresh fetch — 사용자 편집 항상 반영
            await fetch_fresh_state(user_id)
            sls = user_state[user_id]["sector_link_store"]
            result = await build_checkpoint(
                state["buffer"],
                date_str,
                prev_checkpoint=user_state[user_id].get("last_checkpoint"),
                sector_link_store=sls
            )
            user_state[user_id]["last_checkpoint"] = result
            user_state[user_id]["buffer"] = []
            await processing_msg.delete()
            html_result = convert_links_to_html(result)
            MAX = 4000
            if len(html_result) <= MAX:
                await update.message.reply_text(html_result, parse_mode="HTML")
            else:
                for i in range(0, len(html_result), MAX):
                    await update.message.reply_text(html_result[i:i+MAX], parse_mode="HTML")
            asyncio.create_task(send_to_dashboard(result, date_str))
        except Exception as e:
            logger.error(f"분석 오류: {e}")
            await processing_msg.edit_text(f"❌ 오류: {str(e)[:100]}")
        return

    # ── 5) 일반 내용 ──
    if len(text) < 5:
        return

    pending = user_state[user_id].get("pending_tag")

    has_url = bool(extract_urls(text)) or bool(entity_urls)
    if has_url:
        processing_msg = await update.message.reply_text("🔍 링크 읽는 중...")
        enriched_text, found_urls = await enrich_text_with_url(text)
        for eu in entity_urls:
            if eu not in found_urls:
                fetched = await fetch_url_text(eu)
                if fetched:
                    enriched_text = enriched_text + "\n" + fetched
                enriched_text += f"\n[[LINK:{eu}]]"
        await processing_msg.delete()
    else:
        enriched_text = text

    if pending:
        tag_type, tag_value = pending
        content = enriched_text
        user_state[user_id]["pending_tag"] = None
        # 🆕 매번 fresh fetch — 사용자 편집 항상 반영
        await fetch_fresh_state(user_id)
        date_str_now = user_state[user_id].get("date", datetime.now().strftime("%-m/%-d"))
        new_cp = await instant_merge(
            user_state[user_id].get("last_checkpoint", ""),
            tag_type, tag_value, content,
            user_state[user_id]["sector_link_store"], date_str_now
        )
        user_state[user_id]["last_checkpoint"] = new_cp
        asyncio.create_task(send_to_dashboard(new_cp, date_str_now))
        tag_display = {
            "SECTOR": f"✔️섹터/{tag_value}",
            "KOSPI": f"📌코스피/{tag_value}",
            "KOSDAQ": f"📌코스닥/{tag_value}",
            "US_MARKET": f"🇺🇸美증시/{tag_value}" if tag_value else "🇺🇸美증시 마감",
            "INDICATOR": "📊지표",
            "AFTER_MARKET": "📌시간외 특이종목",
            "NXT": "📌NXT 괴리율",
            "SIGNAL": "📡시장 시그널",
        }
        label = tag_display.get(tag_type, tag_value)
        await update.message.reply_text(f"✅ {label} → 대시보드 업데이트됨")
        return

    parsed_blocks = parse_multi_tag(enriched_text)

    def get_label(tt, tv):
        m = {"SECTOR": f"✔️섹터/{tv}", "KOSPI": f"📌코스피/{tv}", "KOSDAQ": f"📌코스닥/{tv}",
             "US_MARKET": "🇺🇸美증시 마감", "INDICATOR": "📊지표",
             "AFTER_MARKET": "📌시간외 특이종목", "NXT": "📌NXT 괴리율", "AUTO": "🔍자동분류"}
        return m.get(tt, tv)

    # 단일 태그-only → pending or 재태깅
    if len(parsed_blocks) == 1:
        tag_type, tag_value, content = parsed_blocks[0]
        is_tag_only = (
            tag_type in ("SECTOR", "KOSPI", "KOSDAQ", "AFTER_MARKET", "NXT", "SIGNAL") and
            not content.strip()
        )
        if is_tag_only:
            label = get_label(tag_type, tag_value)
            buf = user_state[user_id]["buffer"]
            if buf and buf[-1][0] == "AUTO":
                _, _, prev_content = buf[-1]
                buf[-1] = (tag_type, tag_value, prev_content)
                count = len(buf)
                is_append = bool(user_state[user_id].get("last_checkpoint"))
                mode = "추가" if is_append else "누적"
                await update.message.reply_text(f"✅ 방금 내용을 {label}로 재태깅했어요! ({count}개 {mode})")
            else:
                user_state[user_id]["pending_tag"] = (tag_type, tag_value)
                await update.message.reply_text(f"📌 {label} 태그 받았어요! 다음 메시지를 이 태그로 묶을게요 ✅")
            return

    # 멀티 태그 or 단일 태그+내용
    added_labels = []
    pending = user_state[user_id].get("pending_tag")
    # 🆕 한 메시지에 여러 블록 있어도 시작 시점에 한 번만 fresh fetch
    await fetch_fresh_state(user_id)
    for tag_type, tag_value, content in parsed_blocks:
        if not content.strip():
            continue
        if tag_type == "AUTO" and pending:
            tag_type, tag_value = pending
            pending = None
        state = user_state[user_id]
        date_str_now = state.get("date", datetime.now().strftime("%-m/%-d"))
        sls = state["sector_link_store"]
        new_cp = await instant_merge(
            state.get("last_checkpoint", ""), tag_type, tag_value, content, sls, date_str_now
        )
        user_state[user_id]["last_checkpoint"] = new_cp
        asyncio.create_task(send_to_dashboard(new_cp, date_str_now))
        added_labels.append(get_label(tag_type, tag_value))

    user_state[user_id]["pending_tag"] = None

    if added_labels:
        labels_str = ", ".join(added_labels)
        await update.message.reply_text(f"✅ {labels_str} → 대시보드 업데이트됨")


# ── 이미지 핸들러 ─────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID != 0 and user_id != ALLOWED_USER_ID:
        return

    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None, "pending_tag": None}

    caption = (update.message.caption or "").strip()
    if caption:
        cap_type, cap_value, _ = parse_user_tag(caption)
        if cap_type in ("SECTOR", "KOSPI", "KOSDAQ", "NXT", "AFTER_MARKET"):
            pending = (cap_type, cap_value)
        else:
            pending = user_state[user_id].get("pending_tag")
    else:
        pending = user_state[user_id].get("pending_tag")

    processing_msg = await update.message.reply_text("📸 이미지 읽는 중...")

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        async with aiohttp.ClientSession() as session:
            async with session.get(file.file_path) as resp:
                image_bytes = await resp.read()

        if pending and pending[0] in ("SECTOR", "KOSPI", "KOSDAQ", "NXT"):
            tag_type, tag_value = pending
            extracted = await extract_sector_content_from_image(image_bytes, tag_type, tag_value, "image/jpeg")
            if not extracted:
                await processing_msg.edit_text("❌ 이미지에서 내용을 읽지 못했어요. 다시 시도해주세요.")
                return
            user_state[user_id]["pending_tag"] = None
            # 🆕 매번 fresh fetch — 사용자 편집 항상 반영
            await fetch_fresh_state(user_id)
            date_str_now = user_state[user_id].get("date", datetime.now().strftime("%-m/%-d"))
            new_cp = await instant_merge(
                user_state[user_id].get("last_checkpoint", ""),
                tag_type, tag_value, extracted,
                user_state[user_id]["sector_link_store"], date_str_now
            )
            user_state[user_id]["last_checkpoint"] = new_cp
            asyncio.create_task(send_to_dashboard(new_cp, date_str_now))
            tag_display = {"SECTOR": f"✔️섹터/{tag_value}", "KOSPI": f"📌코스피/{tag_value}",
                           "KOSDAQ": f"📌코스닥/{tag_value}", "NXT": "📌NXT 괴리율"}
            label = tag_display.get(tag_type, tag_value)
            await processing_msg.delete()
            await update.message.reply_text(f"✅ {label} → 대시보드 업데이트됨\n\n📷 인식 결과:\n{extracted}")

        else:
            extracted = await extract_indicators_from_image(image_bytes, "image/jpeg")
            if not extracted:
                await processing_msg.edit_text("❌ 이미지에서 지표를 읽지 못했어요. 다시 시도해주세요.")
                return
            # 🆕 매번 fresh fetch — 사용자 편집 항상 반영
            await fetch_fresh_state(user_id)
            date_str_now = user_state[user_id].get("date", datetime.now().strftime("%-m/%-d"))
            new_cp = await instant_merge(
                user_state[user_id].get("last_checkpoint", ""),
                "INDICATOR", "", extracted,
                user_state[user_id]["sector_link_store"], date_str_now
            )
            user_state[user_id]["last_checkpoint"] = new_cp
            asyncio.create_task(send_to_dashboard(new_cp, date_str_now))
            await processing_msg.delete()
            await update.message.reply_text(f"✅ 📊지표 → 대시보드 업데이트됨\n\n📊지표\n{extracted}")
    except Exception as e:
        logger.error(f"이미지 처리 오류: {e}")
        await processing_msg.edit_text(f"❌ 오류: {str(e)[:100]}")


HELP_TEXT = """📋 CheckPoint Bot 명령어

📅 세션시작
3/16 체크포인트 생성

📥 내용 쌓기
섹터/전력설비 + 기사내용
코스피/삼성전자 + 기사내용
코스닥/아크릴 + 기사내용
태그 없이 붙여넣기 → 자동분류
다우/나스닥 포함 텍스트 → 美증시 마감

📊 지표 입력 (텍스트)
지표/
SOX +1.34%
VIX 23.95 -1.45%
EWY +6.38%
WTI 90.70 +2.92%
야간선물 +3.2%

📸 이미지 입력
태그 없이 이미지 → 지표(INDICATOR)로 인식
섹터/방산 후 이미지 → ✔️방산 칸에 종목·수치 저장
코스피/한화에어로 후 이미지 → 📌코스피 칸에 저장
코스닥/이수페타시스 후 이미지 → 📌코스닥 칸에 저장
NXT/ 후 이미지 → 📌NXT 괴리율 표 인식 저장

📊 시간외/NXT 입력
시간외/
(시간외 특이종목 데이터 붙여넣기)

NXT/
(NXT 괴리율 텍스트 붙여넣기)

✅ 정리
정리해줘

✏️ 부분수정
수정/코스피/LG디스플레이
- 새내용

🔄 전체수정
전체수정
3/16 Check Point✨
..."""

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    # 🆕 진단 — 환경변수 상태 즉시 확인
    logger.info(f"🔧 DASHBOARD_URL = {DASHBOARD_URL!r}")
    logger.info(f"🔧 API_SECRET    = {'<SET, len=' + str(len(DASHBOARD_API_SECRET)) + '>' if DASHBOARD_API_SECRET else '<EMPTY>'}")
    logger.info("🚀 CheckPoint Bot 시작! (대시보드 fresh-fetch 모드)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
