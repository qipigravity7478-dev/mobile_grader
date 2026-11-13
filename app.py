import streamlit as st
import os
import json
import base64
import csv
import requests
from datetime import datetime
from collections import Counter
import fitz  # PyMuPDF
from openai import OpenAI
from pydantic import BaseModel, Field
from typing import List

# ==========================================
# 1. 초기 설정 및 API 키 연동 (Streamlit Secrets 사용)
# ==========================================
# 클라우드 환경에서는 보안상 st.secrets를 통해 환경변수를 불러옵니다.
API_KEY = st.secrets.get("OPENAI_API_KEY", "")
TELEGRAM_BOT_TOKEN = st.secrets.get("TELEGRAM_BOT_TOKEN", "")

if not API_KEY:
    st.error("OpenAI API 키가 설정되지 않았습니다. 클라우드 시크릿 설정을 확인해주세요.")
    st.stop()

client = OpenAI(api_key=API_KEY)
KEY_DIR = "answer_keys"
os.makedirs(KEY_DIR, exist_ok=True)

# Pydantic 모델 정의
class GradedItem(BaseModel):
    item_number: int
    question_prompt: str
    correct_answer: str
    student_answer: str
    is_correct: bool
    confidence: float

class ExamGradingResult(BaseModel):
    student_name: str = Field(description="시험지 상단에 적힌 학생의 손글씨 이름")
    total_questions: int
    correct_count: int
    score_percentage: float
    items: List[GradedItem]

class BlankQuiz(BaseModel):
    target_word: str
    sentence_with_blank: str
    sentence_translation: str
    correct_word: str

class MultipleChoiceQuiz(BaseModel):
    target_word: str
    question: str
    options: List[str]
    correct_index: int

class ReviewQuizSet(BaseModel):
    blank_quizzes: List[BlankQuiz]
    multiple_choice_quizzes: List[MultipleChoiceQuiz]

# ==========================================
# 2. 공통 함수 (PDF 생성, 텔레그램 발송)
# ==========================================
def generate_pdf(student_name, academy_name, final_text):
    output_file = f"{student_name}_오답복습지.pdf"
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    col_starts, col_width, y_pos, current_col = [40, 310], 245, 85, 0
    
    logo_path, watermark_path = "logo.png", "watermark.png"
    if os.path.exists(watermark_path):
        page.insert_image(fitz.Rect(147, 271, 447, 571), filename=watermark_path)
        
    text_start_x = 40
    if os.path.exists(logo_path):
        page.insert_image(fitz.Rect(40, 35, 75, 70), filename=logo_path)
        text_start_x = 85
    
    page.insert_text((text_start_x, 55), academy_name, fontname="cjk", fontsize=11, color=(0.2, 0.2, 0.2))
    page.insert_text((220, 55), "📝 오답 복습 시험지", fontname="cjk", fontsize=16)
    page.insert_text((430, 45), datetime.now().strftime("%Y. %m. %d."), fontname="cjk", fontsize=10)
    page.insert_text((430, 60), f"이름: {student_name}", fontname="cjk", fontsize=10)
    page.draw_line((40, 70), (555, 70), color=(0,0,0), width=1)

    def add_block(text, height, fontsize=11, color=(0,0,0)):
        nonlocal y_pos, current_col, page, doc
        if y_pos + height > 800:
            if current_col == 0: current_col, y_pos = 1, 85
            else:
                page = doc.new_page(width=595, height=842)
                if os.path.exists(watermark_path): page.insert_image(fitz.Rect(147, 271, 447, 571), filename=watermark_path)
                page.draw_line((40, 40), (555, 40), color=(0,0,0), width=1)
                current_col, y_pos = 0, 60
        rect = fitz.Rect(col_starts[current_col], y_pos, col_starts[current_col] + col_width, y_pos + height)
        page.insert_textbox(rect, text, fontname="cjk", fontsize=fontsize, color=color)
        y_pos += height

    for block in final_text.split("\n\n"):
        if not block.strip(): continue
        line_count = len(block.strip().split('\n'))
        add_block(block.strip(), line_count * 15 + 10, 12 if block.startswith("[") else 11, (0,0,1) if block.startswith("[") else (0,0,0))
        add_block("\n", 15)
    doc.save(output_file); doc.close()
    return output_file

