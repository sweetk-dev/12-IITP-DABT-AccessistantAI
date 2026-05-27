import os
import glob
import json
import re
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

if not GEMINI_API_KEY:
    logging.error("GEMINI_API_KEY가 설정되지 않았습니다. .env 파일을 확인해주세요.")
    exit(1)

# =====================================================================
# 2. 데이터베이스 스키마 초기화 함수
# =====================================================================
def init_schema(cur, conn):
    logging.info("데이터베이스 스키마 초기화 및 확정(v1.5) 중...")
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    cur.execute("DROP TABLE IF EXISTS welfare_policies CASCADE;")
    cur.execute("DROP TABLE IF EXISTS policy_chunks CASCADE;")
    
    # 마스터 테이블 생성 (GIN 인덱스 대응)
    cur.execute("""
        CREATE TABLE welfare_policies (
            id VARCHAR(10) PRIMARY KEY,
            leaflet_section VARCHAR(50),
            leaflet_number INT,
            title VARCHAR(200),
            short_summary TEXT,
            category VARCHAR(20),
            benefit_type VARCHAR(20),
            severity_levels TEXT[],
            has_companion_benefit BOOLEAN,
            has_income_criteria BOOLEAN,
            age_min INT,
            age_max INT,
            full_data JSONB,
            last_verified DATE,
            version VARCHAR(10),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX idx_policies_category ON welfare_policies(category);
        CREATE INDEX idx_policies_severity ON welfare_policies USING GIN(severity_levels);
        CREATE INDEX idx_policies_jsonb ON welfare_policies USING GIN(full_data);
    """)

    # 청크 테이블 생성 (신형 모델 이름으로 기본값 변경)
    cur.execute("""
        CREATE TABLE policy_chunks (
            id BIGSERIAL PRIMARY KEY,
            policy_id VARCHAR(10) REFERENCES welfare_policies(id) ON DELETE CASCADE,
            chunk_type VARCHAR(30) NOT NULL,
            chunk_subtype VARCHAR(100),
            content TEXT NOT NULL,
            embedding VECTOR(768),
            embedding_model_version VARCHAR(50) NOT NULL DEFAULT 'models/gemini-embedding-001',
            metadata JSONB,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX idx_chunks_policy ON policy_chunks(policy_id);
        CREATE INDEX idx_chunks_type ON policy_chunks(chunk_type);
    """)
    conn.commit()

# =====================================================================
# 3. 데이터 파싱 및 전처리 유틸리티
# =====================================================================
def parse_age_criteria(age_text):
    if not age_text:
        return None, None
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

    summary_data = {"요약": data.get("short_summary"), "지원금액_비율": data.get("supported_amount")}
    chunks.append(("summary", None, "요약 및 지원규모", summary_data, {}))
    chunks.append(("eligibility", None, "지원 대상 및 자격요건", data.get("eligibility"), {}))
    chunks.append(("how_to_use", None, "이용 및 혜택 적용 방법", data.get("how_to_use"), {}))
    chunks.append(("application", None, "신청 방법 및 필요 서류", data.get("application"), {}))
    
    for i, faq in enumerate(data.get("faq", [])):
        faq_text = f"질문: {faq.get('q')}\n답변: {faq.get('a')}"
        chunks.append(("faq", f"faq_q{i+1}", "자주 묻는 질문(FAQ)", faq_text, {}))
        
    chunks.append(("exceptions", None, "예외 사항 및 주의점", data.get("exceptions_and_caveats"), {}))
    chunks.append(("legal_basis", None, "법적 근거", data.get("legal_basis"), {}))
    
    for i, agency in enumerate(data.get("operating_agencies", [])):
        meta = {"region": agency.get("region"), "agency": agency.get("agency")}
        chunks.append(("agency_specific", f"agency_{i}", f"{agency.get('region')} {agency.get('agency')} 세부 운영", agency, meta))
        
    chunks.append(("validity", None, "유효기간 및 갱신", data.get("validity"), {}))
    chunks.append(("penalties", None, "부정사용 제재 및 벌칙", data.get("penalties_for_misuse"), {}))
    chunks.append(("contact", None, "문의처 및 콜센터", data.get("contact"), {}))

    final_chunks = []
    for c_type, c_subtype, kor_name, raw_data, meta in chunks:
        if raw_data:
            content = make_chunk_content(pid, ptitle, kor_name, raw_data)
            final_chunks.append({
                "type": c_type, "subtype": c_subtype, 
                "content": content, "metadata": meta
            })
    return final_chunks

