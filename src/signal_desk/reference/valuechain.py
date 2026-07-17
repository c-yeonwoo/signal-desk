"""섹터별 밸류체인(업스트림→미드스트림→다운스트림) 대표기업 큐레이션 — 국내(코스피)/해외(나스닥).

투자 판단을 위한 "산업 구조 지도" 참고자료다. 종목은 각 단계의 대표성 기준으로 큐레이션했고
(전수/추천 아님), 국내는 티커를 달아 시그널 탭과 연결할 수 있게 했다. 해외는 라벨(티커)만.
사이클 탭의 국면별 주도 섹터(reference/cycle.py)와 `tags`로 연결된다.

주의: 편입/대표기업은 시황·구성 변화로 달라질 수 있는 큐레이션 스냅샷이며 추천이 아니다.
"""

from __future__ import annotations


def _d(name, ticker=None):
    return {"name": name, "ticker": ticker}


SECTORS = [
    {
        "key": "semiconductor",
        "name": "반도체",
        "tags": ["반도체"],
        "summary": "소재·장비 → 설계·제조 → 후공정·세트로 이어지는 한국 증시의 핵심 축.",
        "stages": [
            {
                "stage": "업스트림 · 소재/장비",
                "desc": "웨이퍼·소재·노광/증착 장비 등 전공정 인프라.",
                "domestic": [_d("한미반도체", "042700"), _d("원익IPS", "240810"),
                             _d("리노공업", "058470"), _d("이오테크닉스", "039030"), _d("DB하이텍", "000990")],
                "overseas": [_d("ASML", "ASML"), _d("Applied Materials", "AMAT"),
                             _d("Lam Research", "LRCX"), _d("KLA", "KLAC")],
            },
            {
                "stage": "미드스트림 · 설계/제조",
                "desc": "메모리·파운드리·팹리스 등 실제 칩 생산·설계.",
                "domestic": [_d("삼성전자", "005930"), _d("SK하이닉스", "000660")],
                "overseas": [_d("TSMC", "TSM"), _d("NVIDIA", "NVDA"),
                             _d("Micron", "MU"), _d("Intel", "INTC"), _d("Broadcom", "AVGO")],
            },
            {
                "stage": "다운스트림 · 후공정/부품/세트",
                "desc": "패키징·기판·카메라모듈 등 부품과 최종 세트.",
                "domestic": [_d("삼성전기", "009150"), _d("LG이노텍", "011070")],
                "overseas": [_d("Apple", "AAPL"), _d("Dell", "DELL")],
            },
        ],
    },
    {
        "key": "ai_datacenter",
        "name": "AI/데이터센터",
        "tags": ["AI/데이터센터", "IT/인터넷"],
        "summary": "AI 가속기(HBM) → 서버/전력·냉각 인프라 → 클라우드·소프트웨어 수요로 확산.",
        "stages": [
            {
                "stage": "업스트림 · AI 반도체/HBM",
                "desc": "AI 학습·추론용 가속기와 고대역폭 메모리(HBM).",
                "domestic": [_d("SK하이닉스", "000660"), _d("삼성전자", "005930"), _d("한미반도체", "042700")],
                "overseas": [_d("NVIDIA", "NVDA"), _d("Broadcom", "AVGO"), _d("AMD", "AMD")],
            },
            {
                "stage": "미드스트림 · 서버/전력·냉각",
                "desc": "데이터센터 서버·전력공급·액침/공랭 냉각 인프라.",
                "domestic": [_d("LS ELECTRIC", "010120"), _d("HD현대일렉트릭", "267260")],
                "overseas": [_d("Super Micro", "SMCI"), _d("Vertiv", "VRT"), _d("Dell", "DELL")],
            },
            {
                "stage": "다운스트림 · 클라우드/SW",
                "desc": "하이퍼스케일 클라우드와 AI 소프트웨어·플랫폼 서비스.",
                "domestic": [_d("네이버", "035420"), _d("삼성SDS", "018260"), _d("카카오", "035720")],
                "overseas": [_d("Microsoft", "MSFT"), _d("Amazon", "AMZN"), _d("Google", "GOOGL")],
            },
        ],
    },
    {
        "key": "battery",
        "name": "2차전지",
        "tags": ["2차전지"],
        "summary": "리튬·양극재 소재 → 셀 제조 → 전기차·ESS 수요로 이어지는 밸류체인.",
        "stages": [
            {
                "stage": "업스트림 · 광물/소재",
                "desc": "리튬·니켈 등 광물과 양극재·음극재·전해질 소재.",
                "domestic": [_d("포스코홀딩스", "005490"), _d("에코프로비엠", "247540"),
                             _d("포스코퓨처엠", "003670"), _d("엘앤에프", "066970"), _d("고려아연", "010130")],
                "overseas": [_d("Albemarle", "ALB"), _d("SQM", "SQM")],
            },
            {
                "stage": "미드스트림 · 셀 제조",
                "desc": "배터리 셀·모듈·팩 제조.",
                "domestic": [_d("LG에너지솔루션", "373220"), _d("삼성SDI", "006400"), _d("SK이노베이션", "096770")],
                "overseas": [_d("CATL", "300750.SZ"), _d("Panasonic", "6752.T")],
            },
            {
                "stage": "다운스트림 · 전기차/ESS",
                "desc": "전기차 완성차와 에너지저장장치(ESS) 수요처.",
                "domestic": [_d("현대차", "005380"), _d("기아", "000270")],
                "overseas": [_d("Tesla", "TSLA"), _d("BYD", "1211.HK")],
            },
        ],
    },
    {
        "key": "auto",
        "name": "자동차",
        "tags": ["자동차"],
        "summary": "부품·전장 → 완성차 → 모빌리티 서비스. 경기민감 대표 업종.",
        "stages": [
            {
                "stage": "업스트림 · 부품/전장",
                "desc": "구동·공조·전장 등 핵심 부품.",
                "domestic": [_d("현대모비스", "012330"), _d("한온시스템", "018880"), _d("HL만도", "204320")],
                "overseas": [_d("Bosch(비상장)"), _d("Aptiv", "APTV")],
            },
            {
                "stage": "미드스트림 · 완성차",
                "desc": "내연·전기차 완성차 제조.",
                "domestic": [_d("현대차", "005380"), _d("기아", "000270")],
                "overseas": [_d("Tesla", "TSLA"), _d("Toyota", "TM"), _d("GM", "GM")],
            },
            {
                "stage": "다운스트림 · 모빌리티/금융",
                "desc": "할부·리스·중고차·모빌리티 서비스.",
                "domestic": [_d("현대글로비스", "086280")],
                "overseas": [_d("Uber", "UBER")],
            },
        ],
    },
    {
        "key": "defense",
        "name": "방산",
        "tags": ["방산"],
        "summary": "소재·부품 → 체계종합(완성무기) → 수출/MRO. 지정학 리스크에 강한 방어·성장 성격.",
        "stages": [
            {
                "stage": "업스트림 · 소재/부품",
                "desc": "특수강·항공소재·전자부품.",
                "domestic": [_d("현대제철", "004020"), _d("풍산", "103140")],
                "overseas": [_d("Howmet Aerospace", "HWM")],
            },
            {
                "stage": "미드스트림 · 체계종합",
                "desc": "항공기·유도무기·지상장비 등 완성 무기체계.",
                "domestic": [_d("한화에어로스페이스", "012450"), _d("한국항공우주", "047810"),
                             _d("LIG넥스원", "079550"), _d("현대로템", "064350")],
                "overseas": [_d("Lockheed Martin", "LMT"), _d("RTX", "RTX"), _d("Northrop Grumman", "NOC")],
            },
            {
                "stage": "다운스트림 · 수출/MRO",
                "desc": "무기 수출과 유지·보수·정비(MRO).",
                "domestic": [_d("한화시스템", "272210")],
                "overseas": [_d("General Dynamics", "GD")],
            },
        ],
    },
    {
        "key": "energy",
        "name": "에너지/원자재",
        "tags": ["에너지/원자재", "소재/화학"],
        "summary": "자원 채굴 → 정제/화학 → 유통. 인플레·경기 후반부에 상대 강세.",
        "stages": [
            {
                "stage": "업스트림 · 자원/채굴",
                "desc": "원유·가스·비철금속 등 원자재 확보.",
                "domestic": [_d("고려아연", "010130"), _d("포스코홀딩스", "005490")],
                "overseas": [_d("ExxonMobil", "XOM"), _d("Freeport-McMoRan", "FCX")],
            },
            {
                "stage": "미드스트림 · 정제/화학",
                "desc": "정유·석유화학 등 소재 가공.",
                "domestic": [_d("S-Oil", "010950"), _d("LG화학", "051910"), _d("롯데케미칼", "011170")],
                "overseas": [_d("Chevron", "CVX"), _d("Dow", "DOW")],
            },
            {
                "stage": "다운스트림 · 유통/발전",
                "desc": "에너지 유통과 발전 연료 공급.",
                "domestic": [_d("GS", "078930"), _d("SK이노베이션", "096770")],
                "overseas": [_d("Shell", "SHEL")],
            },
        ],
    },
    {
        "key": "power_nuclear",
        "name": "전력/원전",
        "tags": ["전력/원전"],
        "summary": "기자재 → 발전·송배전 → 전력 판매. AI 전력수요·원전 르네상스 테마. 방어+성장 혼재.",
        "stages": [
            {
                "stage": "업스트림 · 원전/발전 기자재",
                "desc": "원자로·터빈·중전기기 등 발전 설비.",
                "domestic": [_d("두산에너빌리티", "034020"), _d("한전기술", "052690"), _d("효성중공업", "298040")],
                "overseas": [_d("GE Vernova", "GEV"), _d("Cameco(우라늄)", "CCJ")],
            },
            {
                "stage": "미드스트림 · 송배전/계통",
                "desc": "변압기·전력망 등 송배전 인프라.",
                "domestic": [_d("LS ELECTRIC", "010120"), _d("HD현대일렉트릭", "267260"), _d("대한전선", "001440")],
                "overseas": [_d("Quanta Services", "PWR")],
            },
            {
                "stage": "다운스트림 · 발전/전력판매",
                "desc": "발전 운영과 전력 판매.",
                "domestic": [_d("한국전력", "015760")],
                "overseas": [_d("Constellation Energy", "CEG"), _d("Vistra", "VST")],
            },
        ],
    },
    {
        "key": "bio",
        "name": "헬스케어/바이오",
        "tags": ["헬스케어/바이오"],
        "summary": "원료·CDMO → 신약개발 → 의료 서비스. 금리인하·경기방어 국면에 관심.",
        "stages": [
            {
                "stage": "업스트림 · 원료/CDMO",
                "desc": "바이오의약품 위탁개발·생산(CDMO)과 원료.",
                "domestic": [_d("삼성바이오로직스", "207940")],
                "overseas": [_d("Lonza", "LONN.SW"), _d("Thermo Fisher", "TMO")],
            },
            {
                "stage": "미드스트림 · 신약/바이오시밀러",
                "desc": "신약개발·바이오시밀러.",
                "domestic": [_d("셀트리온", "068270"), _d("유한양행", "000100"),
                             _d("한미약품", "128940"), _d("SK바이오팜", "326030")],
                "overseas": [_d("Eli Lilly", "LLY"), _d("Novo Nordisk", "NVO"), _d("Pfizer", "PFE")],
            },
            {
                "stage": "다운스트림 · 의료기기/서비스",
                "desc": "의료기기·진단·병원 서비스.",
                "domestic": [_d("클래시스", "214150"), _d("루닛", "328130")],
                "overseas": [_d("Intuitive Surgical", "ISRG"), _d("Medtronic", "MDT")],
            },
        ],
    },
    {
        "key": "shipbuilding",
        "name": "조선",
        "tags": ["조선"],
        "summary": "기자재·엔진 → 조선소(건조) → 해운·해양플랜트 수요. 슈퍼사이클·친환경 선박 수혜.",
        "stages": [
            {"stage": "업스트림 · 기자재/엔진", "desc": "선박 엔진·LNG 보냉재 등 핵심 기자재.",
             "domestic": [_d("HD현대마린엔진", "071970"), _d("한화엔진", "082740"), _d("한국카본", "017960")],
             "overseas": [_d("Wärtsilä", "WRT1V.HE")]},
            {"stage": "미드스트림 · 조선소(건조)", "desc": "상선·군함·해양플랜트 건조.",
             "domestic": [_d("HD한국조선해양", "009540"), _d("삼성중공업", "010140"),
                          _d("한화오션", "042660"), _d("HD현대중공업", "329180"), _d("대한조선", "439260")],
             "overseas": [_d("중국 CSSC(비상장)"), _d("일본 Imabari(비상장)")]},
            {"stage": "다운스트림 · 해운/AS", "desc": "해운 운송과 선박 유지·보수(AS).",
             "domestic": [_d("HD현대마린솔루션", "443060"), _d("HMM", "011200"), _d("팬오션", "028670")],
             "overseas": [_d("Maersk", "MAERSK-B.CO")]},
        ],
    },
    {
        "key": "steel",
        "name": "철강·금속",
        "tags": ["철강·금속", "소재/화학"],
        "summary": "원료·제련 → 제강·압연 → 가공·수요(건설·자동차·조선). 경기·인프라 민감.",
        "stages": [
            {"stage": "업스트림 · 원료/비철제련", "desc": "철광석·비철금속 제련.",
             "domestic": [_d("고려아연", "010130")],
             "overseas": [_d("BHP", "BHP"), _d("Rio Tinto", "RIO"), _d("Vale", "VALE")]},
            {"stage": "미드스트림 · 제강/압연", "desc": "고로·전기로 제강, 판재·봉형강.",
             "domestic": [_d("POSCO홀딩스", "005490"), _d("현대제철", "004020")],
             "overseas": [_d("Nucor", "NUE"), _d("ArcelorMittal", "MT")]},
            {"stage": "다운스트림 · 가공/부품", "desc": "신동·특수합금 등 가공제품.",
             "domestic": [_d("풍산", "103140")],
             "overseas": [_d("Freeport-McMoRan", "FCX")]},
        ],
    },
    {
        "key": "chemical",
        "name": "화학",
        "tags": ["화학", "소재/화학"],
        "summary": "기초유분 → 석유화학 → 정밀·소재. 유가·수요 사이클과 2차전지·반도체 소재로 확장.",
        "stages": [
            {"stage": "업스트림 · 기초유분/납사", "desc": "정유·납사분해로 기초원료 생산.",
             "domestic": [_d("S-Oil", "010950"), _d("롯데케미칼", "011170")],
             "overseas": [_d("ExxonMobil Chemical", "XOM")]},
            {"stage": "미드스트림 · 석유화학", "desc": "합성수지·고무·기초화학제품.",
             "domestic": [_d("LG화학", "051910"), _d("금호석유화학", "011780"), _d("한화솔루션", "009830")],
             "overseas": [_d("BASF", "BAS.DE"), _d("Dow", "DOW")]},
            {"stage": "다운스트림 · 정밀/소재", "desc": "반도체·2차전지·산업소재 등 정밀화학.",
             "domestic": [_d("한솔케미칼", "014680"), _d("코오롱인더", "120110"),
                          _d("이수스페셜티케미컬", "457190"), _d("OCI홀딩스", "010060")],
             "overseas": [_d("Covestro", "1COV.DE")]},
        ],
    },
    {
        "key": "telecom",
        "name": "통신",
        "tags": ["통신"],
        "summary": "네트워크 장비 → 통신사(인프라) → 콘텐츠·클라우드. 안정적 배당·AI 데이터 트래픽 수혜.",
        "stages": [
            {"stage": "업스트림 · 네트워크 장비", "desc": "기지국·통신 장비.",
             "domestic": [_d("삼성전자", "005930")],
             "overseas": [_d("Ericsson", "ERIC"), _d("Nokia", "NOK")]},
            {"stage": "미드스트림 · 통신사", "desc": "이동통신·유선·기업통신 서비스.",
             "domestic": [_d("SK텔레콤", "017670"), _d("KT", "030200"), _d("LG유플러스", "032640")],
             "overseas": [_d("Verizon", "VZ"), _d("AT&T", "T")]},
            {"stage": "다운스트림 · 콘텐츠/클라우드", "desc": "미디어·IDC·클라우드로 트래픽 수익화.",
             "domestic": [_d("삼성에스디에스", "018260")],
             "overseas": [_d("American Tower", "AMT")]},
        ],
    },
    {
        "key": "cosmetics",
        "name": "화장품",
        "tags": ["화장품"],
        "summary": "원료·ODM → 브랜드 → 디바이스·유통. K-뷰티 수출·인디브랜드 성장.",
        "stages": [
            {"stage": "업스트림 · 원료/ODM", "desc": "제조자개발생산(ODM)·원료.",
             "domestic": [_d("한국콜마", "161890"), _d("코스맥스", "192820")],
             "overseas": [_d("Givaudan", "GIVN.SW")]},
            {"stage": "미드스트림 · 브랜드", "desc": "화장품 브랜드·제조.",
             "domestic": [_d("아모레퍼시픽", "090430"), _d("LG생활건강", "051900"), _d("아모레퍼시픽홀딩스", "002790")],
             "overseas": [_d("L'Oréal", "OR.PA"), _d("Estée Lauder", "EL")]},
            {"stage": "다운스트림 · 디바이스/신흥브랜드", "desc": "뷰티 디바이스·신흥 인디브랜드.",
             "domestic": [_d("에이피알", "278470"), _d("달바글로벌", "483650")],
             "overseas": [_d("e.l.f. Beauty", "ELF")]},
        ],
    },
    {
        "key": "retail",
        "name": "유통·리테일",
        "tags": ["유통·리테일", "필수소비재"],
        "summary": "제조·소싱 → 대형유통 → 편의점·이커머스. 내수·소비경기 바로미터.",
        "stages": [
            {"stage": "업스트림 · 제조/소싱", "desc": "식품·소비재 제조와 소싱.",
             "domestic": [_d("CJ제일제당", "097950")],
             "overseas": [_d("Procter & Gamble", "PG")]},
            {"stage": "미드스트림 · 대형유통", "desc": "대형마트·백화점.",
             "domestic": [_d("이마트", "139480"), _d("롯데쇼핑", "023530"),
                          _d("신세계", "004170"), _d("현대백화점", "069960")],
             "overseas": [_d("Walmart", "WMT"), _d("Costco", "COST")]},
            {"stage": "다운스트림 · 편의점/이커머스", "desc": "편의점·온라인 유통·렌탈.",
             "domestic": [_d("BGF리테일", "282330"), _d("GS리테일", "007070"), _d("코웨이", "021240")],
             "overseas": [_d("Amazon", "AMZN")]},
        ],
    },
    {
        "key": "game",
        "name": "게임",
        "tags": ["게임", "IT/인터넷"],
        "summary": "엔진·인프라 → 개발 → 퍼블리싱·플랫폼. 신작·글로벌 흥행에 실적 레버리지.",
        "stages": [
            {"stage": "업스트림 · 엔진/인프라", "desc": "게임엔진·GPU·클라우드 인프라.",
             "domestic": [_d("삼성에스디에스", "018260")],
             "overseas": [_d("NVIDIA", "NVDA"), _d("Unity", "U")]},
            {"stage": "미드스트림 · 개발", "desc": "게임 개발·자체 IP.",
             "domestic": [_d("크래프톤", "259960"), _d("NC", "036570"),
                          _d("넷마블", "251270"), _d("시프트업", "462870")],
             "overseas": [_d("Tencent", "0700.HK"), _d("Nintendo", "7974.T")]},
            {"stage": "다운스트림 · 퍼블리싱/플랫폼", "desc": "퍼블리싱·유통 플랫폼.",
             "domestic": [_d("더블유게임즈", "192080")],
             "overseas": [_d("Roblox", "RBLX")]},
        ],
    },
    {
        "key": "entertainment",
        "name": "엔터·미디어",
        "tags": ["엔터·미디어"],
        "summary": "제작 → 기획사(IP) → 광고·플랫폼. K-콘텐츠 글로벌 팬덤·IP 수익화.",
        "stages": [
            {"stage": "업스트림 · 제작/음원", "desc": "콘텐츠 제작·음원 유통.",
             "domestic": [_d("하이브", "352820")],
             "overseas": [_d("Universal Music", "UMG.AS")]},
            {"stage": "미드스트림 · 기획/IP", "desc": "아티스트·IP 기획·매니지먼트.",
             "domestic": [_d("하이브", "352820")],
             "overseas": [_d("Live Nation", "LYV")]},
            {"stage": "다운스트림 · 광고/플랫폼", "desc": "광고·스트리밍 플랫폼 수익화.",
             "domestic": [_d("제일기획", "030000")],
             "overseas": [_d("Netflix", "NFLX"), _d("Spotify", "SPOT")]},
        ],
    },
    {
        "key": "robotics",
        "name": "로봇",
        "tags": ["로봇"],
        "summary": "부품·감속기 → 로봇 제조 → 자동화 응용. 고령화·인건비·AI로 구조적 성장.",
        "stages": [
            {"stage": "업스트림 · 부품/공작기계", "desc": "감속기·모터·공작기계.",
             "domestic": [_d("현대위아", "011210")],
             "overseas": [_d("Harmonic Drive", "6324.T"), _d("Fanuc", "6954.T")]},
            {"stage": "미드스트림 · 로봇 제조", "desc": "협동로봇·산업로봇.",
             "domestic": [_d("두산로보틱스", "454910")],
             "overseas": [_d("ABB", "ABBNY"), _d("Intuitive Surgical", "ISRG")]},
            {"stage": "다운스트림 · 자동화 응용", "desc": "스마트팩토리·물류 자동화.",
             "domestic": [_d("포스코DX", "022100")],
             "overseas": [_d("Rockwell Automation", "ROK")]},
        ],
    },
    {
        "key": "airline",
        "name": "항공·여행",
        "tags": ["항공", "여행"],
        "summary": "항공기·MRO → 항공사 → 여행·면세. 유가·환율·리오프닝(여행수요) 민감.",
        "stages": [
            {"stage": "업스트림 · 항공기/MRO", "desc": "항공기 부품·정비(MRO).",
             "domestic": [_d("한국항공우주", "047810")],
             "overseas": [_d("Boeing", "BA"), _d("Airbus", "AIR.PA")]},
            {"stage": "미드스트림 · 항공사", "desc": "여객·화물 항공 운송.",
             "domestic": [_d("대한항공", "003490"), _d("아시아나항공", "020150"), _d("한진칼", "180640")],
             "overseas": [_d("Delta", "DAL"), _d("United", "UAL")]},
            {"stage": "다운스트림 · 여행/면세", "desc": "여행·호텔·면세 소비.",
             "domestic": [_d("호텔신라", "008770")],
             "overseas": [_d("Booking", "BKNG")]},
        ],
    },
    {
        "key": "construction",
        "name": "건설",
        "tags": ["건설", "산업재/기계"],
        "summary": "건자재 → 시공(주택·플랜트) → 부동산·인프라. 금리·분양·해외수주 사이클.",
        "stages": [
            {"stage": "업스트림 · 건자재", "desc": "시멘트·도료·건축자재.",
             "domestic": [_d("KCC", "002380")],
             "overseas": [_d("Caterpillar", "CAT")]},
            {"stage": "미드스트림 · 시공/플랜트", "desc": "주택·토목·플랜트 시공.",
             "domestic": [_d("현대건설", "000720"), _d("GS건설", "006360"),
                          _d("DL이앤씨", "375500"), _d("삼성E&A", "028050")],
             "overseas": [_d("Vinci", "DG.PA")]},
            {"stage": "다운스트림 · 부동산/인프라", "desc": "리츠·인프라 운용.",
             "domestic": [_d("SK리츠", "395400"), _d("맥쿼리인프라", "088980")],
             "overseas": [_d("Prologis", "PLD")]},
        ],
    },
    {
        "key": "finance",
        "name": "금융",
        "tags": ["은행·금융", "은행/금융", "금융"],
        "summary": "은행 → 증권·보험 → 핀테크·카드. 금리·자본시장 사이클, 밸류업(주주환원) 테마.",
        "stages": [
            {"stage": "업스트림 · 은행/지주", "desc": "은행·금융지주.",
             "domestic": [_d("KB금융", "105560"), _d("신한지주", "055550"),
                          _d("하나금융지주", "086790"), _d("우리금융지주", "316140")],
             "overseas": [_d("JPMorgan", "JPM")]},
            {"stage": "미드스트림 · 증권/보험", "desc": "증권·생명·손해보험.",
             "domestic": [_d("미래에셋증권", "006800"), _d("삼성생명", "032830"),
                          _d("삼성화재", "000810"), _d("DB손해보험", "005830")],
             "overseas": [_d("Goldman Sachs", "GS"), _d("Berkshire", "BRK-B")]},
            {"stage": "다운스트림 · 카드/핀테크", "desc": "카드·간편결제·인터넷은행.",
             "domestic": [_d("삼성카드", "029780"), _d("카카오뱅크", "323410"), _d("카카오페이", "377300")],
             "overseas": [_d("Visa", "V"), _d("PayPal", "PYPL")]},
        ],
    },
]

