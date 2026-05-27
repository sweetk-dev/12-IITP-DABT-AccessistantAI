import os
import glob
import json
import re
import hashlib
import logging
from time import sleep
import psycopg2
from psycopg2.extras import Json
from pgvector.psycopg2 import register_vector
import requests
from dotenv import load_dotenv

# =====================================================================
# 1. 환경 설정 및 로깅
# =====================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# 상위 폴더(welfare_backend/) 의 .env 를 단일 진입점으로 사용
from pathlib import Path as _Path
load_dotenv(_Path(__file__).resolve().parent.parent / ".env")
DB_NAME = os.environ.get("DB_NAME", "welfare_db")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "")
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "5432")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# =====================================================================
# 2. 유틸리티 함수
# =====================================================================
def calculate_file_hash(file_path):
    """파일 내용의 MD5 해시값을 계산하여 반환합니다 (32글자)."""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as afile:
        buf = afile.read()
        hasher.update(buf)
    return hasher.hexdigest()

def parse_age_criteria(age_text):
    if not age_text: return None, None
    age_min, age_max = None, None
    m = re.search(r'만\s*(\d+)\s*세\s*[~∼\-]\s*(?:만\s*)?(\d+)\s*세', age_text)
    if m: return int(m.group(1)), int(m.group(2))
    m = re.search(r'만\s*(\d+)\s*세\s*(이상|초과)', age_text)
    if m: age_min = int(m.group(1))
    m = re.search(r'만\s*(\d+)\s*세\s*(미만|이하)', age_text)
    if m: age_max = int(m.group(1))
    return age_min, age_max

def make_chunk_content(policy_id, title, section_name, data_content):
    header = f"[{policy_id} {title} — {section_name}]\n"
    if isinstance(data_content, (dict, list)):
        body = json.dumps(data_content, ensure_ascii=False, indent=2)
    else:
        body = str(data_content)
    return header + body

def extract_chunks(data):
    pid = data.get("id")
    ptitle = data.get("title")
    chunks = []
    
    chunks.append(("summary", None, "요약 및 지원규모", {"요약": data.get("short_summary"), "지원금액_비율": data.get("supported_amount")}, {}))
    chunks.append(("eligibility", None, "지원 대상 및 자격요건", data.get("eligibility"), {}))
    chunks.append(("how_to_use", None, "이용 및 혜택 적용 방법", data.get("how_to_use"), {}))
    chunks.append(("application", None, "신청 방법 및 필요 서류", data.get("application"), {}))
    for i, faq in enumerate(data.get("faq", [])):
        chunks.append(("faq", f"faq_q{i+1}", "자주 묻는 질문(FAQ)", f"질문: {faq.get('q')}\n답변: {faq.get('a')}", {}))
    chunks.append(("exceptions", None, "예외 사항 및 주의점", data.get("exceptions_and_caveats"), {}))
    chunks.append(("legal_basis", None, "법적 근거", data.get("legal_basis"), {}))
    for i, agency in enumerate(data.get("operating_agencies", [])):
        chunks.append(("agency_specific", f"agency_{i}", f"{agency.get('region')} {agency.get('agency')} 세부 운영", agency, {"region": agency.get("region"), "agency": agency.get("agency")}))
    chunks.append(("validity", None, "유효기간 및 갱신", data.get("validity"), {}))
    chunks.append(("penalties", None, "부정사용 제재 및 벌칙", data.get("penalties_for_misuse"), {}))
    chunks.append(("contact", None, "문의처 및 콜센터", data.get("contact"), {}))

    final_chunks = []
    for c_type, c_subtype, kor_name, raw_data, meta in chunks:
        if raw_data:
            final_chunks.append({"type": c_type, "subtype": c_subtype, "content": make_chunk_content(pid, ptitle, kor_name, raw_data), "metadata": meta})
    return final_chunks

