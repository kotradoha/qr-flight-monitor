# 카타르항공 도하 ↔ 인천 운항 모니터

KOTRA 도하무역관에서 현지 교민에게 제공하는 카타르항공 한국 노선(QR858 · QR862 · QR859)
지연·결항 상시 모니터링 페이지입니다. 중동 정세(이란 관련 무력 충돌, 영공 폐쇄)로 인한
운항 차질을 매시간 자동으로 확인합니다. 한국어/영어를 지원합니다.

## 구조

- `docs/index.html` — 공개 웹페이지 (GitHub Pages로 서비스, 한/영 전환)
- `docs/data.json` — 운항 데이터 (GitHub Actions가 매시간 갱신)
- `scripts/update.py` — 데이터 수집 스크립트 (Python 표준 라이브러리만 사용)
- `.github/workflows/update.yml` — 매시간 자동 실행 워크플로
- `docs/manual_notice.json` — 긴급 Travel Update 수동 공지(운영자 편집; items에 추가하면 표 위에 즉시 표시)
- 지원 언어: 한국어 / English / العربية(아랍어, RTL)

## 데이터 출처

| 항목 | 출처 |
|---|---|
| 운항 상태 (지연·결항) | Cirium/FlightStats (항공업계 표준 데이터, 웹페이지와 동일한 JSON) |
| 공식 여행 경보 | 카타르항공 공식 travel-alerts 페이지 (한국 노선·영공 키워드 감지) |
| 영공 위험 권고 | SafeAirspace (EASA CZIB·각국 NOTAM 집계) |
| 실시간 항공기 위치 | adsb.lol (커뮤니티 ADS-B) + Flightradar24 링크 |

## 최초 설정 (1회)

1. **Settings → Pages → Build and deployment**: Source = `Deploy from a branch`,
   Branch = `main`, 폴더 = `/docs` 선택 후 Save
2. **Settings → Actions → General → Workflow permissions**: `Read and write permissions` 선택 후 Save
   (매시간 워크플로가 `docs/data.json`을 커밋할 수 있어야 함)
3. **Actions 탭**에서 `Update flight data` 워크플로를 열고 `Run workflow`로 1회 수동 실행
4. 몇 분 뒤 `https://<계정명>.github.io/<저장소명>/` 에서 페이지 확인

## 판정 기준

- 결항(Cancelled) / 회항(Diverted), 출발·도착 20분 이상 지연 → 배너·표에 표시
- 카타르 영공 폐쇄(SafeAirspace) → 상단 표시줄·배너에 상세(감지시각·권고 유효기간·영향편) 표시
- QR862가 스케줄에 없는 날은 **결항으로 처리하지 않음** (비정기 운항편)
- 조회 실패는 **'미운항'이나 '정상'으로 단정하지 않고** `확인 중`으로 표기하며,
  실시간 확인이 전무하면 `degraded=true`로 상단에 안내를 띄운다.

## 견고성 / 정합성

- `http_get`은 3회 재시도(지수 백오프). FlightStats 실패와 '해당일 미운항'을 구분한다
  (`FetchError` vs `None`).
- 개별 편·부가 소스 실패가 전체 출력을 무너뜨리지 않는다(각 단계 try/except, 실패 시
  이전 데이터 유지 가능).
- 초기 `docs/data.json`은 허위 상태가 아닌 `확인 중(degraded)` 시드다. 첫 워크플로 실행 후
  실데이터로 대체된다.

## 주의

- FlightStats JSON은 비공식 엔드포인트다. GitHub Actions(데이터센터 IP)에서 차단될 경우
  전 항목이 `확인 중`으로 표시되고 `degraded` 안내가 뜬다. 이 경우 유료 항공데이터 API
  (Cirium/FlightAware AeroAPI)로 교체하면 안정적이다 — `scripts/update.py`의
  `fetch_flight`만 교체하면 된다.
- 본 페이지는 참고용이며, 최종 확인은 카타르항공 공식 채널을 이용해야 한다.
