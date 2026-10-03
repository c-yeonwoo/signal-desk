# 거시 지표의 관측일과 실제 공개시각

작성: 2026-10-03 · 범위: 기존 FRED/한국은행 ECOS 입력의 출처 표시와 입력 방어

- 거시 리본의 날짜는 **통계의 관측일**이다. CPI의 해당 월, 금리·지수의 해당 일자를 뜻하며 “그 날 장중 이 수치를 알았다”는 뜻이 아니다. 공식 페이지 링크와 검색용 시리즈 식별자를 함께 보존하고, API에서 `source_published_at=null`, `strict_pit_eligible=false`로 공개시각 미검증 상태를 명시한다.
- 수집 시각(`retrieved_at`)도 별도 필드다. 오래된 관측값을 오늘 가져왔다고 원천 데이터가 오늘 발표된 것으로 바꾸지 않는다. 현재 화면의 우호/비우호는 기존 규칙 그대로이며, 이번 보강으로 가중치·매매 한도·등록 가설을 변경하지 않는다.
- 요청 응답은 1 MiB로 제한하고 키가 다른 호스트로 전달될 수 있는 리다이렉트를 따르지 않는다. 잘못된 날짜·비수치·무한대·NaN은 입력하지 않는다. FRED HTTP 오류 문자열에는 API 키가 담긴 URL이 들어갈 수 있으므로, 로그에는 예외 **유형**만 남긴다.
- FRED의 기본 응답은 조회일 기준 현재 알려진 값이며, 수정 이전에 알았던 값을 재현하려면 `realtime_start`/`realtime_end`를 지정하는 별도의 vintage 자료가 필요하다. 날짜 단위 vintage만으로 정확한 장중 발표시각은 알 수 없다. 이번 변경은 역사적 PIT 거시 백테스트의 적격 판정을 열지 않는다.
- 추가 수집 후보의 우선순위는 공식 API의 시점·갱신·이용 조건을 먼저 확인한 뒤 결정한다. 원천 월간 매출·산업 통계를 한 회사나 업종 전체의 매수 신호로 바로 전가하지 않는다.

원문: [FRED 관측 API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html), [FRED/ALFRED 실시간 기간](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html), [한국은행 ECOS](https://ecos.bok.or.kr/).
