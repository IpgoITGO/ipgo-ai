"""입고 계산 엔진 (담당: 장준영).

규칙: 이 패키지는 순수 파이썬 함수만 둔다. fastapi · openai · DB 드라이버를 import하지 않는다.
파일·DB에서 읽는 일은 app/이 하고, 엔진은 읽은 데이터(DataFrame · schemas 모델)를 받는다.
tests/test_engine_boundary.py가 이 규칙을 검사한다.
"""

# 계산 규칙이나 출력 형식이 바뀌면 올린다. Spring이 calculation_runs.engine_version에 저장한다.
#   - 정책·계산식 변경: 0.x.0 올림  /  버그 수정: 0.0.x 올림
ENGINE_VERSION = "0.1.0"
