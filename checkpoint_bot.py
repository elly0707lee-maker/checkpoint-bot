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
# { user_id: { "date": "3/13", "buffer": [...], "last_checkpoint": "..." } }
user_state = {}

# ── Claude 프롬프트 ────────────────────────────────────────
SYSTEM_PROMPT = """너는 한국 경제방송 앵커의 방송 전 브리핑을 도와주는 전문 어시스턴트야.

출력 형식:

{날짜} Check Point✨

📌美증시 마감
[마감 수치와 핵심 이슈를 2-3줄로 압축. 미증시 내용이 없으면 이 섹션 생략]

📌Sector
✔️[섹터명]
- 핵심 내용 (숫자/팩트 중심)
- 핵심 내용
- 관련 종목: 종목A, 종목B, 종목C
[섹터가 여러 개면 ✔️섹터명으로 반복]

📌코스피
[종목명]
- 관련 내용
[없으면 섹션 생략]

📌코스닥
[종목명]
- 관련 내용
[없으면 섹션 생략]

엄격한 규칙:
1. ** 표시 절대 사용 금지. 볼드체 없음.
2. 섹터 중분류는 반드시 ✔️ 사용
3. 기사에 언급된 종목은 해당 섹터 안 "관련 종목:" 줄에만 표시. 코스피/코스닥 칸에 중복 표시 금지.
4. 코스피/코스닥 칸은 섹터 기사와 무관하게 단독으로 언급된 종목만.
5. 사용자가 섹터를 직접 언급하면 (예: "섹터/바이오", "바이오 섹터로") 반드시 그 섹터명 그대로 사용.
6. 여러 기사를 받았어도 최종 출력은 하나의 체크포인트로 통합."""


async def build_checkpoint(messages: list, date_str: str, prev_checkpoint: str = None) -> str:
    """Claude API로 체크포인트 생성 (이전 체크포인트가 있으면 덧붙이기)"""
    combined = "\n\n---\n\n".join(messages)

    if prev_checkpoint:
        user_content = (
            f"날짜: {date_str}\n\n"
            f"아래는 이미 정리된 체크포인트야:\n\n{prev_checkpoint}\n\n"
            f"---\n\n여기에 추가로 들어온 내용들을 반영해서 체크포인트를 업데이트해줘. "
            f"기존 내용은 유지하고 새 내용을 적절히 합쳐줘:\n\n{combined}"
        )
    else:
        user_content = (
            f"날짜: {date_str}\n\n"
            f"아래 내용들을 하나의 체크포인트로 통합 정리해줘:\n\n{combined}"
        )

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

    # ── 1) 새 체크포인트 시작 감지: "3/13 체크포인트 생성" 패턴 ──
    new_session_match = re.search(
        r"(\d{1,2}/\d{1,2})\s*(체크포인트|checkpoint)\s*(생성|시작|열어|만들어)",
        text, re.IGNORECASE
    )
    if new_session_match:
        date_str = new_session_match.group(1)
        user_state[user_id] = {"date": date_str, "buffer": [], "last_checkpoint": None}
        await update.message.reply_text(
            f"📅 {date_str} 체크포인트 새로 시작할게요!\n"
            f"기사나 뉴스 보내주시면 모아뒀다가 '정리해줘' 하시면 정리해드려요 ✅"
        )
        return

    # ── 2) 정리 요청 감지 ──
    trigger_words = ["정리해줘", "정리해", "정리 해줘", "체크포인트 만들어", "뽑아줘"]
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
            await processing_msg.edit_text(f"❌ 오류가 발생했어요: {str(e)[:100]}")
        return

    # ── 3) 일반 내용 → 버퍼에 쌓기 ──
    if len(text) < 10:
        await update.message.reply_text("뉴스 내용이나 미증시 마감 데이터를 붙여넣어 주세요! 📋")
        return

    if user_id not in user_state:
        today = datetime.now().strftime("%-m/%-d")
        user_state[user_id] = {"date": today, "buffer": [], "last_checkpoint": None}

    user_state[user_id]["buffer"].append(text)
    count = len(user_state[user_id]["buffer"])

    if user_state[user_id].get("last_checkpoint"):
        await update.message.reply_text(
            f"✅ 받았어요! ({count}개 추가) 이전 체크포인트에 덧붙여서 업데이트할게요. '정리해줘' 하시면 반영해드려요!"
        )
    else:
        await update.message.reply_text(
            f"✅ 받았어요! ({count}개 누적) 더 보내시거나 '정리해줘' 하시면 한번에 정리할게요!"
        )


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🚀 CheckPoint Bot 시작!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