# =====================================================================
# 4. 개별 파일 처리 메인 로직 (신형 모델 적용 & 차원 압축)
# =====================================================================
def process_file(file_path, cur, conn):
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    policy_id = data.get("id")
    logging.info(f"[{policy_id}] 데이터 처리 시작...")

    try:
        eligibility = data.get("eligibility", {})
        has_companion = True if eligibility.get("companion_allowed") else False
        has_income = True if eligibility.get("income_criteria") else False
        severity_levels = eligibility.get("severity_levels", [])
        age_min, age_max = parse_age_criteria(eligibility.get("age_criteria"))
        
        insert_master_query = """
            INSERT INTO welfare_policies 
            (id, leaflet_section, leaflet_number, title, short_summary, category, benefit_type, 
             severity_levels, has_companion_benefit, has_income_criteria, age_min, age_max, full_data, last_verified, version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                leaflet_section = EXCLUDED.leaflet_section,
                leaflet_number = EXCLUDED.leaflet_number,
                title = EXCLUDED.title,
                short_summary = EXCLUDED.short_summary,
                category = EXCLUDED.category,
                benefit_type = EXCLUDED.benefit_type,
                severity_levels = EXCLUDED.severity_levels,
                has_companion_benefit = EXCLUDED.has_companion_benefit,
                has_income_criteria = EXCLUDED.has_income_criteria, 
                age_min = EXCLUDED.age_min,
                age_max = EXCLUDED.age_max,
                full_data = EXCLUDED.full_data,
                last_verified = EXCLUDED.last_verified,
                version = EXCLUDED.version,
                updated_at = NOW();
        """
        cur.execute(insert_master_query, (
            policy_id, data.get("leaflet_section"), data.get("leaflet_number"),
            data.get("title"), data.get("short_summary"), data.get("category"), data.get("benefit_type"),
            severity_levels, has_companion, has_income, age_min, age_max, 
            Json(data), data.get("last_verified"), data.get("version")
        ))

        cur.execute("DELETE FROM policy_chunks WHERE policy_id = %s;", (policy_id,))

        chunks = extract_chunks(data)
        max_chunk_len = max([len(c["content"]) for c in chunks])
        logging.info(f"  - 생성된 청크 수: {len(chunks)}개 (최대 길이: {max_chunk_len} 글자)")
        
        insert_chunk_query = """
            INSERT INTO policy_chunks 
            (policy_id, chunk_type, chunk_subtype, content, embedding, embedding_model_version, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """

        # 구글 신형 모델(gemini-embedding-001) REST API 엔드포인트
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}"
        headers = {'Content-Type': 'application/json'}

        for idx, chunk in enumerate(chunks):
            for attempt in range(3):
                try:
                    payload = {
                        "model": "models/gemini-embedding-001",
                        "content": {
                            "parts": [{"text": chunk["content"]}]
                        },
                        "outputDimensionality": 768  # 중요: 3072차원을 768로 압축하여 DB 스키마와 맞춤
                    }
                    
                    response = requests.post(api_url, headers=headers, json=payload)
                    response.raise_for_status() 
                    
                    emb_values = response.json()["embedding"]["values"]
                    
                    cur.execute(insert_chunk_query, (
                        policy_id, chunk["type"], chunk["subtype"], 
                        chunk["content"], emb_values, 'models/gemini-embedding-001', Json(chunk["metadata"])
                    ))
                    break
                except Exception as e:
                    if attempt == 2:
                        error_msg = response.text if 'response' in locals() else '응답없음'
                        logging.error(f"  - [청크 {idx+1}] 임베딩 최종 실패: {e} | {error_msg}")
                        raise
                    sleep(1.5 ** attempt)

        conn.commit()
        logging.info(f"[{policy_id}] 완료.")

    except Exception as e:
        conn.rollback()
        logging.error(f"[{policy_id}] 처리 중 오류 발생 (해당 파일만 건너뜁니다): {e}")

# =====================================================================
# 5. 실행부 (엔트리 포인트)
# =====================================================================
def main():
    logging.info("시스템 초기화 중...")
    
    try:
        conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
        register_vector(conn)
        cur = conn.cursor()
    except Exception as e:
        logging.error(f"DB 접속 실패. 설정을 확인하세요: {e}")
        return

    init_schema(cur, conn)

    json_files = sorted(glob.glob("items/B0*.json"))
    if not json_files:
        logging.warning("items 폴더 내에 처리할 JSON 파일(B0*.json)이 없습니다.")
    else:
        for jf in json_files:
            process_file(jf, cur, conn) 

    logging.info("대규모 데이터 적재 완료. HNSW 인덱스 생성을 시작합니다...")
    cur.execute("""
        CREATE INDEX idx_chunks_hnsw ON policy_chunks USING hnsw (embedding vector_cosine_ops) 
        WITH (m = 16, ef_construction = 64);
    """)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM welfare_policies;")
    n_policies = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM policy_chunks;")
    n_chunks = cur.fetchone()[0]
    
    logging.info(f"==========================================")
    logging.info(f"🎯 적재 결과 요약: 정책 {n_policies}건, 청크 {n_chunks}건")
    
    cur.execute("""
        SELECT id, title FROM welfare_policies p
        WHERE NOT EXISTS (SELECT 1 FROM policy_chunks c WHERE c.policy_id = p.id);
    """)
    orphans = cur.fetchall()
    if orphans:
        logging.warning(f"⚠️ 경고: 청크가 하나도 생성되지 않은 빈 정책 발견: {orphans}")
    else:
        logging.info("✅ 모든 정책에 청크가 정상적으로 매핑되었습니다.")
    logging.info(f"==========================================")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()