# GICS 11 섹터(한국어 약칭)로 각 밸류체인 테마를 상위 분류 — 시그널 리스트 '섹터' 컬럼 표준.
# 밸류체인 테마(예: 반도체)는 더 세분화된 개념이라, 대표 GICS 섹터 하나로 매핑한다(근사).
_GICS = {
    "semiconductor": "IT",
    "ai_datacenter": "IT",
    "battery": "IT",
    "auto": "경기소비재",
    "defense": "산업재",
    "energy": "에너지",
    "power_nuclear": "유틸리티",
    "bio": "헬스케어",
}
for _s in SECTORS:
    _s["gics"] = _GICS.get(_s["key"])

_BY_KEY = {s["key"]: s for s in SECTORS}


def sectors() -> list[dict]:
    return SECTORS


def sector(key: str) -> dict | None:
    return _BY_KEY.get(key)


def key_for_tag(tag: str) -> str | None:
    """사이클의 주도섹터 이름(예: '반도체', 'IT/인터넷')을 밸류체인 섹터 key로 매핑. 없으면 None.
    '/'와 '·' 표기 차이를 흡수한다(은행/금융 ↔ 은행·금융)."""
    if not tag:
        return None
    aliases = {tag, tag.replace("/", "·"), tag.replace("·", "/")}
    for s in SECTORS:
        tags = s["tags"]
        if any(a in tags for a in aliases):
            return s["key"]
    return None


