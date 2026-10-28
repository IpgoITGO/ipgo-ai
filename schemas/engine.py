"""계산 엔진 입출력 계약 (DA-11)

기준: API 명세서 4장 (Spring ↔ FastAPI), 데이터 스키마(ERD v6), 10/27 백엔드 합의
담당: 장준영 · 확인: 이석형(AI 인사이트), 박혜윤·정무영(백엔드)

- 이 파일이 FastAPI OpenAPI 명세의 원본이고, Spring DTO는 여기서 만든다.
- 필드를 바꿀 때는 명세 → FastAPI(이 파일) → Spring 순서로 같이 바꾸고 docs/decisions/에 남긴다.
- 수량: 엔진은 소수로 끝까지 계산한다(float). 정수는 입고 단위로 올린 수량과 금액뿐이다.
  화면 표시용 정수 변환은 엔진이 하지 않는다 (docs/decisions/0001).
"""
from datetime import date

from pydantic import Field, model_validator

from .base import (
    AccuracyGrade,
    CamelModel,
    Method,
    ResultStatus,
    RiskStatus,
    ScenarioType,
)

# =====================================================================
# 1. POST /engine/clean — 수불부 정제·검증 (DA-01, DA-02) · 120초
# =====================================================================


class SourceFile(CamelModel):
    """업로드 1건에 들어 있는 원본 파일 하나 (upload_files 1행)."""

    upload_file_id: int
    storage_key: str
    original_name: str


class CleanRequest(CamelModel):
    upload_id: int                          # 묶음 번호 = upload_files.job_id
    user_id: int
    warehouse_id: int
    files: list[SourceFile] = Field(min_length=1)


class CleanedProduct(CamelModel):
    """상품 1행 = SKU바코드 1개."""

    sku_barcode: str
    sku_name: str
    unit_cost: int | None = None            # 수불부에 원가가 있으면 채움 (원)
    is_new: bool                            # 이번 업로드에서 처음 나온 바코드


class InventoryUnit(CamelModel):
    """재고관리단위: 같은 창고 · sku_mixed_id · 입고일의 재고 묶음."""

    source_inventory_key: str               # 원본 sku_mixed_id
    sku_barcode: str
    received_on: date


class UnitDailyRecord(CamelModel):
    """재고관리단위별 하루 기록 → daily_stock_records."""

    upload_file_id: int
    source_inventory_key: str
    received_on: date
    stock_date: date
    opening_qty: int
    inbound_qty: int
    outbound_qty: int
    adjustment_qty: int
    closing_qty: int
    source_sheet: str
    source_row: int


class ProductDailyStock(CamelModel):
    """상품 단위로 합친 하루 기록 → product_daily_stocks."""

    upload_file_id: int
    sku_barcode: str
    stock_date: date
    opening_qty: int
    inbound_qty: int
    outbound_qty: int
    adjustment_qty: int
    closing_qty: int
    is_valid_observation: bool              # 시작 재고 0이면 False (당일 입고가 있어도) — DA-02
    observation_note: str | None = None     # "첫 입고 이전" · "시작재고 0(미관측)" · "종료재고 0"


class ExcludedSku(CamelModel):
    """재고 검산 불일치로 계산에서 뺀 SKU."""

    sku_barcode: str
    sku_name: str
    date: date
    reason: str                             # 셀러에게 그대로 보여줄 문장


class ValidationReport(CamelModel):
    checked_sku_count: int
    target_sku_count: int
    excluded_skus: list[ExcludedSku] = []


class CleanResponse(CamelModel):
    period_start: date
    period_end: date
    as_of_date: date                        # 기준일 = 자료의 마지막 날짜
    products: list[CleanedProduct]
    inventory_units: list[InventoryUnit]
    unit_daily_records: list[UnitDailyRecord]
    product_daily_stocks: list[ProductDailyStock]
    validation: ValidationReport
    # S1 제안값. 백엔드가 validation_report에 저장 → 셀러 확인 후 창고 기본값.
    # TODO(합의): 이번 파일만이 아니라 누적 기록으로 추정하는 방법 (docs/decisions/0001)
    estimated_review_cycle_days: int | None = None


