# 시험 prompt 고정본 (`insight_prompts.json`)

#32 모델 선정 · 연동 시험(`python -m insight.benchmark`)에서 모든 모델에 똑같이 보내는 prompt입니다. 운영 prompt 코드는 아직 정해지지 않은 사항에 걸려 있어서, 그 결과물을 미리 만들어 둔 것입니다.

## 만든 방법

참조 구현(`ai_insight` v0.3, prompt `prompt-2026.10.21-v0.4`)의 `build_prompt()` 출력을 그대로 쓰고 아래만 바꿨습니다. 10/21 로컬 비교와 같은 조건을 유지하기 위해서입니다.

| 바꾼 것 | 내용 |
|---|---|
| 자리 표시자 이름 | 0002 3-4 이름으로 바꿈. 예: `qty.s100` → `qty_p100`, `remain.s80` → `ending_p80`, `risk_before` → `risk_pre`, `sample_n` → `risk_sample_size`, `formula` → `method`. † 문구 키(`label` · `action` · `dir.*`)는 시나리오 이름만 `p80` · `p100` · `p120`으로 바꿈 |
| 0002 3-4에 없는 키 | `lead_time` · `restock_cycle` · `order_unit` · `recent_28d_avg` · `unreflected_qty`가 들어간 줄을 뺌 |
| 입고 불필요 사례의 비교 문구 | 참조 구현이 권장량 시나리오가 없는데도 목록에 없는 비교 키(`action.custom` · `dir.remain.custom_p100.end`)를 안내해서, 그 문구를 뺌 |
| 확인 필요(`LOW_GRADE`) 사례 | 참조 구현에 없던 상태라 `low_sample`을 바탕으로 직접 씀. #4 문서 3장대로 위험 대신 `{{forecast_range_low}}`~`{{forecast_range_high}}` 범위로 설명하게 함 |

값은 모두 참조 구현 `samples/`의 가짜 값입니다(상품 이름은 "(이 상품)"). 실제 셀러 데이터는 없습니다.

## 사례

사례마다 자리 표시자 값을 넣은 판(`values: true`)과 이름만 넣은 판(`values: false`)이 있습니다. 명세 7장 미결 항목 "실제 숫자 전송 여부"의 근거로 씁니다.

| `id` | 상태 | 출처 (참조 구현 `samples/`) | 필수 자리 표시자 |
|---|---|---|---|
| `normal_custom` | 정상 + 직접 입력 | `engine_result_normal.json` | `qty_p100`, `risk_post_p100`, `risk_pre` |
| `duplicate` | 정상, 중복 시나리오 | `engine_result_duplicate.json` | `qty_p100`, `risk_post_p100`, `risk_pre` |
| `low_sample` | 판단 보류 (위험 미산출) | `engine_result_low_sample.json` | `qty_p100` |
| `pooled` | 전체 상품 기준 위험 | `engine_result_pooled.json` | `qty_p100`, `risk_post_p100`, `risk_pre` |
| `no_restock_custom` | 입고 불필요 + 직접 입력 | `engine_result_no_restock.json` | 없음 |
| `low_grade` | 확인 필요 | `engine_result_low_sample.json` + 직접 작성 | `qty_p100`, `forecast_range_low`, `forecast_range_high` |

필수 자리 표시자는 참조 구현 `required_keys()` 규칙(권장량, 산출된 경우 도착 후 · 도착 전 위험)을 따랐고, `low_grade`만 범위 두 키를 더했습니다. 계산 불가 샘플은 LLM을 부르지 않는 경우라 넣지 않았습니다(명세 3.8, BE-10).

## 버리는 조건

아래가 정해져 운영 prompt 코드가 생기면, `insight/benchmark.py`의 `load_cases()`가 고정본 대신 운영 코드로 사례를 만들게 바꿉니다.

- 자리 표시자 이름 · 목록 만들기: #14, 0002 3-4
- 응답 형식 `{"text"}` / `{"sentences"}`: IpgoITGO/ipgo-server#25
- 입력 구조: #10, `engine/insight_facts.py`
- 값 넣기 · 방향 문구 위치: #24 ①

그때 이 고정본을 지울지, 10/21 · #32 측정과 같은 조건으로 다시 비교하는 기준점으로 남길지 함께 정합니다.
