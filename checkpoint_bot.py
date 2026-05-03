"""
Morning Broadcast CheckPoint Bot 🌅
방송 전 뉴스 → 섹터/종목 자동 분류 텔레그램 봇
+ 여러 태그 한 번에 입력 가능
+ 대시보드 자동 전송
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

# 대시보드 설정
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
API_SECRET = os.environ.get("API_SECRET", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── 대시보드 전송 ─────────────────────────────────────────
async def send_to_dashboard(content: str, date_label: str) -> tuple[bool, str]:
    """대시보드로 체크포인트 전송. (성공여부, 에러메시지) 반환"""
    if not DASHBOARD_URL:
        return False, "DASHBOARD_URL 환경변수 없음"
    if not API_SECRET:
        return False, "API_SECRET 환경변수 없음"
    try:
        date_iso = datetime.now().strftime("%Y-%m-%d")
        url = f"{DASHBOARD_URL}/api/post/checkpoint"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Secret": API_SECRET,
                },
                json={"content": content, "date": date_iso},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    logger.info(f"✅ 대시보드 전송 성공: {date_label}")
                    return True, ""
                else:
                    body = await resp.text()
                    err = f"HTTP {resp.status}: {body[:150]}"
                    logger.error(f"❌ 대시보드 전송 실패: {err}")
                    return False, err
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:150]}"
        logger.error(f"❌ 대시보드 전송 오류: {err}")
        return False, err


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


async def enrich_text_with_url(text: str) -> str:
    urls = extract_urls(text)
    if not urls:
        return text
    enriched = text
    for url in urls:
        fetched = await fetch_url_text(url)
        if fetched:
            enriched = enriched.replace(url, f"{url}\n{fetched}")
    return enriched


# ── 이미지 → Claude Vision으로 지표 추출 ──────────────────
async def extract_indicators_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
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


# ── 사용자별 상태 저장 ────────────────────────────────────
# { user_id: { "date": "3/13", "buffer": [...], "last_checkpoint": "..." } }
user_state = {}


# ── Claude 프롬프트 ────────────────────────────────────────
SYSTEM_PROMPT = """너는 한국 경제방송 앵커의 방송 전 브리핑을 도와주는 전문 어시스턴트야.

입력 데이터는 아래 형식으로 구조화되어 들어와:

[태그유형: 값]
내용

태그 유형:
- SECTOR: 섹터/테마명 (사용자 지정 — 반드시 그대로 사용)
- KOSPI: 코스피 종목명 (사용자 지정)
- KOSDAQ: 코스닥 종목명 (사용자 지정)
- US_MARKET: 미증시 마감 내용
- INDICATOR: 시장 지표 (수치 그대로 유지)
- AUTO: 태그 없이 들어온 일반 기사

규칙:
1. SECTOR 태그가 있으면 → 반드시 📌Sector 아래 ✔️섹터명으로 표시
2. KOSPI 태그는 반드시 📌코스피 칸에만. 섹터로 승격 금지.
3. KOSDAQ 태그는 반드시 📌코스닥 칸에만. 섹터로 승격 금지.
4. US_MARKET 태그는 📌美증시 마감 칸에만 표시. 없으면 섹션 통째로 생략.
5. INDICATOR 태그는 📌지표 섹션으로 맨 위에. 수치 절대 수정 금지. 없으면 섹션 생략.
6. AUTO 태그는 네가 섹터 판단해서 분류
7. ** 볼드 표시 절대 금지
8. 섹터 중분류는 ✔️ 사용
9. 섹터 안 "관련 종목:" 줄에는 등락률 ±2% 이상만 포함
10. 같은 종목이 섹터에도 들어가고 코스피/코스닥에도 들어가면 두 군데 모두 유지하되 표현 약간 다르게
11. 등락률에 따라온 ⏰(시간외)/🌙(NXT) 이모지는 그대로 유지
12. 각 섹터(✔️)의 불릿은 최대 2개. 핵심만.
13. 출처 정보가 들어왔어도 결과에는 사실 한 줄만 정리

출력 형식:
{날짜} Check Point✨