def send_telegram(chat_id, text, pdf_path):
    if not TELEGRAM_BOT_TOKEN: return False
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id": chat_id, "text": text})
        if pdf_path and os.path.exists(pdf_path):
            with open(pdf_path, 'rb') as f:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument", data={"chat_id": chat_id}, files={"document": f})
        return True
    except: return False

# ==========================================
# 3. 화면 UI 구성 (모바일 최적화 탭)
# ==========================================
st.set_page_config(page_title="모바일 단어 채점기", page_icon="📝", layout="centered")
st.title("📱 단어 시험지 자동 채점 앱")

tab_grade, tab_key, tab_addr, tab_stat = st.tabs(["🚀 채점", "⚙️ 정답지", "📞 주소록", "📈 통계"])

# --- [탭 1] 채점 및 결과 ---
with tab_grade:
    academy_name = st.text_input("학원/학교명", value="푸른 어학원")
    
    # 저장된 정답지 목록 불러오기
    key_files = [f for f in os.listdir(KEY_DIR) if f.endswith(".json")]
    selected_key_file = st.selectbox("📖 적용할 정답지 선택", key_files) if key_files else None
    
    if not key_files:
        st.warning("'⚙️ 정답지' 탭에서 먼저 정답지를 생성해주세요.")
    else:
        # 스마트폰에서는 파일 업로드 버튼을 누르면 카메라/갤러리 선택창이 뜹니다.
        uploaded_files = st.file_uploader("📸 시험지 사진 촬영 또는 선택 (여러 장 가능)", type=['jpg', 'jpeg', 'png'], accept_multiple_files=True)
        
        if uploaded_files and st.button("🚀 채점 및 자동 발송 시작", use_container_width=True):
            # 주소록 불러오기
            address_book = {}
            if os.path.exists("address_book.csv"):
                with open("address_book.csv", "r", encoding="utf-8-sig") as f:
                    for row in csv.reader(f):
                        if len(row) >= 2: address_book[row[0].strip()] = row[1].strip()

            # 선택한 정답지 불러오기
            with open(os.path.join(KEY_DIR, selected_key_file), "r", encoding="utf-8") as f:
                answer_key = json.load(f)
            answer_text = "\n".join([f"Q{k}: {v}" for k, v in sorted(answer_key.items(), key=lambda x: int(x[0]))])
            
            progress_bar = st.progress(0)
            status_text = st.empty()
            
            for idx, file in enumerate(uploaded_files, 1):
                original_filename = file.name
                status_text.info(f"[{idx}/{len(uploaded_files)}] {original_filename} 분석 중...")
                
                try:
                    base64_image = base64.b64encode(file.read()).decode("utf-8")
                    system_prompt = "You are a grading assistant. 1. Extract student's handwritten name. 2. Grade answers. Strict spelling."
                    
                    grade_resp = client.beta.chat.completions.parse(
                        model="gpt-4o-2024-08-06",
                        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": [{"type": "text", "text": f"Answer Key:\n{answer_text}\n\nGrade this."}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}]}],
                        response_format=ExamGradingResult, temperature=0.1
                    )
                    
                    result = grade_resp.choices[0].message.parsed
                    student_name = result.student_name.strip()
                    if not student_name or "미상" in student_name: student_name = f"이름미상_{idx}"
                    
                    wrong_items = [i for i in result.items if not i.is_correct]
                    
                    # CSV 기록
                    with open("grading_history.csv", 'a', newline='', encoding='utf-8-sig') as f:
                        w = csv.writer(f)
                        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), student_name, result.total_questions, result.correct_count, result.score_percentage, ", ".join([i.question_prompt for i in wrong_items])])
                    
                    output_pdf = None
                    if wrong_items:
                        status_text.warning(f"[{student_name}] 오답 퀴즈 및 PDF 생성 중...")
                        words_info = "\n".join([f"- {i.question_prompt}: {i.correct_answer}" for i in wrong_items])
                        quiz_resp = client.beta.chat.completions.parse(
                            model="gpt-4o-2024-08-06",
                            messages=[{"role": "system", "content": "Create 1 fill-in-the-blank and 1 multiple-choice quiz for each missed word with Korean translations."}, {"role": "user", "content": words_info}],
                            response_format=ReviewQuizSet, temperature=0.3
                        )
                        quizzes = quiz_resp.choices[0].message.parsed
                        draft_text = "[ Part 1. 문맥 맞춤 빈칸 채우기 ]\n\n"
                        for i, q in enumerate(quizzes.blank_quizzes, 1): draft_text += f"{i}. {q.sentence_with_blank}\n(해석: {q.sentence_translation})\n정답: {q.correct_word}\n\n"
                        draft_text += "[ Part 2. 객관식 퀴즈 ]\n\n"
                        for i, q in enumerate(quizzes.multiple_choice_quizzes, 1): draft_text += f"{i}. {q.question}\n" + "".join([f"  {'①②③④'[opt_idx]} {opt}\n" for opt_idx, opt in enumerate(q.options)]) + "\n"
                        
                        output_pdf = generate_pdf(student_name, academy_name, draft_text)

                    # 텔레그램 발송
                    if student_name in address_book and TELEGRAM_BOT_TOKEN:
                        status_text.info(f"[{student_name}] 텔레그램 알림 발송 중...")
                        msg = f"[{academy_name}] {student_name} 학생 점수: {result.score_percentage}점\n" + ("오답 복습지를 첨부합니다." if output_pdf else "만점입니다! 🎉")
                        send_telegram(address_book[student_name], msg, output_pdf)
                    
                    st.success(f"✅ {student_name} 처리 완료 ({result.score_percentage}점)")
                    
                    # 브라우저에서 다운로드 할 수 있도록 버튼 제공
                    if output_pdf:
                        with open(output_pdf, "rb") as pdf_file:
                            st.download_button(label=f"📄 {student_name} 복습지 다운로드", data=pdf_file, file_name=output_pdf, mime="application/pdf", key=f"dl_{idx}")

                except Exception as e:
                    st.error(f"❌ {original_filename} 실패: {e}")
                
                progress_bar.progress(idx / len(uploaded_files))
            
            status_text.success("🎉 모든 채점 및 발송 작업이 완료되었습니다!")

