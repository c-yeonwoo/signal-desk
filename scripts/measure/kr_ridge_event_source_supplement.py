"""Opt-in SIG-21 pinned three-event adapter; no collection or market rewriting."""
from __future__ import annotations
from dataclasses import dataclass
import datetime as dt
from io import BytesIO
from pathlib import Path
import re
import subprocess
from zipfile import ZipFile, BadZipFile
# Use the byte-identical Hyundai dependency for strict JSON and Korean HTML only.
import importlib.util
_s = importlib.util.spec_from_file_location('event_original_parser', Path(__file__).with_name('kr_ridge_hold_source_supplement.py'))
_h = importlib.util.module_from_spec(_s)
_s.loader.exec_module(_h)
sha, text, decode_json = _h.sha, _h.text, _h.decode_json
ARCHIVES = {'data/research/celltrion-kia-source-review-2026-10-18-v1.zip': ('ba0be13df6d23e5575d97d2e06a6cef1d0bb6911bc58cbd06b11bef5f416bb82', 52), 'data/research/celltrion-adjacent-cancellation-originals-2026-10-18-v1.zip': ('9a54336d98b4f3b19d9b1a0a0b6a5e7601c31cd49c007f9271db4bb75d1ab848', 8)}
MEMBERS = {'data/research/celltrion-kia-source-review-2026-10-18-v1.zip': ['manifest.json', '01-cell-list.bin', '01-payload.txt', '02-kia-list.bin', '02-payload.txt', '03-cell-listing-viewer.bin', '04-cell-cancel-viewer.bin', '05-kia-listing-viewer.bin', '06-cell-merger-viewer.bin', '07-cell-listing-resolver.bin', '08-cell-cancel-resolver.bin', '09-kia-listing-resolver.bin', '10-cell-merger-resolver.bin', '11-cell-listing-original.bin', '11-cell-listing-original.txt', '12-cell-cancel-original.bin', '12-cell-cancel-original.txt', '13-kia-listing-original.bin', '13-kia-listing-original.txt', '14-cell-merger-original.bin', '14-cell-merger-original.txt', '15-kia-decision-list.bin', '15-payload.txt', '16-kia-decision-viewer.bin', '17-cell-correction-list.bin', '17-payload.txt', '18-kia-correction-list.bin', '18-payload.txt', '19-kia-decision-resolver.bin', '20-cell-correction-valid-list.bin', '20-payload.txt', '21-kia-decision-original.bin', '21-kia-decision-original.txt', '22-cell-decision-viewer.bin', '23-cell-decision-resolver.bin', '24-cell-decision-original.bin', '24-cell-decision-original.txt', 'adjudicated-fact-proposals.json', 'build_report.py', 'cell-correction-form.txt', 'cell-correction-valid-form.txt', 'cell-form.txt', 'fetch.py', 'input-manifest.json', 'kia-correction-form.txt', 'kia-decision-form.txt', 'kia-form.txt', 'krx-preserved-extract.json', 'mission.md', 'prior-review-check.json', 'report.md', 'requests.json'], 'data/research/celltrion-adjacent-cancellation-originals-2026-10-18-v1.zip': ['01-source-list.htm', 'query-payload.txt', '02-viewer.htm', '03-resolver.htm', '04-original.htm', 'original-text.txt', 'summary.json', 'requests.json']}
SOURCE_SHA = {'11-cell-listing-original.bin': 'd7ec5ebbd6646341287a8e24a0ff080371435922c4cc146a8c019a2db85c499c', '12-cell-cancel-original.bin': '1d6af7eade90e559506204de7f937ff1beff3fe244dcab69af95371ce676b204', '13-kia-listing-original.bin': '32768fea1c6982baae11784330540752199097885f80f7e0157a779708febc55', '14-cell-merger-original.bin': '2cc67a1c5db4ffea6766a763e3ac8b226f5418338776c66329e33d3c593281ca', '21-kia-decision-original.bin': '154ed36f776fa5ba0fb941526f7fc4d70aabb99f0c0ff49217629ee2a9197727', '24-cell-decision-original.bin': 'b6435fdb995fb11a1fff30686e21f85cf0aae351de6fc5bf062cd37aa336ffab', '04-original.htm': 'a1d8d2b79c015d377aba6d72f7ac31b1999111feb27a1690091cdc612b97b4a1'}
PINNED_FACTS = [{'share_class': 'ordinary_common', 'holder_quantity_effect': 'unchanged', 'holder_quantity_multiplier': 1, 'price_unit_multiplier': 1, 'mechanical_price_adjustment_required': 'no_for_this_event_only', 'coverage_state': 'partial', 'observation_mode': 'retrospective', 'historical_capture_verified': False, 'execution_input_activated': False, 'adoption_claim': False, 'other_corporate_actions_or_dividends_certified': False, 'event_key': 'preserved-raw:2024-01-12:068270', 'issuer': '(주)셀트리온', 'ticker': '068270', 'security_id': 'KR7068270008', 'kind': 'merger_additional_listing_surviving_issuer', 'effective_on': '2023-12-28', 'issuance_date': '2023-12-28', 'listing_session': '2024-01-12', 'previous_session': '2024-01-11', 'listed_shares_before': 146402770, 'listed_shares_after': 220290520, 'listed_share_delta': 73887750, 'par_value_KRW': 1000, 'issuance_value_KRW': 148853, 'holder_effect_basis': '존속법인 기존 보통주를 계속 보유하는 외부주주. 신주 배정은 셀트리온헬스케어 보유분에 한정. 원문 지분표의 셀트리온만 보유한 주주 수량도 동일. 추가상장 수량/총수비율을 기존보유수량에 곱하지 않음.', 'event_cash_or_rights_for_external_holder': 'no_automatic_merger_distribution_for_continuing_survivor_share_only; statutory_appraisal_rights_were_conditional_and_separate', 'excluded_holder_cases': ['소멸회사 셀트리온헬스케어 주식 1주당 셀트리온0.4492620주', '그 신주배정 단주는 상장일 종가 기준 1개월 내 현금 지급 예정', '합병반대 주식매수청구 행사자 및 미완료 가격협의는 별도 계좌정산 필요'], 'merger_allocation_record_date': '2023-12-19', 'merger_share_consolidation_reference_date': '2023-12-20', 'date_limit': '결과보고서는 전자증권 일반 주석에서 병합기준일 신주효력 발생을 언급. 이 필드들을 합병기일/발행일/상장일과 혼동하지 않음. 법적 효력 시점 전체에 관한 별도 판단 아님.', 'trading_halt_evidence': '회사합병 결정의 존속회사 매매거래 정지예정기간 시작/종료는 모두 dash. 전체 거래정지 이력 부재 인증 아님.', 'separate_adjacent_event': {'listing_session': '2024-01-15', 'cancellation_date': '2024-01-04', 'shares_before': 220290520, 'shares_after': 217980707, 'delta': -2309813, 'scope': '현재키에 합산하거나 자동 승인하지 않음'}, 'correction_scope': '2023-12-01~2024-01-31 전체53건, 2023-08-01~2026-07-31 합병 제목7건. 2023-08-29/09-01/09-12 증권신고서 정정 존재. 해당 정정 본문 미수집. 채택 근거는 이후 발행실적 및 완료상장과 직접 대조한 수량/교부조건. 2026-08-01~10-18 및 전매체 전체정정 완전성 미확인. 각 보존 viewer 선택 본문1건.', 'sources': [{'role': 'completed_additional_listing', 'receipt_id': '20240109000087', 'body_id': '20240109000227', 'publication_at_displayed_KST': '2024-01-09T17:09:00+09:00', 'observed_at': '2026-10-18T08:54:36.541138+00:00', 'url': 'https://kind.krx.co.kr/external/2024/01/09/000087/20240109000227/68154.htm', 'path': '11-cell-listing-original.bin', 'sha256': 'd7ec5ebbd6646341287a8e24a0ff080371435922c4cc146a8c019a2db85c499c'}, {'role': 'merger_issuance_result', 'receipt_id': '20231228000770', 'body_id': '20231228001664', 'publication_at_displayed_KST': '2023-12-28T16:23:00+09:00', 'observed_at': '2026-10-18T08:54:38.014970+00:00', 'url': 'https://kind.krx.co.kr/external/2023/12/28/000770/20231228001664/10810.htm', 'path': '14-cell-merger-original.bin', 'sha256': '2cc67a1c5db4ffea6766a763e3ac8b226f5418338776c66329e33d3c593281ca'}, {'role': 'merger_decision', 'receipt_id': '20230817000227', 'body_id': '20230817000684', 'publication_at_displayed_KST': '2023-08-17T15:33:00+09:00', 'observed_at': '2026-10-18T08:55:51.272738+00:00', 'url': 'https://kind.krx.co.kr/external/2023/08/17/000227/20230817000684/11344.htm', 'path': '24-cell-decision-original.bin', 'sha256': 'b6435fdb995fb11a1fff30686e21f85cf0aae351de6fc5bf062cd37aa336ffab'}, {'role': 'adjacent_separate_cancellation_listing', 'receipt_id': '20240110000096', 'body_id': '20240110000239', 'publication_at_displayed_KST': '2024-01-10T17:02:00+09:00', 'observed_at': '2026-10-18T08:54:36.948176+00:00', 'url': 'https://kind.krx.co.kr/external/2024/01/10/000096/20240110000239/68155.htm', 'path': '12-cell-cancel-original.bin', 'sha256': '1d6af7eade90e559506204de7f937ff1beff3fe244dcab69af95371ce676b204'}]}, {'share_class': 'ordinary_common', 'holder_quantity_effect': 'unchanged', 'holder_quantity_multiplier': 1, 'price_unit_multiplier': 1, 'mechanical_price_adjustment_required': 'no_for_this_event_only', 'coverage_state': 'partial', 'observation_mode': 'retrospective', 'historical_capture_verified': False, 'execution_input_activated': False, 'adoption_claim': False, 'other_corporate_actions_or_dividends_certified': False, 'event_key': 'preserved-raw:2024-05-28:000270', 'issuer': '기아(주)', 'ticker': '000270', 'security_id': 'KR7000270009', 'kind': 'treasury_cancellation', 'effective_on': '2024-05-10', 'listing_session': '2024-05-28', 'previous_session': '2024-05-27', 'listed_shares_before': 402044203, 'listed_shares_after': 399858417, 'listed_share_delta': -2185786, 'par_value_KRW': 5000, 'holder_effect_basis': '기취득 자기주식만 배당가능이익 범위에서 소각, 자본금 감소 없음. 외부 기존주식 소각/교환 배율 아님.', 'event_cash_or_rights_for_external_holder': 'none_inferred_for_this_treasury_cancellation_only', 'cancellation_book_value_KRW': 191608105235, 'correction_scope': '2024-01-01~2024-05-31 소각 제목2건, ~2026-10-18 소각 제목8건. 관련 두건에 정정 표시 없음. 양 viewer 본문1건. 전체 매체/취소/정정 완전성 및 당시 바이트/PIT 미확인.', 'sources': [{'role': 'completed_cancellation_listing', 'receipt_id': '20240523000020', 'body_id': '20240523000038', 'publication_at_displayed_KST': '2024-05-23T17:02:00+09:00', 'observed_at': '2026-10-18T08:54:37.281959+00:00', 'url': 'https://kind.krx.co.kr/external/2024/05/23/000020/20240523000038/68155.htm', 'path': '13-kia-listing-original.bin', 'sha256': '32768fea1c6982baae11784330540752199097885f80f7e0157a779708febc55'}, {'role': 'treasury_cancellation_decision', 'receipt_id': '20240426000511', 'body_id': '20240425001098', 'publication_at_displayed_KST': '2024-04-26T16:02:00+09:00', 'observed_at': '2026-10-18T08:55:24.074429+00:00', 'url': 'https://kind.krx.co.kr/external/2024/04/26/000511/20240425001098/61131.htm', 'path': '21-kia-decision-original.bin', 'sha256': '154ed36f776fa5ba0fb941526f7fc4d70aabb99f0c0ff49217629ee2a9197727'}]}]
ADJACENT = {'schema': 'celltrion-adjacent-treasury-cancellation-review-v1', 'ticker': '068270', 'security_id': 'KR7068270008', 'share_class': 'ordinary_common', 'kind': 'treasury_cancellation', 'effective_on': '2024-01-04', 'listing_session': '2024-01-15', 'previous_session': '2024-01-12', 'listed_shares_before': 220290520, 'listed_shares_after': 217980707, 'cancelled_shares': 2309813, 'holder_quantity_effect': 'unchanged', 'holder_quantity_multiplier': 1, 'price_unit_multiplier': 1, 'coverage_state': 'partial', 'observation_mode': 'retrospective', 'historical_capture_verified': False, 'source_review_by': 'controller; independent QA required before activation', 'decision': {'source_url': 'https://kind.krx.co.kr/external/2023/10/23/000128/20231012001477/61131.htm', 'receipt_id': '20231023000128', 'body_id': '20231012001477', 'publication_at_displayed_KST': '2023-10-23T12:07:00+09:00', 'observed_at': '2026-10-18T08:59:30.798842+00:00', 'path': '04-original.htm', 'sha256': 'a1d8d2b79c015d377aba6d72f7ac31b1999111feb27a1690091cdc612b97b4a1', 'treasury_acquisition': '기취득 자기주식', 'condition': '셀트리온-셀트리온헬스케어 합병 효력 발생을 조건으로 함; 2023-12-28 발행실적+2024-01-12 실제 추가상장으로 완료대조', 'capital_reduction': False, 'book_value_KRW': 359880414465}, 'listing': {'source_url': 'https://kind.krx.co.kr/external/2024/01/10/000096/20240110000239/68155.htm', 'receipt_id': '20240110000096', 'body_id': '20240110000239', 'publication_at_displayed_KST': '2024-01-10T17:02:00+09:00', 'path': '12-cell-cancel-original.bin', 'sha256': '1d6af7eade90e559506204de7f937ff1beff3fe244dcab69af95371ce676b204'}, 'limitations': ['이번 소각만 기존 외부 보통주 수량·가격단위 불변으로 해석; 별도 배당/권리 없음 인증 아님', '당시 PIT/원문 바이트 보존·전 정정 완전성 미인증', '다른 날짜/회사의 소각 또는 합병에 일반화하지 않음'], 'collection': {'previous_action_requests': 305, 'new_requests': 4, 'cumulative_action_requests': 309, 'joint_case_requests': 28, 'joint_case_request_limit': 30}}

