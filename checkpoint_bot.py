"""
Morning Broadcast CheckPoint Bot 🌅
방송 전 뉴스 → 섹터/종목 자동 분류 텔레그램 봇
"""

import logging
import os
import re
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
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
2. KOSPI 태그가 있으면 → 반드시 📌코스피 칸에만 표시. 섹터 칸에 넣지 말 것.
3. KOSDAQ 태그가 있으면 → 반드시 📌코스닥 칸에만 표시. 섹터 칸에 넣지 말 것.
4. US_MARKET 태그가 있으면 → 📌美증시 마감 칸에 표시
5. US_MARKET 태그가 하나도 없으면 → 📌美증시 마감 섹션 절대 생성하지 말 것. 기사 내용에서 미증시 정보를 추론해서 넣지 말 것.
6. AUTO 태그 내용은 네가 섹터 판단해서 분류
7. ** 볼드 표시 절대 금지
8. 섹터 중분류는 ✔️ 사용
9. 기사에 언급된 종목은 해당 섹터 안 "관련 종목:" 줄에만. 코스피/코스닥 칸에 중복 금지.
10. 최종 출력은 하나의 체크포인트로 통합

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


def parse_user_tag(text: str):
    """
    사용자가 보낸 메시지에서 태그 추출
    반환: (tag_type, tag_value, content)
    tag_type: "SECTOR" | "KOSPI" | "KOSDAQ" | "US_MARKET" | "AUTO"
    """
    # 섹터 태그: "섹터/2차전지" 또는 "섹터 / 2차전지"
    sector_match = re.match(r"^섹터\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if sector_match:
        tag_value = sector_match.group(1).strip()
        content = text[sector_match.end():].strip()
        return "SECTOR", tag_value, content

    # 코스피 태그: "코스피/종목명"
    kospi_match = re.match(r"^코스피\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if kospi_match:
        tag_value = kospi_match.group(1).strip()
        content = text[kospi_match.end():].strip()
        return "KOSPI", tag_value, content

    # 코스닥 태그: "코스닥/종목명"
    kosdaq_match = re.match(r"^코스닥\s*/\s*(.+?)[\n\r]", text + "\n", re.IGNORECASE)
    if kosdaq_match:
        tag_value = kosdaq_match.group(1).strip()
        content = text[kosdaq_match.end():].strip()
        return "KOSDAQ", tag_value, content

    # 미증시 마감 태그: "미증시" 또는 "美증시" 로 시작하거나 지수 수치가 포함된 짧은 텍스트
    us_keywords = ["다우", "나스닥", "s&p", "S&P", "미증시", "美증시", "뉴욕증시", "월스트리트"]
    if any(kw in text for kw in us_keywords) and len(text) < 500:
        return "US_MARKET", "", text

    return "AUTO", "", text


def format_buffer_for_claude(buffer: list) -> str:
    """버퍼를 Claude에게 보낼 구조화된 텍스트로 변환"""
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
    """Claude API로 체크포인트 생성"""
    structured = format_buffer_for_claude(buffer)

    if prev_checkpoint:
        user_content = (
            f"날짜: {date_str}\n\n"
            f"기존 체크포인트:\n{prev_checkpoint}\n\n"
            f"---\n\n추가 내용 (아래를 반영해서 업데이트해줘):\n\n{structured}"
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
            f"기사 보낼 때 앞에 태그 붙여주시면 정확하게 분류할게요 ✅\n\n"
            f"태그 예시:\n"
            f"섹터/폴더블 → 기사내용\n"
            f"코스닥/아크릴 → 기사내용\n"
            f"코스피/삼성전자 → 기사내용\n"
            f"(태그 없이 보내도 자동 분류해요)"
        )
        return

    # ── 2) 정리 요청 ──
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

    # ── 3) 일반 내용 → 태그 파싱 후 버퍼에 쌓기 ──
    if len(text) < 5:
        return

    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None}

    tag_type, tag_value, content = parse_user_tag(text)
    user_state[user_id]["buffer"].append((tag_type, tag_value, content))
    count = len(user_state[user_id]["buffer"])

    # 태그 확인 메시지
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


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🚀 CheckPoint Bot 시작!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