# =====================================================================
# 2. POST /engine/calculate — 입고 계산 (DA-03~07, DA-11, DA-13) · 60초
# =====================================================================


class Policy(CamelModel):
    """정책 선정(DA-09) 결과. 백테스트 전까지는 B · 1.00."""

    default_method: Method = Method.B
    multiplier: float = Field(1.0, ge=1.0, le=1.5)


class ProductSettings(CamelModel):
    """상품별 입고 조건 (Spring이 기본값을 적용한 최종값)."""

    product_id: int
    lead_time_days: int = Field(ge=1)
    review_cycle_days: int = Field(ge=1)
    order_unit_qty: int = Field(ge=1)
    unit_cost: int = Field(ge=0)


class PlannedReceiptInput(CamelModel):
    """기존 발주 물량. 도착 예정일 없는 건(NO_DATE)은 Spring이 빼고 보낸다."""

    product_id: int
    quantity: int = Field(ge=1)
    expected_on: date


class CalculateRequest(CamelModel):
    run_id: int
    user_id: int
    warehouse_id: int                       # 확인용
    upload_file_ids: list[int] = Field(min_length=1)  # 읽을 수불. 덮어쓴 파일은 Spring이 빼고 보냄
    as_of_date: date
    today: date
    product_ids: list[int] | None = None    # None이면 전체
    policy: Policy
    settings: list[ProductSettings]
    planned_receipts: list[PlannedReceiptInput] = []


class DailyProjection(CamelModel):
    date: date
    closing_qty: float


class ScenarioResult(CamelModel):
    """DA-06 · DA-07. 입고 후 출고 순서, 부족분은 미충족 수요로 기록하고 다음 날로 넘기지 않는다."""

    type: ScenarioType
    qty: int = Field(ge=0)                  # 입고 단위로 올린 정수
    is_duplicate: bool = False
    expected_depletion_on: date | None      # 커버 기간 안에 소진되지 않으면 None
    expected_ending_stock: float            # 다음 입고 시점 잔여 재고
    unmet_demand_qty: float
    post_arrival_risk: float | None = Field(None, ge=0, le=1)  # riskStatus가 OK가 아니면 None
    daily_projection: list[DailyProjection] = []


_REASON_REQUIRED = {ResultStatus.NOT_CALCULABLE, ResultStatus.EXCLUDED, ResultStatus.REUPLOAD_REQUIRED}


class ProductResult(CamelModel):
    """SKU 하나의 계산 결과 → calculation_results 1행 + scenario_results N행."""

    product_id: int
    status: ResultStatus
    reason: str | None = None               # NOT_CALCULABLE · EXCLUDED · REUPLOAD_REQUIRED면 필수

    # 계산 근거 (FE-05 · K1 result)
    method: Method | None = None
    method_reason: str | None = None        # "평가 결과 기본 방식", "작년 자료 없어 B로 대체"
    multiplier: float | None = None
    horizon_days: int | None = None         # 커버 기간 H = 입고 주기 + 리드타임
    forecast_outbound_qty: float | None = None
    start_stock_qty: float | None = None
    start_stock_estimated: bool = False     # 오늘 > 기준일이면 True
    planned_receipts_in_horizon: int = 0    # 커버 기간 안에 도착하는 기존 발주 수량 합계 (건수 아님)
    pre_arrival_unmet_qty: float = 0
    required_qty_before_rounding: float | None = None
    recommended_qty: int | None = None      # 입고 단위로 올린 정수
    recommended_amount: int | None = None   # 원가 × 권장 수량 (원)
    expected_shortage_on: date | None = None

    # 품절 위험 (DA-07)
    pre_arrival_risk: float | None = Field(None, ge=0, le=1)
    risk_sample_size: int | None = None
    risk_status: RiskStatus | None = None

    # 정확도 (DA-13). 유효 관측일 7일 미만이면 grade 없이 NOT_CALCULABLE
    accuracy_grade: AccuracyGrade | None = None
    valid_observation_days: int | None = None
    cover_success_count: int | None = None
    cover_trial_count: int | None = None
    forecast_error_rate: float | None = None
    forecast_range_low: float | None = None   # LOW 등급일 때만 값, 나머지 None
    forecast_range_high: float | None = None

    scenarios: list[ScenarioResult] = []    # 권장량 0개면 빈 목록

    @model_validator(mode="after")
    def _reason_required(self) -> ProductResult:
        """계산 불가 · 제외 결과는 이유 없이 내보내지 않는다 (0개로 바꾸지 않는다)."""
        if self.status in _REASON_REQUIRED and not self.reason:
            raise ValueError(f"status가 {self.status}이면 reason이 필요해요")
        return self