CODE_FILES = ('scripts/measure/kr_ridge_hold_renewal_diagnostic.py',
              'scripts/measure/kr_ridge_hold_source_supplement.py',
              'scripts/measure/kr_ridge_event_source_supplement.py',
              'tests/test_kr_ridge_hold_renewal_diagnostic.py')
PROPOSAL_PATH = 'docs/kr-r3-celltrion-adjacent-fact-2026-10-18.json'
PROPOSAL_SHA = '382dcb86aeadc17dd9aa15d2b01298f222c61d66eb15cbfbbf09d5f0a02ea500'
FACTS_SHA = '72dc04b9a771fcc4a8dc6120112c0580b89f52c5eb72e274346c5d8e86c8753a'
MANIFEST_SHA = '221d7f5fa1a5d34ea2b0b0e6857a3a6c6e96473a89d04ffa822af88301bfb59a'


def local_file(root, name, maximum):
    # Only literal allowlisted repository paths may be read by this adapter.
    if name not in {*ARCHIVES, PROPOSAL_PATH}:
        raise ValueError('event_path_not_allowed')
    root = Path(root).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or path.stat().st_size > maximum:
        raise ValueError('event_path_or_size')
    return path.read_bytes()


def read_evidence(root):
    packs = []
    for name, (expected, count) in ARCHIVES.items():
        raw = local_file(root, name, 20*1024*1024)
        if sha(raw) != expected:
            raise ValueError('event_archive_hash')
        try:
            with ZipFile(BytesIO(raw)) as archive:
                infos = archive.infolist()
                if (len(infos) != count or len({i.filename for i in infos}) != count
                        or set(archive.namelist()) != set(MEMBERS[name])
                        or sum(i.file_size for i in infos) > 20*1024*1024
                        or any(i.is_dir() or i.file_size > 200000 or i.flag_bits & 1
                               or Path(i.filename).name != i.filename for i in infos)):
                    raise ValueError('event_zip_members_or_size')
                # Inspect every member but only read the evidence members below.
                needed = ({*SOURCE_SHA, 'manifest.json', 'adjudicated-fact-proposals.json'}
                          & set(archive.namelist()))
                packs.append({n: archive.read(n) for n in needed})
        except BadZipFile as exc:
            raise ValueError('event_invalid_zip') from exc
    files = {k: v for pack in packs for k, v in pack.items()}
    if sha(files['manifest.json']) != MANIFEST_SHA or sha(files['adjudicated-fact-proposals.json']) != FACTS_SHA:
        raise ValueError('event_manifest_or_facts_hash')
    proposal = local_file(root, PROPOSAL_PATH, 200000)
    if sha(proposal) != PROPOSAL_SHA:
        raise ValueError('event_proposal_hash')
    manifest = decode_json(files['manifest.json'])
    facts = decode_json(files['adjudicated-fact-proposals.json'])['facts']
    adjacent = decode_json(proposal)
    if facts != PINNED_FACTS or adjacent != ADJACENT:
        raise ValueError('event_fact_fields')
    for name, expected in SOURCE_SHA.items():
        if sha(files[name]) != expected:
            raise ValueError('event_direct_source_hash')
        if name != '04-original.htm':
            entries = [v for v in manifest['files'] if v['path'] == name]
            if len(entries) != 1 or entries[0]['sha256'] != expected or entries[0]['bytes'] != len(files[name]):
                raise ValueError('event_manifest_source')
    # Pinned proposals preserve publication, observation, correction and holder limits.
    for f in facts:
        if (f['coverage_state'] != 'partial' or f['observation_mode'] != 'retrospective'
                or f['historical_capture_verified'] is not False or not f['correction_scope']
                or type(f['holder_quantity_multiplier']) is not int or f['holder_quantity_multiplier'] != 1
                or type(f['price_unit_multiplier']) is not int or f['price_unit_multiplier'] != 1):
            raise ValueError('event_scope_or_multiplier')
        for source in f['sources']:
            published = dt.datetime.fromisoformat(source['publication_at_displayed_KST'])
            observed = dt.datetime.fromisoformat(source['observed_at'])
            if (published.tzinfo is None or observed.tzinfo is None or published > observed
                    or published.date().isoformat() > source_session(source, f)
                    or SOURCE_SHA[source['path']] != source['sha256']
                    or not source['url'].startswith('https://kind.krx.co.kr/external/')):
                raise ValueError('event_source_clock_or_identity')
    published = dt.datetime.fromisoformat(adjacent['decision']['publication_at_displayed_KST'])
    observed = dt.datetime.fromisoformat(adjacent['decision']['observed_at'])
    if published > observed or published.date().isoformat() > adjacent['effective_on']:
        raise ValueError('event_adjacent_clock')
    validate_bodies(files)
    return facts, adjacent, files