def tickers_for_lead_tags(tags: list[str], limit: int = 12) -> list[dict]:
    """사이클 lead_sectors 태그 → 밸류체인 국내 대표 티커(중복 제거, 순서 유지).

    반환: [{"ticker", "name", "tag", "vc_key"}, ...] — KB 우선 수집 타깃용.
    """
    out, seen = [], set()
    for tag in tags or []:
        vk = key_for_tag(tag)
        sec = _BY_KEY.get(vk) if vk else None
        if not sec:
            continue
        for st in sec.get("stages") or []:
            for c in st.get("domestic") or []:
                tk = c.get("ticker")
                if not tk or tk in seen:
                    continue
                seen.add(tk)
                out.append({"ticker": tk, "name": c.get("name") or tk,
                            "tag": tag, "vc_key": vk})
                if len(out) >= limit:
                    return out
    return out


def company_position(ticker: str) -> dict | None:
    """국내 티커의 밸류체인 포지셔닝: {sector, stage, stage_desc}. 큐레이션에 없으면 None.
    시그널 탭에서 '뭐하는 기업인지' 정적 소개(사실 기반, 환각 없음)로 재활용한다."""
    for s in SECTORS:
        for st in s["stages"]:
            if any(c.get("ticker") == ticker for c in st["domestic"]):
                return {"sector": s["name"], "gics": s.get("gics"), "stage": st["stage"], "stage_desc": st["desc"]}
    return None
