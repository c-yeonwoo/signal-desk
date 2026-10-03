# 기존 SEC EDGAR 수집의 실제 연락처 관문

작성: 2026-10-03 · 범위: 기존 미국 재무/13F 수집 경로의 접근 정직성

- 기존 `ingest/edgar.py`의 `admin@signal-desk.local`은 실제 회신 가능한 연락처가 아닌 예시 주소였다. [SEC 자동 접근 안내](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)는 식별 가능한 User-Agent와 적정 요청 속도를 요청한다. 이 주소를 그대로 보내는 것은 올바른 운영 신원으로 볼 수 없다.
- 운영자가 **실제 수신 가능한** `SEC_CONTACT_EMAIL`을 설정하기 전에는 기존 회사 CIK 맵, companyfacts, 13F의 새 SEC 요청을 시작하지 않는다. 예시/로컬 도메인은 거부한다. 관리자 `/api/data-health`의 `sec_edgar`에는 연락처의 존재 여부만 표시하며 주소 자체는 내보내지 않는다.
- 연락처가 없다고 해서 기존 미국 재무 파일에 `edgar_attempted_at`을 새로 찍거나 저장된 13F 파일을 빈 결과로 덮지 않는다. 과거 캐시는 과거 관측으로 남고, **신선한 자료라는 뜻은 아니다**. 현재의 기존 신호 입력이나 가중치를 바꾸지는 않는다.
- 연락처를 설정한 뒤에도 실제 SEC 요청은 HTTPS 공식 호스트만 허용하고, 리다이렉트를 따르지 않으며, 한 응답을 32 MiB로 제한하고, 프로세스 안의 요청을 최소 0.2초 간격으로 직렬화한다. 이는 SEC의 [공식 개발자 안내](https://www.sec.gov/about/developer-resources)에 제시된 초당 10회 상한보다 보수적인 로컬 제한이지 여러 컨테이너 합산 한도 보장은 아니다.
- 향후 미국 관심종목 비교 카드는 새 `financial_evidence`의 원문·관측시각·공식 CIK 확인을 따로 사용한다. 기존 `edgar.py` 캐시를 사전등록 PIT 근거나 검증된 기업 변화로 승격하지 않는다.

운영 확인: 연락처 설정 전 `sec_edgar.status=missing_real_contact`, 신규 SEC 요청 0, 기존 13F/재무 캐시 보존을 확인한다. 실제 연락처를 설정한 뒤 `contact_configured`는 수집 성공이 아니므로 공식 응답/CIK/기간/단위와 요청량을 다시 대조한다.