def source_session(source, fact):
    return ('2024-01-15' if source['role'] == 'adjacent_separate_cancellation_listing'
            else fact['listing_session'])


def require(pattern, body):
    if re.search(pattern, body) is None:
        raise ValueError('event_body_semantics:' + pattern)


def validate_bodies(files):
    bodies = {n: text(raw) for n, raw in files.items() if n in SOURCE_SHA}
    listing = bodies['11-cell-listing-original.bin']
    for p in (r'셀트리온.*보통주.*추가상장', r'보통주\s*73,887,750주',
              r'증자방법\s*:\s*합병', r'발행일\s*:\s*2023년\s*12월\s*28일',
              r'상장일\s*:\s*2024년\s*01월\s*12일', r'KR7068270008\s*\(단축코드\s*:\s*A068270\)'):
        require(p, listing)
    merger = bodies['14-cell-merger-original.bin']
    decision = bodies['24-cell-decision-original.bin']
    for p in (r'셀트리온\s*\(존속회사\)', r'셀트리온헬스케어\s*\(소멸회사\)',
              r'합병기일\s*2023년\s*12월\s*28일', r'합병신주\s*보통주.*73,887,750',
              r'총발행주식수\s*보통주\s*146,402,770.*220,290,520'):
        require(p, merger)
    # The exchange ratio belongs to the disappearing company's holders only.
    require(r'셀트리온헬스케어\s*주주명부.*1주당.*셀트리온\s*보통주식\s*0\.4492620주', decision)
    require(r'합병신주의\s*종류와\s*수\(주\)\s*보통주식\s*73,887,750', decision)
    for name, issuer, isin, ticker, before, after, qty, effective, listed, dec in (
        ('12-cell-cancel-original.bin', '셀트리온', 'KR7068270008', '068270', '220,290,520', '217,980,707', '2,309,813', ('2024','01','04'), ('2024','01','15'), '04-original.htm'),
        ('13-kia-listing-original.bin', '기아', 'KR7000270009', '000270', '402,044,203', '399,858,417', '2,185,786', ('2024','05','10'), ('2024','05','28'), '21-kia-decision-original.bin')):
        body, d = bodies[name], bodies[dec]
        for p in (issuer+r'.*주식소각', isin+r'\s*\(단축코드\s*:\s*A'+ticker+r'\)',
                  r'기명식\s*보통주\s*'+before+r'주\s*→\s*'+after+r'주',
                  r'변경주식수\s*:\s*-'+qty+r'주',
                  r'발행일\(소각일\).*?'+effective[0]+r'년\s*'+effective[1]+r'월\s*'+effective[2]+r'일',
                  r'변경상장일\s*:\s*'+listed[0]+r'년\s*'+listed[1]+r'월\s*'+listed[2]+r'일'):
            require(p, body)
        section = re.search(r'1\.\s*소각할\s*주식의\s*종류와\s*수(.*?)2\.\s*발행주식\s*총수', d)
        if section is None:
            raise ValueError('event_decision_section')
        require(r'보통주식\s*\(주\)\s*'+qty, section[1])
        require(r'기취득\s*자기주식', d)
        require(r'자본금의\s*감소는\s*없', d)
        require(r'7\.\s*소각\s*예정일\s*'+'-'.join(effective)+r'\s*8\.', d)
    require(r'합병.*효력이\s*발생하는\s*것을\s*조건', bodies['04-original.htm'])
    # No comparison of the October decision's total to January's pre-cancel total:
    # the intervening merger issuance is independently verified above.


