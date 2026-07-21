# 기후 시그널 (실험)

> 상태: **뱃지·관측 전용** · 봇/문턱/`combine` 미연동  
> 모듈: `signals/climate.py` · UI: `.clim-pill`

## 한 줄

기존 시그널 점수에, **최근 이슈 흐름의 emphasized 갈래** 파급(`q`)을 얹어  
날씨 라벨(맑음+/맑음/흐림/비/폭풍)로만 보여 주는 실험 트랙.

## 격리

| | 기존 시그널 | 기후 |
|---|---|---|
| `score` / `kind` | 불변 | 별도 `climate.score` / `climate.kind` |
| `combine()` | 8팩터만 | 미사용 |
| 봇·buylist·매수 문턱 | 기존만 | **미연동** |
| UI | `.sig-pill` 녹/빨 | `.clim-pill` 하늘/보라 |

## 수치화 (v1)

1. 이슈 흐름 캐시(`hypo:v4`) · 7일 초과 시 뱃지 숨김  
2. **emphasized fork**의 outcome `sector_keys` · `watch_tickers`만  
3. `q = Σ sign × support% × (0.35+0.65×branch%)` · `|q|≤0.6`  
4. `affinity=risk_off`이면 성장 업종 키에 역풍(`sign=-`) 추가  
5. `score_climate = clamp(score_base + 0.8·q, -3, 3)` → 기존 `classify()`  
6. 문장·detail은 reason 표시만 (점수 입력 금지)

## Shadow 관측

마감 후 `climate.snapshot_shadow()` → `data/cache/climate_shadow.json`  
관리자 `GET /api/climate-shadow` 로 일별 부착 수·기존 kind와 다른 비율만 본다.  
**승격 게이트 아님** · 실측 IC 비교는 표본 쌓인 뒤.

## 비목표

- 봇 연동 · 문턱 변경 · LLM 문장 가산 · path/alt 동시 반영(v1 제외)  
