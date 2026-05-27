import os
import requests
import psycopg2
from pgvector.psycopg2 import register_vector
from dotenv import load_dotenv

# 환경설정 로드
# 상위 폴더(welfare_backend/) 의 .env 를 단일 진입점으로 사용
from pathlib import Path as _Path
load_dotenv(_Path(__file__).resolve().parent.parent / ".env")
DB_NAME = os.environ.get("DB_NAME", "welfare_db")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "")
DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
DB_PORT = os.environ.get("DB_PORT", "5432")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

def get_embedding(text):
    """사용자의 자연어 질문을 768차원 벡터로 변환합니다."""
    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={GEMINI_API_KEY}"
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": 768
    }
    response = requests.post(api_url, json=payload)
    response.raise_for_status()
    return response.json()["embedding"]["values"]

def search_welfare_db(query, top_k=3):
    """벡터 DB(pgvector)를 검색하여 가장 관련성 높은 조각을 가져옵니다."""
    print(f"\n🔍 사용자의 질문: '{query}'")
    print("--------------------------------------------------")
    print("⏳ 1. 질문의 숨은 의미(문맥)를 벡터로 변환하는 중...")
    query_vector = get_embedding(query)

    print("⏳ 2. DB에서 가장 유사한 정책 정보 3개를 찾는 중...\n")
    conn = psycopg2.connect(dbname=DB_NAME, user=DB_USER, password=DB_PASS, host=DB_HOST, port=DB_PORT)
    register_vector(conn)
    cur = conn.cursor()

    # 핵심 SQL: pgvector의 코사인 거리 연산자(<=>)를 사용해 가장 가까운 텍스트를 찾습니다.
    # 1 - 거리 = 유사도(Similarity) 점수가 됩니다. (1에 가까울수록 완벽 일치)
    sql = """
        SELECT p.title, c.chunk_type, c.chunk_subtype, c.content, 1 - (c.embedding <=> %s::vector) as similarity
        FROM policy_chunks c
        JOIN welfare_policies p ON c.policy_id = p.id
        ORDER BY c.embedding <=> %s::vector
        LIMIT %s;
    """
    
    # 쿼리 실행
    cur.execute(sql, (query_vector, query_vector, top_k))
    results = cur.fetchall()

    print("================ [✨ 검색 결과 ✨] ================")
    for i, row in enumerate(results):
        title, chunk_type, chunk_subtype, content, similarity = row
        print(f"🥇 랭킹 {i+1}위 (유사도 점수: {similarity:.4f})")
        print(f"📌 정책명: {title}")
        print(f"🏷️ 타입: {chunk_type} ({chunk_subtype or 'N/A'})")
        print(f"📖 내용:\n{content}")
        print("--------------------------------------------------")

    cur.close()
    conn.close()

if __name__ == "__main__":
    # =========================================================
    # 💡 테스트하고 싶은 질문을 아래에 자유롭게 적어보세요!
    # =========================================================
    
    # test_question = "지하철 탈 때 버스로 환승해도 무료로 적용 되나요?"
    # test_question = "활동지원서비스 신청하려면 어디로 가야하나요?"
    test_question = "버스요금 할인은 어떻게 받아야 하나요? 경기도 교통카드로 받을 수 있나요?"
    
    search_welfare_db(test_question)