📌지표
[INDICATOR 내용. 없으면 섹션 통째로 생략]

📌美증시 마감
[US_MARKET 내용. 없으면 섹션 통째로 생략]

📌Sector
✔️[섹터명]
- 핵심 내용
- 관련 종목: 종목A(+X%⏰), 종목B(+Y%⏰)

📌코스피
[종목명]
- 관련 내용 (등락률⏰)

📌코스닥
[종목명]
- 관련 내용 (등락률⏰)
"""

EDIT_PROMPT = """너는 체크포인트 문서를 수정하는 어시스턴트야.

규칙:
1. ** 볼드 표시 절대 금지
2. 섹터 중분류는 ✔️ 사용
3. 원본 형식과 구조 유지하면서 해당 항목만 수정
4. 수정 지시 외 부분은 절대 건드리지 말 것
5. 전체 체크포인트를 그대로 출력 (수정된 부분 포함)"""


# ── 멀티 태그 파싱 (여러 태그를 한 번에) ──────────────────
def parse_multi_tag_message(text: str) -> list:
    """
    한 메시지에 여러 태그가 있을 때 자동 분리.
    예시:
        섹터/반도체
        - 반도체 슈퍼사이클 강세
        - 관련 종목: 시지트로닉스(+6.96%⏰)

        섹터/조선기자재
        - 데이터센터 기대감
        - 관련 종목: 동방선기(+3.28%⏰)

        코스닥/바이젠셀
        - VT-EBV-N 승인 후 차익실현 (-1.79%⏰)

    반환: [(tag_type, tag_value, content), ...]
    """
    items = []
    
    # 태그 패턴: 줄 시작에 "섹터/", "코스피/", "코스닥/", "지표/", "미증시/" 등
    # 각 태그마다 다음 태그 직전까지를 content로 잡음
    tag_pattern = re.compile(
        r'^(섹터|코스피|코스닥|지표|미증시|US_MARKET|US|미국증시)\s*/\s*([^\n\r]*)',
        re.MULTILINE | re.IGNORECASE
    )
    
    matches = list(tag_pattern.finditer(text))
    
    if not matches:
        # 태그가 하나도 없으면 AUTO로 처리
        if text.strip():
            return [("AUTO", "", text.strip())]
        return []
    
    # 첫 번째 태그 이전 내용도 AUTO로 처리할지 — 일단 무시
    
    for i, match in enumerate(matches):
        tag_korean = match.group(1).strip()
        tag_value = match.group(2).strip()
        
        # content 시작 위치: 태그 줄 다음 줄부터
        content_start = match.end()
        
        # content 끝 위치: 다음 태그 시작 직전
        if i + 1 < len(matches):
            content_end = matches[i + 1].start()
        else:
            content_end = len(text)
        
        content = text[content_start:content_end].strip()
        
        # 한국어 태그 → 영어 변환
        tag_map = {
            "섹터": "SECTOR",
            "코스피": "KOSPI",
            "코스닥": "KOSDAQ",
            "지표": "INDICATOR",
            "미증시": "US_MARKET",
            "us_market": "US_MARKET",
            "us": "US_MARKET",
            "미국증시": "US_MARKET",
        }
        tag_type = tag_map.get(tag_korean.lower(), tag_map.get(tag_korean, "AUTO"))
        
        # 지표/미증시는 tag_value가 비어있을 수 있음 (전체가 content)
        if tag_type in ("INDICATOR", "US_MARKET"):
            if tag_value:  # "지표/some text" 형식이면 tag_value도 content 일부
                content = (tag_value + "\n" + content).strip()
                tag_value = ""
        
        if content:  # 내용이 있는 것만 추가
            items.append((tag_type, tag_value, content))
    
    return items


def format_buffer_for_claude(buffer: list) -> str:
    parts = []
    us_market_lines = []
    indicator_lines = []
    sector_items = []
    auto_items = []

    for item in buffer:
        tag_type, tag_value, content = item
        if tag_type == "US_MARKET":
            us_market_lines.append(content.strip())
        elif tag_type == "INDICATOR":
            indicator_lines.append(content.strip())
        elif tag_type == "SECTOR":
            sector_items.append(f"[SECTOR: {tag_value}]\n{content}")
        elif tag_type == "AUTO":
            auto_items.append(f"[AUTO]\n{content}")

    if indicator_lines:
        parts.append(f"[INDICATOR]\n" + "\n".join(indicator_lines))
    if us_market_lines:
        parts.append(f"[US_MARKET]\n" + "\n".join(us_market_lines))
    parts.extend(sector_items)
    parts.extend(auto_items)

    return "\n\n---\n\n".join(parts)


async def build_checkpoint(buffer: list, date_str: str, prev_checkpoint: str = None) -> str:
    """체크포인트 생성. KOSPI/KOSDAQ는 코드에서 직접 처리. 섹터/지표/미증시는 Claude가 처리."""
    claude_buffer = []
    kospi_items = []
    kosdaq_items = []

    for item in buffer:
        tag_type, tag_value, content = item
        if tag_type == "KOSPI":
            kospi_items.append((tag_value, content))
        elif tag_type == "KOSDAQ":
            kosdaq_items.append((tag_value, content))
        else:
            claude_buffer.append(item)

    # Claude로 섹터/지표/미증시/AUTO 처리
    if claude_buffer or prev_checkpoint:
        structured = format_buffer_for_claude(claude_buffer)
        if prev_checkpoint:
            cp_base = re.split(r"\n📌코스피", prev_checkpoint)[0]
            user_content = (
                f"날짜: {date_str}\n\n기존 체크포인트 (📌코스피/코스닥 섹션 제외):\n{cp_base}\n\n"
                f"---\n\n추가 내용 (반영해서 업데이트해줘. 📌코스피/📌코스닥 섹션은 출력하지 말 것):\n\n{structured}"
            )
        else:
            user_content = (
                f"날짜: {date_str}\n\n{structured}\n\n"
                f"※ 📌코스피/📌코스닥 섹션은 출력하지 말 것. 지표/美증시/Sector만 출력."
                if structured.strip() else f"날짜: {date_str}"
            )

        response = client.messages.create(
            model="claude-opus-4-20250514",
            max_tokens=2500,
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
        """코스피/코스닥 종목 내용을 핵심 한두 줄로 정리"""
        seen = set()
        lines = []
        for l in content.split("\n"):
            l = l.strip()
            if not l:
                continue
            if l.startswith("http"):
                continue
            skip_keywords = ["기자 구독", "구독하기", "Forwarded from", "today at",
                             "naver.com", "hankyung.com", "글자크기", "스크랩",
                             "로그인", "회원가입"]
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

    result = base.strip()
    if kospi_block:
        result += "\n\n" + kospi_block
    if kosdaq_block:
        result += "\n\n" + kosdaq_block

    return result.strip()


async def apply_partial_edit(checkpoint: str, edit_type: str, target: str, new_content: str) -> str:
    if edit_type == "섹터":
        instruction = f"📌Sector 아래 ✔️{target} 섹션의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "코스피":
        instruction = f"📌코스피 아래 '{target}' 항목의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "코스닥":
        instruction = f"📌코스닥 아래 '{target}' 항목의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "미증시":
        instruction = f"📌美증시 마감 섹션 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "지표":
        instruction = f"📌지표 섹션 내용을 아래로 교체해줘:\n{new_content}"
    else:
        instruction = f"'{target}' 항목을 찾아서 내용을 아래로 교체해줘:\n{new_content}"

    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=2500,
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

    user_text = update.message.text
    if not user_text or not user_text.strip():
        return

    text = user_text.strip()

    # ── 1) 새 체크포인트 시작 ──
    new_session_match = re.search(
        r"(\d{1,2}/\d{1,2})\s*(체크포인트|checkpoint)\s*(생성|시작|열어|만들어)",
        text, re.IGNORECASE
    )
    if new_session_match:
        date_str = new_session_match.group(1)
        user_state[user_id] = {
            "date": date_str,
            "buffer": [],
            "last_checkpoint": None,
        }
        await update.message.reply_text(
            f"📅 {date_str} 체크포인트 새로 시작!\n\n"
            f"📥 입력 방법:\n"
            f"한 메시지에 여러 태그를 넣어도 자동 분리해드려요!\n\n"
            f"예시:\n"
            f"섹터/반도체\n"
            f"- 슈퍼사이클 기대감\n"
            f"- 관련 종목: 시지트로닉스(+6.96%⏰)\n\n"
            f"섹터/조선기자재\n"
            f"- 데이터센터 기대감\n"
            f"- 관련 종목: 동방선기(+3.28%⏰)\n\n"
            f"코스닥/바이젠셀\n"
            f"- VT-EBV-N 승인 (-1.79%⏰)\n\n"
            f"📸 이미지(지표 캡쳐) 전송도 가능!\n"
            f"✅ 정리 후 자동으로 대시보드 전송됩니다."
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
            user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None}
        user_state[user_id]["last_checkpoint"] = new_checkpoint
        user_state[user_id]["buffer"] = []
        date_match = re.search(r"(\d{1,2}/\d{1,2})", new_checkpoint)
        if date_match:
            user_state[user_id]["date"] = date_match.group(1)
        # 대시보드 자동 전송 (실패 시 상세 에러)
        date_label = user_state[user_id]["date"]
        sent, err = await send_to_dashboard(new_checkpoint, date_label)
        if sent:
            await update.message.reply_text(f"✅ 전체수정 완료! (📤 대시보드 전송 OK)\n\n" + new_checkpoint)
        else:
            await update.message.reply_text(f"✅ 전체수정 완료!\n⚠️ 대시보드 전송 실패: {err}\n\n" + new_checkpoint)
        return

    # ── 3) 부분수정 ──
    edit_match = re.match(r"^수정\s*/\s*(.+?)\s*/\s*(.+?)[\n\r](.*)", text, re.DOTALL)
    if edit_match:
        edit_type = edit_match.group(1).strip()
        target = edit_match.group(2).strip()
        new_content = edit_match.group(3).strip()
        state = user_state.get(user_id)
        if not state or not state.get("last_checkpoint"):
            await update.message.reply_text("수정할 체크포인트가 없어요!")
            return
        processing_msg = await update.message.reply_text(f"⏳ {edit_type}/{target} 수정 중...")
        try:
            result = await apply_partial_edit(state["last_checkpoint"], edit_type, target, new_content)
            user_state[user_id]["last_checkpoint"] = result
            sent, err = await send_to_dashboard(result, state["date"])
            await processing_msg.delete()
            if sent:
                await update.message.reply_text(f"✅ 수정 완료! (📤 대시보드 전송 OK)\n\n" + result)
            else:
                await update.message.reply_text(f"✅ 수정 완료!\n⚠️ 대시보드 전송 실패: {err}\n\n" + result)
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
        processing_msg = await update.message.reply_text("⏳ 통합 정리 + 대시보드 전송 중...")
        try:
            date_str = state.get("date", datetime.now().strftime("%-m/%-d"))
            result = await build_checkpoint(
                state["buffer"],
                date_str,
                prev_checkpoint=state.get("last_checkpoint")
            )
            user_state[user_id]["last_checkpoint"] = result
            user_state[user_id]["buffer"] = []
            # 자동 대시보드 전송 (상세 에러 표시)
            sent, err = await send_to_dashboard(result, date_str)
            await processing_msg.delete()
            if sent:
                await update.message.reply_text(result + "\n\n📤 대시보드 전송 OK")
            else:
                await update.message.reply_text(result + f"\n\n⚠️ 대시보드 전송 실패: {err}")
        except Exception as e:
            logger.error(f"분석 오류: {e}")
            await processing_msg.edit_text(f"❌ 오류: {str(e)[:100]}")
        return

    # ── 5) 일반 내용 → 멀티 태그 파싱 후 버퍼에 쌓기 ──
    if len(text) < 5:
        return

    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None}

    # URL 크롤링
    has_url = bool(extract_urls(text))
    if has_url:
        processing_msg = await update.message.reply_text("🔍 링크 읽는 중...")
        enriched_text = await enrich_text_with_url(text)
        await processing_msg.delete()
    else:
        enriched_text = text

    # 멀티 태그 파싱
    parsed_items = parse_multi_tag_message(enriched_text)
    
    if not parsed_items:
        return
    
    # 버퍼에 추가
    for tag_type, tag_value, content in parsed_items:
        user_state[user_id]["buffer"].append((tag_type, tag_value, content))
    
    count = len(user_state[user_id]["buffer"])
    is_append = bool(user_state[user_id].get("last_checkpoint"))
    mode = "추가" if is_append else "누적"
    
    # 파싱된 항목 요약
    summary_parts = []
    for tag_type, tag_value, _ in parsed_items:
        if tag_type == "SECTOR":
            summary_parts.append(f"✔️섹터/{tag_value}")
        elif tag_type == "KOSPI":
            summary_parts.append(f"📌코스피/{tag_value}")
        elif tag_type == "KOSDAQ":
            summary_parts.append(f"📌코스닥/{tag_value}")
        elif tag_type == "INDICATOR":
            summary_parts.append("📌지표")
        elif tag_type == "US_MARKET":
            summary_parts.append("📌美증시")
        else:
            summary_parts.append("🔍자동분류")
    
    summary = ", ".join(summary_parts)
    
    await update.message.reply_text(
        f"✅ {len(parsed_items)}개 태그 인식!\n"
        f"{summary}\n\n"
        f"({count}개 {mode}) '정리해줘' 하시면 {'업데이트' if is_append else '정리'}하고 대시보드로 자동 전송할게요!"
    )


# ── 이미지 핸들러 ─────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """이미지 → 지표(INDICATOR)로 추출"""
    user_id = update.effective_user.id
    if ALLOWED_USER_ID != 0 and user_id != ALLOWED_USER_ID:
        return

    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None}

    processing_msg = await update.message.reply_text("📸 이미지 읽는 중...")

    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)

        async with aiohttp.ClientSession() as session:
            async with session.get(file.file_path) as resp:
                image_bytes = await resp.read()

        extracted = await extract_indicators_from_image(image_bytes, "image/jpeg")

        if not extracted:
            await processing_msg.edit_text("❌ 이미지에서 지표를 읽지 못했어요.")
            return

        user_state[user_id]["buffer"].append(("INDICATOR", "", extracted))
        count = len(user_state[user_id]["buffer"])
        is_append = bool(user_state[user_id].get("last_checkpoint"))
        mode = "추가" if is_append else "누적"

        await processing_msg.delete()
        await update.message.reply_text(
            f"✅ 📌지표 저장 완료! ({count}개 {mode})\n\n"
            f"📌지표\n{extracted}\n\n"
            f"'정리해줘' 하시면 {'업데이트' if is_append else '정리'}할게요!"
        )

    except Exception as e:
        logger.error(f"이미지 처리 오류: {e}")
        await processing_msg.edit_text(f"❌ 오류: {str(e)[:100]}")


HELP_TEXT = """📋 CheckPoint Bot 명령어

📅 세션시작
3/16 체크포인트 생성

📥 내용 쌓기 (한 메시지에 여러 태그 가능!)
섹터/방산
- 美-韓 협력 기대
- 관련 종목: 한화에어로(+2.1%⏰)

섹터/반도체
- 슈퍼사이클 기대
- 관련 종목: 시지트로닉스(+6.96%⏰)

코스피/SK텔레콤
- 구글 투자 계획 보도 (+2.5%⏰)

코스닥/바이젠셀
- VT-EBV-N 승인 (-1.79%⏰)

📊 지표 입력
지표/
SOX +1.34%
VIX 23.95 -1.45%

📸 이미지 입력
지표 캡쳐 → 자동 인식

✅ 정리 (대시보드 자동 전송!)
정리해줘

✏️ 부분수정
수정/코스피/LG디스플레이
- 새내용

🔄 전체수정
전체수정
3/16 Check Point✨
...전체내용..."""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info(f"🚀 CheckPoint Bot 시작! 대시보드: {'ON' if DASHBOARD_URL else 'OFF'}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
