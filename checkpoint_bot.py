"""
Morning Broadcast CheckPoint Bot 🌅
방송 전 뉴스 → 섹터/종목 자동 분류 텔레그램 봇
+ 지표 텍스트 태그 지원
+ 이미지 캡쳐 → Claude Vision 자동 인식
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
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")           # 예: https://yenny.railway.app
DASHBOARD_API_SECRET = os.environ.get("API_SECRET", "anchoryen")  # 대시보드와 동일한 변수 사용

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── 대시보드 전송 ─────────────────────────────────────────
_last_dashboard_error = ""

def convert_links_to_html(text: str) -> str:
    """[[LINK:url]] 마커를 HTML 링크로 변환하고 나머지 텍스트 HTML 이스케이프"""
    import re as _re
    # 마커 추출 후 플레이스홀더로 교체
    links = {}
    def replacer(m):
        key = f"__LINKPH{len(links)}__"
        links[key] = f'<a href="{m.group(1)}">🔗</a>'
        return " " + key
    text = _re.sub(r"\[\[LINK:([^\]]+)\]\]", replacer, text)
    # HTML 이스케이프
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 플레이스홀더 복원
    for key, val in links.items():
        text = text.replace(key, val)
    return text

async def send_to_dashboard(content: str, date_str: str) -> bool:
    """체크포인트를 대시보드 /api/post/checkpoint 로 전송"""
    global _last_dashboard_error
    if not DASHBOARD_URL:
        _last_dashboard_error = "DASHBOARD_URL 미설정"
        return False
    url = DASHBOARD_URL.rstrip("/") + "/api/post/checkpoint"
    payload = {"content": content, "date": date_str}
    headers = {
        "Content-Type": "application/json",
        "X-API-Secret": DASHBOARD_API_SECRET,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                body = await resp.text()
                if resp.status == 200:
                    logger.info("대시보드 전송 성공")
                    _last_dashboard_error = ""
                    return True
                else:
                    _last_dashboard_error = f"HTTP {resp.status}: {body[:150]}"
                    logger.error(f"대시보드 전송 실패 {resp.status}: {body[:200]}")
                    return False
    except Exception as e:
        _last_dashboard_error = f"예외: {str(e)[:150]}"
        logger.error(f"대시보드 전송 오류: {e}")
        return False

# ── URL 크롤링 ──────────────────────────────────────────
async def fetch_url_text(url: str) -> str | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    }
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
    """URL 크롤링 + 원본 URL 목록 반환"""
    urls = extract_urls(text)
    if not urls:
        return text, []
    enriched = text
    found_urls = []
    for url in urls:
        fetched = await fetch_url_text(url)
        if fetched:
            enriched = enriched.replace(url, fetched)
            logger.info(f"크롤링 성공: {url}")
        else:
            enriched = enriched.replace(url, "")
            logger.info(f"크롤링 실패, 원문 텍스트 사용: {url}")
        found_urls.append(url)
    # URL 마커를 content 끝에 추가
    markers = "".join(f"\n[[LINK:{u}]]" for u in found_urls)
    return enriched.strip() + markers, found_urls

# ── 이미지 → Claude Vision으로 지표 추출 ──────────────────
async def extract_indicators_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
    """이미지에서 시장 지표 수치를 추출해 텍스트로 반환"""
    try:
        image_data = base64.standard_b64encode(image_bytes).decode("utf-8")
        response = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "이 이미지에서 시장 지표 수치만 추출해줘.\n"
                            "형식: 항목명 현재값 (등락%)\n"
                            "등락률은 반드시 괄호 안에 넣을 것.\n"
                            "예시:\n"
                            "SOX 7,773.13 (+1.34%)\n"
                            "VIX 23.95 (-1.45%)\n"
                            "EWY 133.81 (+6.38%)\n"
                            "WTI 90.70 (+2.92%)\n"
                            "DXY 99.17 (+0.23%)\n"
                            "US10Y 4.362% (+0.026)\n\n"
                            "수치가 없는 항목은 제외. 설명 없이 수치만 나열."
                        )
                    }
                ],
            }]
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"이미지 분석 오류: {e}")
        return None


async def extract_sector_content_from_image(
    image_bytes: bytes,
    tag_type: str,
    tag_value: str,
    mime_type: str = "image/jpeg"
) -> str | None:
    """섹터/코스피/코스닥 태그가 걸린 상태에서 이미지를 받으면
    종목명·현재가·등락률을 추출해 섹터 기사 형식 텍스트로 반환"""
    try:
        image_data = base64.standard_b64encode(image_bytes).decode("utf-8")

        if tag_type == "NXT":
            prompt = (
                "이 이미지는 NXT 괴리율 표야.\n"
                "표에서 종목명, KRX 종가, NXT 종가, 괴리율(%), 이유를 모두 추출해줘.\n"
                "형식: 종목명 KRX가 NXT가 괴리율% [이유]\n"
                "예시:\n"
                "넥스틸 12,290 14,420 +17.33% [걸프 송유관 수출 기대]\n"
                "한올바이오파마 54,400 44,050 -19.03%\n\n"
                "이유 없으면 괄호 생략. 설명 없이 목록만 나열."
            )
        else:
            if tag_type == "SECTOR":
                context = f"'{tag_value}' 섹터 관련 종목 화면"
            elif tag_type == "KOSPI":
                context = f"코스피 종목 '{tag_value}' 관련 화면"
            elif tag_type == "KOSDAQ":
                context = f"코스닥 종목 '{tag_value}' 관련 화면"
            else:
                context = "시장 화면"

            prompt = (
                f"이 이미지는 {context}야.\n"
                "이미지에 보이는 종목명(또는 티커)과 현재가, 등락률을 모두 추출해줘.\n"
                "형식: 종목명(티커) 현재가 (등락률)\n"
                "예시:\n"
                "KDEF 56.25 USD (+6.68%)\n"
                "한화에어로스페이스 85,400원 (+3.21%)\n\n"
                "수치가 없으면 종목명만 적어도 됨.\n"
                "설명·부연 없이 수치 목록만 나열할 것."
            )

        response = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": prompt}
                ],
            }]
        )
        return response.content[0].text.strip()
    except Exception as e:
        logger.error(f"섹터 이미지 분석 오류: {e}")
        return None

# ── 사용자별 상태 저장 ────────────────────────────────────
# { user_id: { "date": "3/13", "buffer": [...], "last_checkpoint": "...",
#              "pending_tag": (tag_type, tag_value),
#              "pending_indicator_image": "텍스트" } }
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
14. 각 섹터(✔️)의 불릿은 반드시 최대 2개. 핵심만. 절대 3개 이상 쓰지 말 것.

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
    """시간외 특이종목 데이터를 Claude로 요약"""
    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=1000,
        system=AFTER_MARKET_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()


async def summarize_nxt(content: str) -> str:
    """NXT 괴리율 데이터를 Claude로 요약"""
    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=1000,
        system=NXT_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()

def parse_multi_tag(text: str) -> list:
    """한 메시지에 여러 태그 블록이 있을 때 모두 분리해서 반환.
    단일 태그 메시지도 그대로 동작.
    반환: [(tag_type, tag_value, content), ...]
    """
    TAG_START = re.compile(
        r"^(섹터|코스피|코스닥|지표|시간외|NXT|시그널)\s*/",
        re.IGNORECASE | re.MULTILINE
    )
    matches = list(TAG_START.finditer(text))
    if not matches:
        return [parse_user_tag(text)]

    blocks = []
    # 태그 앞에 내용이 있으면 AUTO로
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
    """사용자 태그 추출 — 지표/ 태그 추가"""
    # 지표 태그
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

    # 시간외 태그
    if re.match(r"^시간외\s*/\s*", text, re.IGNORECASE):
        content = re.sub(r"^시간외\s*/\s*", "", text, flags=re.IGNORECASE).strip()
        return "AFTER_MARKET", "", content

    # 시그널 태그
    if re.match(r"^시그널\s*/\s*", text, re.IGNORECASE):
        content = re.sub(r"^시그널\s*/\s*", "", text, flags=re.IGNORECASE).strip()
        return "SIGNAL", "", content

    # NXT 태그
    if re.match(r"^NXT\s*/\s*", text, re.IGNORECASE):
        content = re.sub(r"^NXT\s*/\s*", "", text, flags=re.IGNORECASE).strip()
        return "NXT", "", content

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

    # 지표 섹션 맨 앞에
    if indicator_lines:
        combined_indicator = "\n".join(indicator_lines)
        parts.insert(0, f"[INDICATOR]\n{combined_indicator}")

    if us_market_lines:
        combined_us = "\n".join(us_market_lines)
        parts.insert(1 if indicator_lines else 0, f"[US_MARKET]\n{combined_us}")

    return "\n\n---\n\n".join(parts)

async def build_checkpoint(buffer: list, date_str: str, prev_checkpoint: str = None) -> str:
    """체크포인트 생성. KOSPI/KOSDAQ/AFTER_MARKET/NXT는 코드에서 직접 처리."""
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
            signal_items.append(content)
        else:
            claude_buffer.append(item)

    if claude_buffer or prev_checkpoint:
        structured = format_buffer_for_claude(claude_buffer)
        if prev_checkpoint:
            cp_base = re.split(r"\n📌코스피|\n📌시간외|\n📌NXT", prev_checkpoint)[0]
            user_content = (
                f"날짜: {date_str}\n\n기존 체크포인트 (📌코스피/코스닥/시간외/NXT 섹션 제외):\n{cp_base}\n\n"
                f"---\n\n추가 내용 (반영해서 업데이트해줘. 📌코스피/📌코스닥/📌시간외/📌NXT 섹션은 출력하지 말 것):\n\n{structured}"
            )
        else:
            user_content = (
                f"날짜: {date_str}\n\n{structured}\n\n"
                f"※ 📌코스피/📌코스닥 섹션은 출력하지 말 것. Sector와 美증시와 지표만 출력."
                if structured.strip() else f"날짜: {date_str}"
            )

        response = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        base = response.content[0].text.strip()
    else:
        base = f"{date_str} Check Point✨"

    # 기존 코스피/코스닥 파싱
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
                    if current_name:
                        current_lines.append(line)
                else:
                    if current_name:
                        result[current_name] = current_lines
                    current_name = line
                    current_lines = []
            if current_name:
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

    for name, content in kospi_items:
        existing_kospi_map[name] = summarize_content(content)
    for name, content in kosdaq_items:
        existing_kosdaq_map[name] = summarize_content(content)

    def build_stock_block(header: str, stock_map: dict) -> str:
        if not stock_map:
            return ""
        lines_out = [header]
        items = []
        for name, bullets in stock_map.items():
            item_lines = [name] + bullets
            items.append("\n".join(item_lines))
        lines_out.append("\n\n".join(items))
        return "\n".join(lines_out)

    kospi_block = build_stock_block("📌코스피", existing_kospi_map)
    kosdaq_block = build_stock_block("📌코스닥", existing_kosdaq_map)

    # ── 시장 시그널 ──
    if signal_items:
        signal_block = "📡시장 시그널\n" + "\n".join(signal_items)
    elif prev_checkpoint:
        sm = re.search(r"(📡시장 시그널.*?)(?=\n📌|\n📡|\Z)", prev_checkpoint, re.DOTALL)
        signal_block = sm.group(1).strip() if sm else ""
    else:
        signal_block = ""

    # 시그널은 📌Sector 바로 앞에 삽입
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

    # ── 시간외 특이종목 처리 ──
    # 새 데이터가 있으면 Claude로 요약, 없으면 기존 prev 섹션 유지
    if after_market_items:
        combined_am = "\n\n".join(after_market_items)
        after_market_block = "📌시간외 특이종목\n\n" + combined_am
        result += "\n\n" + after_market_block
    elif prev_checkpoint:
        am_m = re.search(r"(📌시간외 특이종목.*?)(?=\n📌NXT|\n📌코스피|\Z)", prev_checkpoint, re.DOTALL)
        if am_m:
            result += "\n\n" + am_m.group(1).strip()

    # ── NXT 괴리율 처리 ──
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
        model="claude-opus-4-20250514",
        max_tokens=2000,
        system=EDIT_PROMPT,
        messages=[{
            "role": "user",
            "content": f"아래 체크포인트에서 {instruction}\n\n체크포인트:\n{checkpoint}"
        }],
    )
    return response.content[0].text

# ── 메시지 핸들러 ─────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ALLOWED_USER_ID != 0 and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    user_text = update.message.text or ""
    # 텔레그램 entities에서 URL 추출 (포워드 메시지 링크 포함)
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
        r"(\d{1,2}/\d{1,2})\s*(체크포인트|checkpoint)\s*(생성|시작|열어|만들어)",
        text, re.IGNORECASE
    )
    if new_session_match:
        date_str = new_session_match.group(1)
        user_state[user_id] = {"date": date_str, "buffer": [], "last_checkpoint": None, "pending_tag": None}
        await update.message.reply_text(
            f"📅 {date_str} 체크포인트 새로 시작!\n"
            f"태그 예시:\n"
            f"섹터/폴더블 + 기사내용\n"
            f"코스닥/아크릴 + 기사내용\n"
            f"지표/\nSOX +1.34%\nVIX 23.95 -1.45%\n야간선물 +3.2%\n"
            f"수정/코스피/LG디스플레이 + 수정내용\n"
            f"전체수정 + 체크포인트 전문\n"
            f"📸 지표 캡쳐 이미지 전송도 가능!"
        )
        return

    # ── 2) 전체수정 ──
    if text.startswith("전체수정"):
        new_checkpoint = text[4:].strip()
        if not new_checkpoint:
            await update.message.reply_text("전체수정 뒤에 체크포인트 내용을 붙여주세요!")
            return
        if user_id not in user_state:
            today = datetime.now().strftime("%-m/%-d")
            user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None, "pending_tag": None}
        user_state[user_id]["last_checkpoint"] = new_checkpoint
        user_state[user_id]["buffer"] = []
        date_match = re.search(r"(\d{1,2}/\d{1,2})", new_checkpoint)
        date_str = date_match.group(1) if date_match else datetime.now().strftime("%-m/%-d")
        if date_match:
            user_state[user_id]["date"] = date_str
        ok = await send_to_dashboard(new_checkpoint, date_str)
        status = "📤 대시보드 전송 OK" if ok else f"⚠️ 전송 실패: {_last_dashboard_error}"
        await update.message.reply_text(f"✅ 전체수정 완료! 베이스로 저장했어요.\n{status}", parse_mode="HTML")
        return

    # ── 3) 부분수정 ──
    edit_match = re.match(r"^수정\s*/\s*(.+?)\s*/\s*(.+?)[\n\r](.*)", text, re.DOTALL)
    if edit_match:
        edit_type = edit_match.group(1).strip()
        target = edit_match.group(2).strip()
        new_content = edit_match.group(3).strip()
        state = user_state.get(user_id)
        if not state or not state.get("last_checkpoint"):
            await update.message.reply_text("수정할 체크포인트가 없어요! 먼저 체크포인트를 만들어 주세요.")
            return
        processing_msg = await update.message.reply_text(f"⏳ {edit_type}/{target} 수정 중...")
        try:
            result = await apply_partial_edit(state["last_checkpoint"], edit_type, target, new_content)
            user_state[user_id]["last_checkpoint"] = result
            await processing_msg.delete()
            await update.message.reply_text("✅ 수정 완료!\n\n" + result)
        except Exception as e:
            logger.error(f"수정 오류: {e}")
            await processing_msg.edit_text(f"❌ 오류: {str(e)[:100]}")
        return

    # ── 4) 정리 요청 ──
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
            result = await build_checkpoint(
                state["buffer"],
                date_str,
                prev_checkpoint=state.get("last_checkpoint")
            )
            user_state[user_id]["last_checkpoint"] = result
            user_state[user_id]["buffer"] = []
            await processing_msg.delete()
            # 링크 마커 → HTML 변환
            html_result = convert_links_to_html(result)
            # 4096자 초과 시 분할 전송
            MAX = 4000
            if len(html_result) <= MAX:
                await update.message.reply_text(html_result, parse_mode="HTML")
            else:
                for i in range(0, len(html_result), MAX):
                    await update.message.reply_text(html_result[i:i+MAX], parse_mode="HTML")
            # ── 대시보드 자동 전송 ──
            ok = await send_to_dashboard(result, date_str)
            await update.message.reply_text("📤 대시보드 전송 OK" if ok else f"⚠️ 전송 실패: {_last_dashboard_error}")
        except Exception as e:
            logger.error(f"분석 오류: {e}")
            await processing_msg.edit_text(f"❌ 오류: {str(e)[:100]}")
        return

    # ── 5) 일반 내용 → 버퍼에 쌓기 ──
    if len(text) < 5:
        return

    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None, "pending_tag": None}

    pending = user_state[user_id].get("pending_tag")

    has_url = bool(extract_urls(text)) or bool(entity_urls)
    if has_url:
        processing_msg = await update.message.reply_text("🔍 링크 읽는 중...")
        enriched_text, found_urls = await enrich_text_with_url(text)
        # entity_urls 중 아직 안 처리된 것 추가
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
        user_state[user_id]["buffer"].append((tag_type, tag_value, content))
        count = len(user_state[user_id]["buffer"])
        tag_display = {
            "SECTOR": f"✔️섹터/{tag_value}",
            "KOSPI": f"📌코스피/{tag_value}",
            "KOSDAQ": f"📌코스닥/{tag_value}",
            "US_MARKET": "🇺🇸美증시 마감",
            "INDICATOR": "📊지표",
            "AFTER_MARKET": "📌시간외 특이종목",
            "NXT": "📌NXT 괴리율",
        "SIGNAL": "📡시장 시그널",
        }
        label = tag_display.get(tag_type, tag_value)
        is_append = bool(user_state[user_id].get("last_checkpoint"))
        mode = "추가" if is_append else "누적"
        await update.message.reply_text(
            f"✅ {label} 기사 묶었어요! ({count}개 {mode}) '정리해줘' 하시면 {'업데이트' if is_append else '정리'}할게요!"
        )
        return

    parsed_blocks = parse_multi_tag(enriched_text)

    def get_label(tt, tv):
        m = {"SECTOR": f"✔️섹터/{tv}", "KOSPI": f"📌코스피/{tv}", "KOSDAQ": f"📌코스닥/{tv}",
             "US_MARKET": "🇺🇸美증시 마감", "INDICATOR": "📊지표",
             "AFTER_MARKET": "📌시간외 특이종목", "NXT": "📌NXT 괴리율", "AUTO": "🔍자동분류"}
        return m.get(tt, tv)

    # ── 단일 태그-only → pending or 재태깅 ──
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
                await update.message.reply_text(
                    f"✅ 방금 내용을 {label}로 재태깅했어요! ({count}개 {mode})"
                )
            else:
                user_state[user_id]["pending_tag"] = (tag_type, tag_value)
                await update.message.reply_text(
                    f"📌 {label} 태그 받았어요! 다음 메시지를 이 태그로 묶을게요 ✅"
                )
            return

    # ── 멀티 태그 or 단일 태그+내용 → 전체 버퍼에 추가 ──
    added_labels = []
    pending = user_state[user_id].get("pending_tag")
    for tag_type, tag_value, content in parsed_blocks:
        if not content.strip():
            continue
        # AUTO인데 pending 태그 있으면 pending으로 덮어씌우기
        if tag_type == "AUTO" and pending:
            tag_type, tag_value = pending
            pending = None  # 한 번만 적용
        user_state[user_id]["buffer"].append((tag_type, tag_value, content))
        added_labels.append(get_label(tag_type, tag_value))

    user_state[user_id]["pending_tag"] = None

    if added_labels:
        count = len(user_state[user_id]["buffer"])
        is_append = bool(user_state[user_id].get("last_checkpoint"))
        mode = "추가" if is_append else "누적"
        if len(added_labels) == 1:
            await update.message.reply_text(
                f"✅ {added_labels[0]} ({count}개 {mode}) '정리해줘' 하시면 {'업데이트' if is_append else '정리'}할게요!"
            )
        else:
            labels_str = "\n".join(f"  · {l}" for l in added_labels)
            await update.message.reply_text(
                f"✅ {len(added_labels)}개 태그 한꺼번에 저장! ({count}개 {mode})\n{labels_str}\n\n'정리해줘' 하시면 {'업데이트' if is_append else '정리'}할게요!"
            )


# ── 이미지 핸들러 ─────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """이미지 수신 → pending 태그 유무에 따라 분기
    - pending 태그 없음: 지표(INDICATOR)로 추출
    - pending 태그 있음(SECTOR/KOSPI/KOSDAQ): 종목·수치 추출 후 해당 태그로 저장
    """
    user_id = update.effective_user.id
    if ALLOWED_USER_ID != 0 and user_id != ALLOWED_USER_ID:
        return

    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None, "pending_tag": None}

    # 캡션에 태그가 있으면 우선 적용 (예: 캡션 = "섹터/방산")
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

        # ── 분기: pending 태그가 있으면 섹터/종목 내용 추출 ──
        if pending and pending[0] in ("SECTOR", "KOSPI", "KOSDAQ", "NXT"):
            tag_type, tag_value = pending
            extracted = await extract_sector_content_from_image(image_bytes, tag_type, tag_value, "image/jpeg")

            if not extracted:
                await processing_msg.edit_text("❌ 이미지에서 내용을 읽지 못했어요. 다시 시도해주세요.")
                return

            user_state[user_id]["buffer"].append((tag_type, tag_value, extracted))
            user_state[user_id]["pending_tag"] = None
            count = len(user_state[user_id]["buffer"])
            is_append = bool(user_state[user_id].get("last_checkpoint"))
            mode = "추가" if is_append else "누적"

            tag_display = {
                "SECTOR": f"✔️섹터/{tag_value}",
                "KOSPI": f"📌코스피/{tag_value}",
                "KOSDAQ": f"📌코스닥/{tag_value}",
                "NXT": "📌NXT 괴리율",
            }
            label = tag_display.get(tag_type, tag_value)

            await processing_msg.delete()
            await update.message.reply_text(
                f"✅ {label} 이미지 저장 완료! ({count}개 {mode})\n\n"
                f"📷 인식 결과:\n{extracted}\n\n"
                f"'정리해줘' 하시면 {'업데이트' if is_append else '정리'}할게요!"
            )

        # ── 기본: pending 태그 없으면 지표 추출 ──
        else:
            extracted = await extract_indicators_from_image(image_bytes, "image/jpeg")

            if not extracted:
                await processing_msg.edit_text("❌ 이미지에서 지표를 읽지 못했어요. 다시 시도해주세요.")
                return

            user_state[user_id]["buffer"].append(("INDICATOR", "", extracted))
            count = len(user_state[user_id]["buffer"])
            is_append = bool(user_state[user_id].get("last_checkpoint"))
            mode = "추가" if is_append else "누적"

            await processing_msg.delete()
            await update.message.reply_text(
                f"✅ 📊지표 저장 완료! ({count}개 {mode})\n\n"
                f"📊지표\n{extracted}\n\n"
                f"'정리해줘' 하시면 {'업데이트' if is_append else '정리'}할게요!"
            )

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
또는 NXT/ 캡션 달아서 이미지 전송

✅ 정리
정리해줘

✏️ 부분수정
수정/코스피/LG디스플레이
- 새내용

수정/지표/
새 지표 내용

🔄 전체수정
전체수정
3/16 Check Point✨
...전체내용..."""

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("help", help_command))
    # 이미지 핸들러 추가
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🚀 CheckPoint Bot 시작!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
