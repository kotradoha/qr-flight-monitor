# 카타르항공 도하 ↔ 인천 운항 모니터

KOTRA 도하무역관에서 현지 교민에게 제공하는 카타르항공 한국 노선(QR858 · QR862 · QR859)
지연·결항 상시 모니터링 페이지입니다. 중동 정세(이란 관련 무력 충돌, 영공 폐쇄)로 인한
운항 차질을 매시간 자동으로 확인합니다. 한국어/영어를 지원합니다.

## 구조

- `docs/index.html` — 공개 웹페이지 (GitHub Pages로 서비스, 한/영 전환)
- `docs/data.json` — 운항 데이터 (GitHub Actions가 매시간 갱신)
- `scripts/update.py` — 데이터 수집 스크립트 (Python 표준 라이브러리만 사용)
- `.github/workflows/update.yml` — 매시간 자동 실행 워크플로

## 데이터 출처

| 항목 | 출처 |
|---|---|
| 운항 상태 (지연·결항) | Cirium/FlightStats (항공업계 표준 데이터, 웹페이지와 동일한 JSON) |
| 공식 여행 경보 | 카타르항공 공식 travel-alerts 페이지 (한국 노선·영공 키워드 감지) |
| 영공 위험 권고 | SafeAirspace (EASA CZIB·각국 NOTAM 집계) |
| 실시간 항공기 위치 | adsb.lol (커뮤니티 ADS-B) + Flightradar24 링크 |

## 최초 설정 (1회)

1. 이 저장소를 GitHub에 push
2. **Settings → Pages → Build and deployment**: Source = `Deploy from a branch`,
   Branch = `main`, 폴더 = `/docs` 선택 후 Save
3. **Actions 탭**에서 `Update flight data` 워크플로를 열고 `Run workflow`로 1회 수동 실행
4. 몇 분 뒤 `https://<계정명>.github.io/<저장소명>/` 에서 페이지 확인
5. 구독 신청 폼: 첫 신청이 들어오면 FormSubmit(폼 전송 서비스)에서 수신 이메일로
   활성화 확인 메일이 오며, 1회 승인하면 이후 신청이 계속 수신됩니다.

## 알림 기준

- 결항(Cancelled) / 회항(Diverted)
- 출발·도착 20분 이상 지연
- QR862가 스케줄에 없는 날은 **결항으로 처리하지 않음** (비정기 운항편)

## 주의

- FlightStats JSON은 비공식 엔드포인트로, 구조가 변경되면 `scripts/update.py`의
  파싱 부분을 수정해야 합니다. 실패 시 이전 데이터가 유지되며 페이지의
  "마지막 확인" 시각으로 최신성을 판단할 수 있습니다.
- 본 페이지는 참고용이며, 최종 확인은 카타르항공 공식 채널을 이용해야 합니다.
