"""
Morning Broadcast CheckPoint Bot 🌅
방송 전 뉴스 → 섹터/종목 자동 분류 텔레그램 봇
"""

import logging
import os
import re
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, BotCommand
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
from datetime import datetime

# ── 설정 ──────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── URL 크롤링 (실패하면 None 반환) ──────────────────────
async def fetch_url_text(url: str) -> str | None:
    """URL 크롤링 시도. 실패하면 None 반환."""
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

                # 본문 추출 시도 (주요 뉴스 사이트 공통 선택자)
                for selector in ["article", ".article-body", ".article_body", "#articleBody",
                                  ".news-content", ".content-article", "main"]:
                    el = soup.select_one(selector)
                    if el:
                        text = el.get_text(separator="\n", strip=True)
                        if len(text) > 100:
                            return text[:2000]  # 너무 길면 앞부분만

                # 그것도 없으면 og:description / 타이틀만
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
    """텍스트에서 URL 추출"""
    return re.findall(r'https?://[^\s]+', text)


async def enrich_text_with_url(text: str) -> str:
    """텍스트 안의 URL을 크롤링해서 내용 보강. 실패하면 원본 텍스트 그대로."""
    urls = extract_urls(text)
    if not urls:
        return text

    enriched = text
    for url in urls:
        fetched = await fetch_url_text(url)
        if fetched:
            # URL 자리에 크롤링된 내용 추가
            enriched = enriched.replace(url, f"{url}\n[기사내용]\n{fetched}")
            logger.info(f"크롤링 성공: {url}")
        else:
            logger.info(f"크롤링 실패, 원문 텍스트 사용: {url}")
    return enriched

# ── 사용자별 상태 저장 ────────────────────────────────────
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
- AUTO: 태그 없이 들어온 일반 기사 (섹터를 네가 판단해서 분류)

규칙:
1. SECTOR 태그가 있으면 → 반드시 그 섹터명 그대로 📌Sector 아래 ✔️섹터명 으로 표시
2. KOSPI 태그가 있으면 → 반드시 📌코스피 칸에만 표시. 섹터 칸에 절대 넣지 말 것. 기사 내용이 아무리 풍부해도 섹터 생성 금지.
3. KOSDAQ 태그가 있으면 → 반드시 📌코스닥 칸에만 표시. 섹터 칸에 절대 넣지 말 것. 기사 내용이 아무리 풍부해도 섹터 생성 금지.
4. US_MARKET 태그가 있으면 → 📌美증시 마감 칸에 표시
5. US_MARKET 태그가 하나도 없으면 → 📌美증시 마감 섹션 절대 생성하지 말 것.
6. AUTO 태그 내용은 네가 섹터 판단해서 분류
7. ** 볼드 표시 절대 금지
8. 섹터 중분류는 ✔️ 사용
9. 기사에 언급된 종목은 해당 섹터 안 "관련 종목:" 줄에만. 코스피/코스닥 칸에 중복 금지.
10. 최종 출력은 하나의 체크포인트로 통합
11. KOSPI/KOSDAQ 태그로 들어온 내용은 절대로 섹터로 승격하거나 섹터를 추가로 만들지 말 것.

출력 형식:

{날짜} Check Point✨

📌美증시 마감
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


