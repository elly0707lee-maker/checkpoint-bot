"""
Morning Broadcast CheckPoint Bot 🌅
방송 전 뉴스 → 섹터/종목 자동 분류 텔레그램 봇
"""

import logging
import os
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

# ── 메시지 버퍼 (사용자별로 모아두기) ────────────────────
message_buffer = {}

# ── Claude 프롬프트 ────────────────────────────────────────
SYSTEM_PROMPT = """너는 한국 경제방송 앵커의 방송 전 브리핑을 도와주는 전문 어시스턴트야.

사용자가 "정리해줘" 또는 "체크포인트" 라고 하면, 그동안 받은 모든 내용을 하나의 체크포인트로 통합 정리해줘.
사용자가 뉴스/기사/미증시 마감을 보내면 "✅ 받았어요. 더 보내시면 같이 정리할게요!" 라고만 답해줘.

정리 요청이 왔을 때 출력 형식:

{날짜} Check Point✨

📌美증시 마감
[마감 수치와 핵심 이슈를 2-3줄로 압축. 미증시 내용이 없으면 이 섹션 생략]

📌Sector
✔️[섹터명] ← 사용자가 섹터를 직접 언급했으면 반드시 그 섹터명 사용
- 핵심 내용 (숫자/팩트 중심)
- 핵심 내용
- 관련 종목: 종목A, 종목B, 종목C ← 기사에 언급된 종목은 여기 포함
[섹터가 여러 개면 ✔️섹터명으로 반복]

📌코스피
[종목명] ← 섹터 기사와 무관하게 별도로 언급된 코스피 종목만
- 관련 내용
[없으면 섹션 생략]

📌코스닥
[종목명] ← 섹터 기사와 무관하게 별도로 언급된 코스닥 종목만
- 관련 내용
[없으면 섹션 생략]

엄격한 규칙:
1. ** 표시 절대 사용 금지. 볼드체 없음.
2. 섹터 중분류는 반드시 ✔️ 사용
3. 기사에 언급된 종목은 해당 섹터 안 "관련 종목:" 줄에만 표시. 코스피/코스닥 칸에 중복 표시 금지.
4. 코스피/코스닥 칸은 섹터 기사와 무관하게 단독으로 언급된 종목만.
5. 사용자가 섹터를 직접 언급하면 (예: "섹터/바이오", "바이오 섹터로") 반드시 그 섹터명 그대로 사용.
6. 여러 기사를 받았어도 최종 출력은 하나의 체크포인트로 통합."""


async def analyze_news(messages: list) -> str:
    """Claude API로 누적 메시지 통합 분석"""
    today = datetime.now().strftime("%-m/%-d")
    combined = "\n\n---\n\n".join(messages)

    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"오늘 날짜: {today}\n\n지금까지 받은 내용들을 하나의 체크포인트로 통합 정리해줘:\n\n{combined}",
            }
        ],
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

    # 정리 요청 감지
    trigger_words = ["정리해줘", "정리해", "체크포인트", "checkpoint", "정리 해줘"]
    is_trigger = any(word in user_text.lower() for word in trigger_words)

    if is_trigger:
        if user_id not in message_buffer or not message_buffer[user_id]:
            await update.message.reply_text("아직 받은 내용이 없어요! 기사나 뉴스를 먼저 보내주세요 📋")
            return

        processing_msg = await update.message.reply_text("⏳ 통합 정리 중...")
        try:
            result = await analyze_news(message_buffer[user_id])
            message_buffer[user_id] = []  # 버퍼 초기화
            await processing_msg.delete()
            await update.message.reply_text(result)
        except Exception as e:
            logger.error(f"분석 오류: {e}")
            await processing_msg.edit_text(f"❌ 오류가 발생했어요: {str(e)[:100]}")
    else:
        # 내용 버퍼에 쌓기
        if len(user_text.strip()) < 10:
            await update.message.reply_text("뉴스 내용이나 미증시 마감 데이터를 붙여넣어 주세요! 📋")
            return

        if user_id not in message_buffer:
            message_buffer[user_id] = []

        message_buffer[user_id].append(user_text)
        count = len(message_buffer[user_id])
        await update.message.reply_text(f"✅ 받았어요! ({count}개 누적) 더 보내시거나 '정리해줘' 라고 하시면 한번에 정리할게요!")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🚀 CheckPoint Bot 시작!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "main__":
    main()