class CalculateResponse(CamelModel):
    engine_version: str
    results: list[ProductResult]


# =====================================================================
# 3. POST /engine/scenarios — 직접 입력 수량 시나리오 (DA-06) · 10초
# =====================================================================


class ScenarioRequest(CamelModel):
    result_id: int
    calculation_input: CalculateRequest     # 해당 결과의 input_snapshot (상품 1개)
    qty: int = Field(ge=1)


# 응답은 ScenarioResult (type=CUSTOM)


# =====================================================================
# 4. POST /engine/backtest — 정확도 평가 (DA-08~10, DA-12, DA-13) · 300초 · 팀 내부용
# =====================================================================


class Period(CamelModel):
    start: date
    end: date


class BacktestRequest(CamelModel):
    user_id: int
    warehouse_id: int
    upload_file_ids: list[int] = Field(min_length=1)
    tune_period: Period                     # 정책 조정: 기준일 5/1 이후, t+H ≤ 7/31
    test_period: Period                     # 최종 시험: 8~9월, t+H ≤ 10/14
    multiplier_candidates: list[float] = [1.00, 1.05, 1.10, 1.15]


class MethodError(CamelModel):
    """DA-08: 구간별 A/B 예측 오차."""

    period: str                             # "TUNE" · "TEST"
    method: Method
    mae: float
    wape: float
    sample_size: int


class MultiplierMetric(CamelModel):
    """DA-09: 배수별 커버 성공률 · 과잉 비율."""

    multiplier: float
    cover_success_rate: float
    excess_rate: float
    meets_target: bool


class ActualInboundComparison(CamelModel):
    """DA-12: 실제 입고 vs 계산식 입고, 같은 조건 시뮬레이션."""

    product_id: int
    engine_shortage_days: int
    engine_average_stock: float
    actual_shortage_days: int
    actual_average_stock: float


class AccuracyHistoryPoint(CamelModel):
    origin_date: date
    forecast_qty: float
    actual_qty: float
    covered: bool


class SkuAccuracy(CamelModel):
    """DA-13 → K1 accuracy 카드."""

    product_id: int
    grade: AccuracyGrade | None
    valid_observation_days: int
    cover_success_count: int | None = None
    cover_trial_count: int | None = None
    forecast_error_rate: float | None = None
    range_low: float | None = None
    range_high: float | None = None
    history: list[AccuracyHistoryPoint] = []


class RiskBucket(CamelModel):
    low: float
    high: float
    actual_stockout_rate: float | None      # 사례 부족이면 None (판정 안 함)
    cases: int


class ScenarioReliability(CamelModel):
    """DA-10: 시나리오 추정값 vs 실제 출고로 다시 계산한 값."""

    ending_stock_mae: float
    risk_buckets: list[RiskBucket]


class BacktestResponse(CamelModel):
    engine_version: str
    selected_method: Method
    selected_multiplier: float
    selection_reason: str
    target_met: bool
    method_errors: list[MethodError]
    multiplier_metrics: list[MultiplierMetric]
    actual_inbound_comparison: list[ActualInboundComparison]
    scenario_reliability: ScenarioReliability | None = None
    sku_accuracy: list[SkuAccuracy]