# --- [탭 2] 정답지 관리 ---
with tab_key:
    st.subheader("새 정답지 만들기")
    new_key_name = st.text_input("정답지 이름 (예: 고1A반_월요일)")
    raw_keys = st.text_area("정답 입력 (엔터로 구분, 예: 1번정답 엔터 2번정답)", height=150)
    
    if st.button("💾 정답지 저장하기", use_container_width=True):
        if new_key_name and raw_keys:
            key_dict = {str(i+1): ans.strip() for i, ans in enumerate(raw_keys.split("\n")) if ans.strip()}
            with open(os.path.join(KEY_DIR, f"{new_key_name}.json"), "w", encoding="utf-8") as f:
                json.dump(key_dict, f, ensure_ascii=False, indent=2)
            st.success(f"[{new_key_name}] 저장 완료! 화면을 새로고침 해주세요.")

# --- [탭 3] 주소록 관리 ---
with tab_addr:
    st.subheader("학부모 연락처 (텔레그램)")
    addr_name = st.text_input("학생 이름")
    addr_chat_id = st.text_input("학부모 텔레그램 채팅 ID (숫자)")
    
    if st.button("➕ 주소록에 추가", use_container_width=True):
        if addr_name and addr_chat_id:
            file_exists = os.path.exists("address_book.csv")
            with open("address_book.csv", "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if not file_exists: w.writerow(["학생이름", "학부모_텔레그램_채팅ID"])
                w.writerow([addr_name.strip(), addr_chat_id.strip()])
            st.success(f"{addr_name} 학생 연락처 추가 완료!")

# --- [탭 4] 오답 통계 ---
with tab_stat:
    st.subheader("📈 자주 틀리는 단어 순위")
    if os.path.exists("grading_history.csv"):
        counter = Counter()
        with open("grading_history.csv", 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if len(row) >= 6 and row[5].strip(): counter.update([w.strip() for w in row[5].split(",")])
        
        if counter:
            for i, (w, c) in enumerate(counter.most_common(10), 1):
                st.write(f"**{i}위** 🚨 {w} ({c}회)")
        else:
            st.info("틀린 단어 기록이 없습니다.")
    else:
        st.info("아직 채점 기록이 없습니다.")