@dataclass(frozen=True)
class MergerSurvivorIssuance:
    ticker: str
    security_id: str
    share_class: str
    kind: str
    effective_on: str
    listing_session: str
    previous_session: str
    listed_shares_before: int
    listed_shares_after: int
    source_url: str
    source_sha256: str
    source_filing_date: str
    source_observed_at: dt.datetime
    source_span: str
    holder_quantity_effect: str = 'unchanged'
    coverage_state: str = 'partial'

    def __post_init__(self):
        f = PINNED_FACTS[0]
        for k in ('ticker','security_id','share_class','effective_on','listing_session','previous_session',
                  'listed_shares_before','listed_shares_after','holder_quantity_effect','coverage_state'):
            if type(getattr(self, k)) is not type(f[k]) or getattr(self, k) != f[k]:
                raise ValueError('event_merger_not_pinned')
        s = f['sources'][0]
        if (self.kind != 'merger_survivor_issuance' or self.source_url != s['url']
                or self.source_sha256 != s['sha256'] or self.source_filing_date != s['publication_at_displayed_KST'][:10]
                or self.source_observed_at != dt.datetime.fromisoformat(s['observed_at']) or not self.source_span):
            raise ValueError('event_merger_provenance')


def merge(root, reviewed, by_day, panel, fact_type):
    facts, adjacent, _ = read_evidence(root)
    result = dict(reviewed)
    all_facts = [facts[0], adjacent, facts[1]]
    for f in all_facts:
        day, ticker = f['listing_session'], f['ticker']
        key = (day, ticker)
        if key in result:
            raise ValueError('event_duplicate_or_conflict')
        index = panel.index[day]
        if index < 1 or panel.sessions[index-1] != f['previous_session']:
            raise ValueError('event_previous_session')
        for session, field in ((f['previous_session'], 'listed_shares_before'), (day, 'listed_shares_after')):
            value = by_day[session][ticker]['LIST_SHRS']
            if (not isinstance(value, str) or not re.fullmatch(r'\d{1,3}(?:,\d{3})*|\d+', value)
                    or int(value.replace(',', '')) != f[field]):
                raise ValueError('event_KRX_integer_mapping')
        source = f['sources'][0] if 'sources' in f else f['listing']
        source_observed = source.get('observed_at', facts[0]['sources'][3]['observed_at'])
        cls = MergerSurvivorIssuance if f is facts[0] else fact_type
        values = {k: f[k] for k in ('ticker','security_id','share_class','effective_on','listing_session',
                                   'previous_session','listed_shares_before','listed_shares_after')}
        result[key] = cls(**values, kind='merger_survivor_issuance' if f is facts[0] else 'treasury_cancellation',
            source_url=source.get('url', source.get('source_url')), source_sha256=source['sha256'],
            source_filing_date=source['publication_at_displayed_KST'][:10],
            source_observed_at=dt.datetime.fromisoformat(source_observed),
            source_span='SIG-21 exact event only; holder quantity/price multiplier 1; retrospective partial; all corrections/rights uncertified')
    return result


