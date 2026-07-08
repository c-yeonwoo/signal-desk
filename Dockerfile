# Signal Desk — prod 컨테이너. 서버(FastAPI+uvicorn)가 자동매매 루프·KB 일일수집을
# in-process로 함께 돌리므로, 컨테이너를 항상 켜두면 별도 스케줄러가 필요 없다.
FROM python:3.12-slim

WORKDIR /app

# 숏폼 영상 렌더는 서버에서 하지 않는다(저사양 컨테이너 OOM) — 자료(zip)만 만들어 사용자 PC에서 렌더.
# 따라서 ffmpeg/cairo 시스템 라이브러리가 서버엔 불필요(서버 경량 유지).

# 의존성 먼저(레이어 캐시)
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# 데이터 캐시/SQLite는 볼륨으로 마운트 권장(백업 대상): -v signal_desk_data:/app/data
ENV HOST=0.0.0.0 PORT=8765 APP_ENV=prod
EXPOSE 8765

# 시세·재무 캐시가 없으면 최초 1회 `sigdesk fetch` 필요(README 참고)
CMD ["sigdesk", "serve"]