# =====================================================================
# 3. 개별 파일 처리 (스마트 동기화 로직)
# =====================================================================
def process_file(file_path, file_hash, cur, conn):
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    policy_id = data.get("id")

    try:
        eligibility = data.get("eligibility", {})
        
        insert_master_query = """
            INSERT INTO welfare_policies 
            (id, leaflet_section, leaflet_number, title, short_summary, category, benefit_type, 
             severity_levels, has_companion_benefit, has_income_criteria, age_min, age_max, full_data, last_verified, version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                leaflet_section = EXCLUDED.leaflet_section, leaflet_number = EXCLUDED.leaflet_number,
                title = EXCLUDED.title, short_summary = EXCLUDED.short_summary,
                category = EXCLUDED.category, benefit_type = EXCLUDED.benefit_type,
                severity_levels = EXCLUDED.severity_levels, has_companion_benefit = EXCLUDED.has_companion_benefit,
                has_income_criteria = EXCLUDED.has_income_criteria, age_min = EXCLUDED.age_min,
                age_max = EXCLUDED.age_max, full_data = EXCLUDED.full_data,
                last_verified = EXCLUDED.last_verified, version = EXCLUDED.version, updated_at = NOW();
        """
        cur.execute(insert_master_query, (
            policy_id, data.get("leaflet_section"), data.get("leaflet_number"),
            data.get("title"), data.get("short_summary"), data.get("category"), data.get("benefit_type"),
            eligibility.get("severity_levels", []), True if eligibility.get("companion_allowed") else False, 
            True if eligibility.get("income_criteria") else False, *parse_age_criteria(eligibility.get("age_criteria")), 
            Json(data), data.get("last_verified"), 
            file_hash # 32글자 해시값이 에러 없이 쏙 들어갑니다!
        ))

        cur.execute("DELETE FROM policy_chunks WHERE policy_id = %s;", (policy_id,))

        chunks = extract_chunks(data)
        insert_chunk_query = """
            INSERT INTO policy_chunks (policy_id, chunk_type, chunk_subtype, content, embedding, embedding_model_version, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}"
        headers = {'Content-Type': 'application/json'}

        for idx, chunk in enumerate(chunks):
            for attempt in range(3):
                try:
                    payload = {"model": "models/gemini-embedding-001", "content": {"parts": [{"text": chunk["content"]}]}, "outputDimensionality": 768}
                    # timeout 추가 — 임베딩 API hang 방지 (connect=10, read=30). 누락 시 무한 대기 위험.
                    response = requests.post(api_url, headers=headers, json=payload, timeout=(10, 30))
                    response.raise_for_status()
                    emb_values = response.json()["embedding"]["values"]

                    cur.execute(insert_chunk_query, (
                        policy_id, chunk["type"], chunk["subtype"], chunk["content"],
                        emb_values, 'models/gemini-embedding-001', Json(chunk["metadata"])
                    ))
                    # 10개 단위 진행 로그 — hang 진단 보조
                    if (idx + 1) % 10 == 0 or idx == len(chunks) - 1:
                        logging.info(f"[{policy_id}] 청크 임베딩 진행 {idx+1}/{len(chunks)}")
                    break
                except Exception as e:
                    if attempt == 2:
                        logging.warning(f"[{policy_id}] 청크 {idx+1} 임베딩 3회 실패, 재호출 시도 후 raise: {e}")
                        raise
                    logging.debug(f"[{policy_id}] 청크 {idx+1} 임베딩 시도 #{attempt+1} 실패, {1.5**attempt:.1f}초 후 재시도: {e}")
                    sleep(1.5 ** attempt)

        conn.commit()
        logging.info(f"[{policy_id}] ✅ 동기화 완료! (재생성된 청크 수: {len(chunks)}개)")

    except Exception as e:
        conn.rollback()
        logging.error(f"[{policy_id}] ❌ 처리 중 오류 발생: {e}")

# =====================================================================
# 4. 메인 실행부
# =====================================================================
def main():
    logging.info("♻️ 스마트 동기화(Smart Sync) 모드를 시작합니다...")
    
    try:
        conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
        register_vector(conn)
        cur = conn.cursor()

        # [자동 패치] 해시값(32자)이 들어갈 수 있도록 version 컬럼 길이를 안전하게 확장합니다.
        cur.execute("ALTER TABLE welfare_policies ALTER COLUMN version TYPE VARCHAR(50);")
        conn.commit()
        
    except Exception as e:
        logging.error(f"DB 접속 실패: {e}")
        return

    cur.execute("SELECT id, version FROM welfare_policies;")
    db_versions = {row[0]: row[1] for row in cur.fetchall()}

    json_files = sorted(glob.glob("items/B0*.json"))
    if not json_files:
        logging.warning("items 폴더 내에 처리할 JSON 파일(B0*.json)이 없습니다.")
        return

    sync_count = 0
    skip_count = 0

    for file_path in json_files:
        filename = os.path.basename(file_path)
        policy_id = filename.split('_')[0] 
        
        current_hash = calculate_file_hash(file_path)
        saved_hash = db_versions.get(policy_id)

        if saved_hash == current_hash:
            logging.info(f"[{policy_id}] 변경사항 없음 (Skip)")
            skip_count += 1
            continue
        
        if saved_hash is None:
            logging.info(f"[{policy_id}] 🆕 신규 파일 감지! 적재를 시작합니다.")
        else:
            logging.info(f"[{policy_id}] 🔄 내용 변경 감지! 업데이트를 시작합니다.")
            
        process_file(file_path, current_hash, cur, conn)
        sync_count += 1

    logging.info(f"==========================================")
    logging.info(f"🎯 스마트 동기화 완료!")
    logging.info(f" - 새로 고치거나 추가된 정책: {sync_count}건")
    logging.info(f" - 변경 없어 건너뛴 정책: {skip_count}건")
    logging.info(f"==========================================")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()