def checked_spec(root, spec_path, commit, contract_commit, contract_sha, hashes, budget):
    root, spec_path = Path(root).resolve(), Path(spec_path).resolve()
    # Restrict addenda to docs; reject secret or arbitrary input paths before read.
    if (not spec_path.is_relative_to(root/'docs') or spec_path.suffix != '.json'
            or spec_path.stat().st_size > 200000 or not re.fullmatch(r'[0-9a-f]{40}', commit or '')):
        raise ValueError('event_spec_path_or_precommit')
    raw = spec_path.read_bytes()
    committed = subprocess.check_output(['git','show',f'{commit}:{spec_path.relative_to(root).as_posix()}'],
        cwd=root, timeout=max(.1, budget.seconds-max(budget.elapsed().values())))
    if raw != committed:
        raise ValueError('event_spec_not_precommitted')
    spec = decode_json(raw)
    expected = {'schema':'kr-r3-event-source-supplement-v1','contract_commit':contract_commit,
        'contract_sha256':contract_sha,'input_sha256':hashes,
        'archive_sha256':{n:v[0] for n,v in ARCHIVES.items()}, 'manifest_sha256':MANIFEST_SHA,
        'facts_sha256':FACTS_SHA,'proposal_path':PROPOSAL_PATH,'proposal_sha256':PROPOSAL_SHA,
        'source_sha256':SOURCE_SHA,'maximum_seconds':600,'new_collection':0}
    code = {n:sha((Path(__file__).resolve().parents[2]/n).read_bytes()) for n in CODE_FILES}
    expected['code_sha256'] = code
    if spec != expected or type(spec.get('new_collection')) is not int or len(hashes) != 35:
        raise ValueError('event_spec_scope_or_code_hash')
    read_evidence(root)
    budget.check()
    return spec, {**expected, 'spec_commit':commit,'spec_sha256':sha(raw),
                  'coverage_state':'partial','observation_mode':'retrospective',
                  'historical_capture_verified':False, 'activation_keys':[
                      [f['listing_session'], f['ticker']] for f in (PINNED_FACTS[0], ADJACENT, PINNED_FACTS[1])]}