def parse_user_tag(text: str):
    """사용자 태그 추출"""
    sector_match = re.match(r"^섹터\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if sector_match:
        return "SECTOR", sector_match.group(1).strip(), text[sector_match.end():].strip()

    kospi_match = re.match(r"^코스피\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if kospi_match:
        return "KOSPI", kospi_match.group(1).strip(), text[kospi_match.end():].strip()

    kosdaq_match = re.match(r"^코스닥\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if kosdaq_match:
        return "KOSDAQ", kosdaq_match.group(1).strip(), text[kosdaq_match.end():].strip()

    us_keywords = ["다우", "나스닥", "s&p", "S&P", "미증시", "美증시", "뉴욕증시", "월스트리트"]
    if any(kw in text for kw in us_keywords) and len(text) < 500:
        return "US_MARKET", "", text

    return "AUTO", "", text


def format_buffer_for_claude(buffer: list) -> str:
    parts = []
    for item in buffer:
        tag_type, tag_value, content = item
        if tag_type == "SECTOR":
            parts.append(f"[SECTOR: {tag_value}]\n{content}")
        elif tag_type == "KOSPI":
            parts.append(f"[KOSPI: {tag_value}]\n{content}")
        elif tag_type == "KOSDAQ":
            parts.append(f"[KOSDAQ: {tag_value}]\n{content}")
        elif tag_type == "US_MARKET":
            parts.append(f"[US_MARKET]\n{content}")
        else:
            parts.append(f"[AUTO]\n{content}")
    return "\n\n---\n\n".join(parts)


async def build_checkpoint(buffer: list, date_str: str, prev_checkpoint: str = None) -> str:
    structured = format_buffer_for_claude(buffer)
    if prev_checkpoint:
        user_content = (
            f"날짜: {date_str}\n\n기존 체크포인트:\n{prev_checkpoint}\n\n"
            f"---\n\n추가 내용 (반영해서 업데이트해줘):\n\n{structured}"
        )
    else:
        user_content = f"날짜: {date_str}\n\n{structured}"

    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return response.content[0].text


async def apply_partial_edit(checkpoint: str, edit_type: str, target: str, new_content: str) -> str:
    """부분수정: Claude에게 특정 항목만 수정 요청"""
    if edit_type == "섹터":
        instruction = f"📌Sector 아래 ✔️{target} 섹션의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "코스피":
        instruction = f"📌코스피 아래 '{target}' 항목의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "코스닥":
        instruction = f"📌코스닥 아래 '{target}' 항목의 내용을 아래로 교체해줘:\n{new_content}"
    elif edit_type == "미증시":
        instruction = f"📌美증시 마감 섹션 내용을 아래로 교체해줘:\n{new_content}"
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
        user_state[user_id] = {"date": date_str, "buffer": [], "last_checkpoint": None}
        await update.message.reply_text(
            f"📅 {date_str} 체크포인트 새로 시작!\n"
            f"태그 예시:\n"
            f"섹터/폴더블 + 기사내용\n"
            f"코스닥/아크릴 + 기사내용\n"
            f"수정/코스피/LG디스플레이 + 수정내용\n"
            f"전체수정 + 체크포인트 전문"
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

        # 날짜 자동 추출
        date_match = re.search(r"(\d{1,2}/\d{1,2})", new_checkpoint)
        if date_match:
            user_state[user_id]["date"] = date_match.group(1)

        await update.message.reply_text(
            "✅ 전체수정 완료! 이 내용을 베이스로 추가 기사 쌓을게요.\n\n" + new_checkpoint
        )
        return

    # ── 3) 부분수정: "수정/코스피/LG디스플레이\n내용" ──
    edit_match = re.match(r"^수정\s*/\s*(.+?)\s*/\s*(.+?)[\n\r](.*)", text, re.DOTALL)
    if edit_match:
        edit_type = edit_match.group(1).strip()   # 코스피, 코스닥, 섹터, 미증시
        target = edit_match.group(2).strip()       # 종목명 또는 섹터명
        new_content = edit_match.group(3).strip()  # 새 내용

        state = user_state.get(user_id)
        if not state or not state.get("last_checkpoint"):
            await update.message.reply_text("수정할 체크포인트가 없어요! 먼저 체크포인트를 만들어 주세요.")
            return

        processing_msg = await update.message.reply_text(f"⏳ {edit_type}/{target} 수정 중...")
        try:
            result = await apply_partial_edit(
                state["last_checkpoint"], edit_type, target, new_content
            )
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
            await update.message.reply_text(result)
        except Exception as e:
            logger.error(f"분석 오류: {e}")
            await processing_msg.edit_text(f"❌ 오류: {str(e)[:100]}")
        return

    # ── 5) 일반 내용 → 버퍼에 쌓기 ──
    if len(text) < 5:
        return

    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None}

    # URL이 포함된 경우 크롤링 시도
    has_url = bool(extract_urls(text))
    if has_url:
        processing_msg = await update.message.reply_text("🔍 링크 읽는 중...")
        enriched_text = await enrich_text_with_url(text)
        await processing_msg.delete()
    else:
        enriched_text = text

    tag_type, tag_value, content = parse_user_tag(enriched_text)
    user_state[user_id]["buffer"].append((tag_type, tag_value, content))
    count = len(user_state[user_id]["buffer"])

    tag_display = {
        "SECTOR": f"✔️섹터/{tag_value}",
        "KOSPI": f"📌코스피/{tag_value}",
        "KOSDAQ": f"📌코스닥/{tag_value}",
        "US_MARKET": "📌美증시 마감",
        "AUTO": "🔍자동분류",
    }
    label = tag_display.get(tag_type, "")
    is_append = bool(user_state[user_id].get("last_checkpoint"))
    mode = "추가" if is_append else "누적"

    await update.message.reply_text(
        f"✅ {label} ({count}개 {mode}) '정리해줘' 하시면 {'업데이트' if is_append else '정리'}할게요!"
    )


HELP_TEXT = """📋 CheckPoint Bot 명령어

📅 세션시작
3/16 체크포인트 생성

📥 내용 쌓기
섹터/전력설비 + 기사내용
코스피/삼성전자 + 기사내용
코스닥/아크릴 + 기사내용
태그 없이 붙여넣기 → 자동분류
다우/나스닥 포함 텍스트 → 美증시 마감

✅ 정리
정리해줘

✏️ 부분수정
수정/코스피/LG디스플레이
- 새내용

🔄 전체수정
전체수정
3/16 Check Point✨
📌美증시 마감
...전체내용...

💡 부분수정·전체수정 후에도
수정본이 베이스가 되어 계속 쌓임"""


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🚀 CheckPoint Bot 시작!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
