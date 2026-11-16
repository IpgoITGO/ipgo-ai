# 고정19일 LightGBM 연결

대상 이슈: #117~121 / IPGO-257~260·263. 순수 특징 함수 출처는 IpgoITGO/model 66bffa0이다. 기존 A/B와 API/큐 경로를 유지하고, 명시적인 LGBM19 요청에만 새 모델을 사용한다.

## 입력과 결과

CalculateRequest.policy에 defaultMethod=LGBM19, multiplier=1, modelVersion(필수)을 보내고 productCodes에 서비스 productId→바코드를 보낸다. app/repository.py의 기존 소유권/source 검증과 read-only DB 조회를 그대로 사용한다. 상품별 배송5일·발주14일 조건이 아니거나 매핑/이력이 부족하면 NOT_CALCULABLE이며 0개 권장으로 바꾸지 않는다.

model_assets는 공개 Git에 넣지 않는다. q50/q80/q90·metadata·product-map·residuals를 scripts/model19/prepare_bundle.py로 묶는다. 명시한 SHA256과 내용 기반 modelVersion, 23개 특징/분위수, 학습·오차 기준일과 비중복을 확인한다. IPGO_MODEL19_DIR의 불변 디렉터리를 프로세스별로 로딩해 재사용한다. 변경은 새 디렉터리·새 버전으로 진행하고 실행 중 덮어쓰지 않는다. 버전/파일 불일치는 요청 실패이며 B로 조용히 대체하지 않는다.

원본 DB의 재고·출고는 수정하지 않고 모델 입력에서만 하루 출고100개 이상을 비판매로 분리한다. 학습 표본5개 미만이면 인수 모델의 최근 평균 fallback을 유지하되 modelFallback으로 표시한다. 주문 정책 자체는 LGBM19/BUFFER100_FLOOR0이다.

## 수량과 날짜

목표 재고는 min(P90×1.2,max(P90×0.8,P80+보충량)), 발주량은 목표−현재추정재고−기간 내 예정입고를 0이상으로 제한하고 주문 단위로 올린다. 기존 A/B required_qty의 도착 전 손실 차감은 추가하지 않는다. P50은 수요 설명과 일별 재고 시나리오에 사용하고, 안전 여유를 포함한 targetStockQty와 분리한다. 일별 P50/19 일정 수요는 연결상의 시뮬레이션 가정이며 실제 날짜별 수요 성능을 검증한 결과가 아니다.

예측은 계산일 today 다음날부터19일이다. asOfDate 이후 today까지는 관측을0판매로 만들지 않고 미관측으로 특징을 생성한다. 시작 재고는 기존 경과일/실제 예정입고 로직과 P50/19로 추정한다. 기간을 g+19일로 늘리거나 19일 예측을 비례 환산하지 않는다. 경과일이 있는 사용 조건은 별도 서비스 검증 대상이며 과거 보고서와 동등한 성능을 주장하지 않는다.

HTTP calculate, SQS CALCULATE, CUSTOM은 같은 bundle을 사용한다. 검증되지 않은 A/B용 DA07·DA13을 붙이지 않고 위험/정확도/인사이트를 미산출로 반환한다. modelVersion·targetStockQty·modelFallback으로 실행 근거를 전달하고 engineVersion에는 +lgbm19를 붙인다. 모델 P80/P90와 주문량80/100/120% 시나리오는 별개다.

## 비공개 이미지 준비

기본 Dockerfile은 libgomp1과 고정 라이브러리를 설치한다. 기본 이미지 빌드 후 Dockerfile.model19가 Git에서 제외된 model_assets를 /opt/ipgo-model19에 복사한다. 로컬 이미지에도 실제 모델이 포함되므로 승인 없이 공개 registry로 push/export하지 않는다. model_assets를 제거한 CI 기본 빌드도 유지한다.

AWS 리소스/권한/Secret 생성은 필요하지 않은 것으로 설계했다. 이미지 push·실제 task 등록·환경변수 변경·서비스 갱신은 실행하지 않는다. 배포 전 실제 메모리/큐 처리/컨테이너 입력 크기와 정확한 SHA·이미지 digest를 확인한다.

복구는 새 계산 요청을 B로 돌리고 이전 AI 이미지와 모델 버전을 복원하는 방식이다. 과거 LGBM19 결과나 snapshot을 B로 덮어쓰지 않는다. 과거 CUSTOM 재생이 필요하면 해당 버전 bundle을 제공해야 한다.
