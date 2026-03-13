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
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "여기에_텔레그램_봇_토큰")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "여기에_ANTHROPIC_API_KEY")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))  # 예니 본인 텔레그램 ID

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Claude 프롬프트 ────────────────────────────────────────
SYSTEM_PROMPT = """너는 한국 경제방송 앵커의 방송 전 브리핑을 도와주는 전문 어시스턴트야.

사용자가 뉴스 기사, 링크 내용, 요약 텍스트, 또는 미국 증시 마감 데이터를 보내면
아래 형식으로 즉시 정리해줘.

**판단 규칙:**
1. 미국 증시 지수(다우, 나스닥, S&P500), 마감 수치가 포함된 내용 → 📌美증시 마감 섹션
2. 뉴스/기사/이슈 → 영향받는 섹터 + 코스피/코스닥 관련 종목 분류
3. 둘 다 섞인 경우 → 모두 반영

**출력 형식 (해당 항목만 표시, 없으면 생략):**

{날짜} Check Point✨

📌美증시 마감
[마감 수치와 핵심 이슈를 2-3줄로 압축. 없으면 이 섹션 생략]

📌Sector
[영향받는 섹터명]
- 핵심 내용 1 (숫자/팩트 중심)
- 핵심 내용 2
[섹터가 여러 개면 섹터별로 반복]

📌코스피
[종목명 (업종)]
- 관련 내용 (왜 영향받는지 한 줄)
[종목이 여러 개면 종목별로 반복. 코스피 관련 종목 없으면 생략]

📌코스닥
[종목명 (업종)]
- 관련 내용 (왜 영향받는지 한 줄)
[종목이 여러 개면 종목별로 반복. 코스닥 관련 종목 없으면 생략]

**스타일 규칙:**
- 팩트와 수치 중심, 군더더기 없이
- 앵커가 방송 전 30초 안에 훑고 머릿속에 넣을 수 있는 밀도
- 종목은 반드시 코스피/코스닥 구분 (애매하면 "코스피 추정" 등으로 표시)
- 섹터는 반도체, AI/데이터센터, 바이오, 2차전지, 방산, 조선, 금융, 에너지, 소비재 등 테마 기준
- 날짜는 오늘 날짜 자동 적용"""


async def analyze_news(text: str) -> str:
    """Claude API로 뉴스 분석"""
    today = datetime.now().strftime("%-m/%-d")

    response = client.messages.create(
        model="claude-opus-4-20250514",
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"오늘 날짜: {today}\n\n아래 내용을 정리해줘:\n\n{text}",
            }
        ],
    )
    return response.content[0].text


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """텔레그램 메시지 처리"""
    user_id = update.effective_user.id

    # 허용된 사용자만 (0이면 전체 허용)
    if ALLOWED_USER_ID != 0 and user_id != ALLOWED_USER_ID:
        await update.message.reply_text("접근 권한이 없습니다.")
        return

    user_text = update.message.text
    if not user_text or not user_text.strip():
        return

    # 짧은 명령어는 무시
    if len(user_text.strip()) < 10:
        await update.message.reply_text(
            "뉴스 내용이나 미증시 마감 데이터를 붙여넣어 주세요! 📋"
        )
        return

    # 처리 중 표시
    processing_msg = await update.message.reply_text("⏳ 분석 중...")

    try:
        result = await analyze_news(user_text)
        await processing_msg.delete()
        await update.message.reply_text(result, parse_mode=None)
    except Exception as e:
        logger.error(f"분석 오류: {e}")
        await processing_msg.edit_text(f"❌ 오류가 발생했어요: {str(e)[:100]}")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """URL이 포함된 메시지 처리 (텍스트와 동일하게)"""
    await handle_message(update, context)


def main():
    """봇 실행"""
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # 텍스트 메시지 핸들러
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🚀 CheckPoint Bot 시작!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
