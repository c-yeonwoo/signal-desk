"""퀀트 방법론 레퍼런스 카탈로그 — 두뇌 레이어(자가 진단)가 'gap → 업계 검증 방법론'을 매핑할 때
참조하는 구조화 지식. **원칙: LLM이 산식을 창작하지 않는다.** 진단 루프는 여기 등재된(=업계 검증된)
방법 중에서만 개선을 제안하고, 관리자 승인 후 사람이 구현한다. (관련: 정확도 로드맵·프로덕트 비전)

각 항목:
- key/name/category(factor|gate|valuation|weighting|structure|risk|sizing|diagnostic)
- status: active(엔진에 이미 반영) | candidate(로드맵/후보) | rejected(검토 후 미채택, 이유 명시)
- idea: 무엇을 재나(정의)  · formula: 핵심 산식(요약)
- addresses: 어떤 gap/개선을 노리나  · evidence: 근거/출처
- risk: 도입 리스크  · validate: 우리 측정체계로 검증하는 법  · note: signal-desk 특이사항
"""

from __future__ import annotations

METHODS = [
    # ---------- active: 엔진에 이미 반영 ----------
    {"key": "technical", "name": "기술적(RSI·MACD·MA)", "category": "factor", "status": "active",
     "idea": "가격 흐름의 추세·과열/과매도", "formula": "RSI(14)·MACD(12/26/9) 크로스·MA20/60 배열 → [-3,3]",
     "addresses": "단기 타이밍", "evidence": "표준 기술적 분석", "risk": "노이즈·후행성",
     "validate": "트래커 팩터 IC(technical)", "note": "가중 0.35, 항상 활성"},
    {"key": "fundamental", "name": "기본(ROE·부채·성장)", "category": "factor", "status": "active",
     "idea": "재무 건전성·수익성·성장", "formula": "ROE/PER/성장/부채 임계 래더 → [-2,2]",
     "addresses": "질적 바탕", "evidence": "기본적 분석", "risk": "후행 연간데이터",
     "validate": "팩터 IC(fundamental)", "note": "데이터 없으면 제외"},
    {"key": "valuation_xs", "name": "밸류 횡단면 백분위", "category": "factor", "status": "active",
     "idea": "PER·PBR 유니버스 상대 위치", "formula": "norm=(50-percentile)/50",
     "addresses": "저평가 매력", "evidence": "Value 팩터(Barra)", "risk": "고평가주 함정",
     "validate": "팩터 IC(valuation)", "note": "섹터 중립화 적용됨(v1) → sector_neutral 참조"},
    {"key": "reversion", "name": "낙폭과대 반등", "category": "factor", "status": "active",
     "idea": "단기 과매도 되돌림", "formula": "N일 누적수익 ≤-15% & RSI<35 → +",
     "addresses": "단기 저가 포착", "evidence": "단기 mean-reversion", "risk": "떨어지는 칼",
     "validate": "팩터 IC(reversion)", "note": "발동 시에만 가중, 추세게이트와 결합"},
    {"key": "flow", "name": "수급(외인·기관)", "category": "factor", "status": "active",
     "idea": "스마트머니 순매수 강도", "formula": "(외인+기관 순매수)/거래량, 자기정규화",
     "addresses": "정보 우위 매수", "evidence": "한국시장 수급 알파", "risk": "소스(네이버) 취약성",
     "validate": "팩터 IC(flow)", "note": "KR만; 주수 기반이라 스케일 무관"},
    {"key": "quality_fscore", "name": "퀄리티(축약 Piotroski)", "category": "factor", "status": "active",
     "idea": "재무 건전성·개선 점수", "formula": "순이익>0·ROE>0·ROE개선·부채개선·성장 5체크",
     "addresses": "부실 회피", "evidence": "Piotroski F-Score", "risk": "CFO/유동비율 미반영(5체크 축약)",
     "validate": "팩터 IC(quality)", "note": "DART 당기+전기"},
    {"key": "momentum_12_1", "name": "모멘텀(12-1)", "category": "factor", "status": "active",
     "idea": "중기 추세 지속", "formula": "252일 수익 − 최근 21일 제외",
     "addresses": "추세 지속 포착", "evidence": "Jegadeesh-Titman; Barra Momentum", "risk": "급반전 취약",
     "validate": "팩터 IC(momentum)", "note": "가격만 필요 → 백테스트 반영"},
    {"key": "short_interest", "name": "공매도 거래비중", "category": "factor", "status": "active",
     "idea": "공매도 압력(하방)", "formula": "공매도량/총거래량, 임계 이상만 음(-) 페널티",
     "addresses": "하방 리스크 회피", "evidence": "Short interest anomaly", "risk": "잔고 아닌 거래(노이즈↑)",
     "validate": "팩터 IC(short)", "note": "KR만; 비중<8% 중립 제외"},
    {"key": "regime_gate", "name": "국면 매수임계 상향", "category": "gate", "status": "active",
     "idea": "약세·비우호 국면에서 매수 문턱↑", "formula": "breadth(%>MA60)+평균모멘텀 → buy_threshold_bump",
     "addresses": "국면 역풍 시 오신호 억제", "evidence": "regime filtering", "risk": "국면 판정 지연",
     "validate": "국면별 매수 정밀도 비교", "note": "매도 임계는 불변"},
    {"key": "trend_gate", "name": "하락추세 게이트(떨어지는 칼)", "category": "gate", "status": "active",
     "idea": "구조적 하락 중 매수 차단", "formula": "종가<MA20<MA60 → BUY→HOLD, 낙폭/밸류 매수기여 무효화",
     "addresses": "가치함정 방지", "evidence": "trend filter", "risk": "반등 초기 놓침",
     "validate": "게이트 on/off 정밀도 A/B", "note": "live+backtest 공통"},
    {"key": "earnings_gate", "name": "실적발표 임박 게이트", "category": "gate", "status": "active",
     "idea": "발표 전 신규 매수 보류", "formula": "D-day까지 ≤7일이면 BUY→HOLD",
     "addresses": "바이너리 이벤트 회피", "evidence": "PEAD/이벤트 리스크", "risk": "발표 후 급등 기회 놓침",
     "validate": "게이트 대상 매수 정밀도", "note": "US 어닝캘린더; KR 소스 gap"},
    {"key": "kb_event_veto", "name": "KB 악재 veto", "category": "gate", "status": "active",
     "idea": "악재 감지 시 매수 후보 제외", "formula": "KB 키워드/심각도 → event_risk, TTL 5일",
     "addresses": "악재 노출 차단", "evidence": "뉴스 이벤트 스터디", "risk": "키워드 정밀도",
     "validate": "veto 후 사후수익", "note": "점수 미반영, veto 전용"},
    {"key": "target_v2", "name": "목표가 v2(선행EPS+애널 앵커)", "category": "valuation", "status": "active",
     "idea": "선행이익·애널 컨센 기반 참고가", "formula": "선행EPS×중앙값PER; 애널 목표주가; 앵커 나열",
     "addresses": "후행 PER 이익사이클 왜곡 교정", "evidence": "forward valuation; sell-side consensus",
     "risk": "애널 낙관편향; 유니버스 PER 사용(섹터 미반영)", "validate": "목표가 upside vs 실현수익 상관",
     "note": "PR#175; KR만; 섹터중립화로 정교화 예정"},

    # ---------- candidate: 로드맵/후보 ----------
    {"key": "sector_neutral", "name": "섹터 중립화", "category": "structure", "status": "active",
     "idea": "팩터를 섹터 내 상대값으로 표준화", "formula": "섹터 내 percentile/z-score(작은 섹터는 유니버스 fallback)",
     "addresses": "섹터 편향 제거(반도체 고PER·고모멘텀 왜곡)", "evidence": "Barra USE4; MSCI factor investing",
     "risk": "섹터 표본 작으면 불안정·섹터오분류 전파", "validate": "트래커 팩터 IC 전후 비교",
     "note": "v1 적용: 밸류(섹터 percentile)+목표가(섹터 중앙값 PER). ⬜v2 확장: 모멘텀·수급·공매도(횡단면 2-pass 필요)"},
    {"key": "ic_weighting", "name": "IC 기반 동적 가중", "category": "weighting", "status": "candidate",
     "idea": "팩터 예측력(IC)에 비례해 가중", "formula": "w_i ∝ 최근 IC_i (보수적)",
     "addresses": "고정 가중치 → 정보 반영", "evidence": "Grinold-Kahn", "risk": "IC 불안정·과적합",
     "validate": "실측 IC로 사후검증", "note": "트래커가 IC 이미 측정 → 근거 확보. 완전자동 X, 보수적 반영"},
    {"key": "estimate_revision", "name": "추정치 리비전", "category": "factor", "status": "candidate",
     "idea": "컨센서스 상/하향 방향", "formula": "Δ선행EPS·Δ목표주가 (시계열)",
     "addresses": "새 알파축(강한 팩터)", "evidence": "revision momentum(문헌 강함)",
     "risk": "시계열 축적 필요(수주)·소스 취약", "validate": "트래커 IC(revision)",
     "note": "컨센 수집 중(PR#174) → 축적 후 착수"},
    {"key": "earnings_surprise_pead", "name": "어닝 서프라이즈·PEAD", "category": "factor", "status": "candidate",
     "idea": "실적 서프라이즈 후 표류", "formula": "(실제−컨센)/표준편차 → 방향 드리프트",
     "addresses": "이벤트 알파 포착(기회 확대)", "evidence": "Post-Earnings Announcement Drift",
     "risk": "실적 실제치·컨센 데이터 필요", "validate": "서프라이즈 분위별 사후수익",
     "note": "현재 어닝은 게이트(회피)만; PEAD는 포착(recall↑)"},
    {"key": "multi_horizon", "name": "다중 horizon 신호 수명", "category": "structure", "status": "candidate",
     "idea": "팩터별 예측 지평 명시", "formula": "신호를 5/20/60일 중 강한 horizon에 라벨",
     "addresses": "기회 확대(setup별 보유기간)", "evidence": "factor decay 문헌", "risk": "복잡도↑",
     "validate": "트래커가 이미 5/20/60일 측정", "note": "recall 확대 레버"},
    {"key": "crowding_risk_lite", "name": "편중 경고(lite 리스크모델)", "category": "risk", "status": "candidate",
     "idea": "섹터·팩터 쏠림 감지", "formula": "포트 섹터/팩터 노출 집중도",
     "addresses": "crowded trade 리스크", "evidence": "2025 퀀트펀드 손실(요인상관·crowding)",
     "risk": "전체 Barra는 과함(경고 수준만)", "validate": "쏠림 구간 드로다운",
     "note": "전체 요인공분산은 우리 규모에 과함"},
    {"key": "vol_adjusted_sizing", "name": "변동성 조정 사이징", "category": "sizing", "status": "candidate",
     "idea": "신호강도×역변동성 비중", "formula": "w ∝ signal / recent_vol",
     "addresses": "리스크 조정 수익", "evidence": "risk parity/vol targeting", "risk": "변동성 추정 오차",
     "validate": "리밸런싱 성과 A/B", "note": "기존 리밸런싱에 부착 가능"},
    {"key": "low_volatility", "name": "저변동성 팩터", "category": "factor", "status": "candidate",
     "idea": "저변동 종목의 초과수익", "formula": "역(최근 변동성) 랭크",
     "addresses": "상보적 알파축", "evidence": "Low-volatility anomaly(Barra)", "risk": "국면 의존",
     "validate": "팩터 IC(low_vol)", "note": "가격만 필요 → 즉시 백테스트 가능"},

    # ---------- rejected: 검토 후 미채택 ----------
    {"key": "ml_alpha_mining", "name": "ML/LLM 알파 마이닝", "category": "factor", "status": "rejected",
     "idea": "수천 피처 비선형 학습·자동 팩터 생성", "formula": "XGBoost/NN/GAN",
     "addresses": "새 알파", "evidence": "헤지펀드(대규모 데이터)", "risk": "우리 데이터(200종목·1년·스케일)로 과적합 확실",
     "validate": "—", "note": "미채택: 결정론+IC측정이 우리에겐 더 정직/강함"},
    {"key": "stat_arb", "name": "통계적 차익(페어)", "category": "factor", "status": "rejected",
     "idea": "공적분·평균회귀 페어트레이딩", "formula": "cointegration spread",
     "addresses": "시장중립 알파", "evidence": "Renaissance/2sigma 계열", "risk": "인프라·차입·실시간성",
     "validate": "—", "note": "미채택: MVP 범위 밖"},
    {"key": "vwap_twap", "name": "VWAP/TWAP 실행", "category": "structure", "status": "rejected",
     "idea": "대형 주문 분할 체결", "formula": "vol/time-weighted slicing",
     "addresses": "시장충격 최소화", "evidence": "기관 실행", "risk": "—",
     "validate": "—", "note": "미채택: 소액 종가체결 paper라 무의미"},
]

_STATUSES = {"active", "candidate", "rejected"}


def all_methods() -> list[dict]:
    return METHODS


def by_status(status: str) -> list[dict]:
    return [m for m in METHODS if m["status"] == status]


def by_category(category: str) -> list[dict]:
    return [m for m in METHODS if m["category"] == category]


def get(key: str) -> dict | None:
    return next((m for m in METHODS if m["key"] == key), None)
