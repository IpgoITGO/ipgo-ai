"""비공개 정제 CSV로 DA07 후보 가용성을 한 번 조사한다. 네트워크/DB/LLM 호출은 없다."""
import argparse
import hashlib
import json
import os
import sys
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from engine.risk_audit import audit_risk_samples  # noqa: E402
from schemas.engine import Period  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="권한을 확인한 정제 product_daily CSV")
    parser.add_argument("--output", type=Path, required=True, help="저장소 밖 private JSON 경로")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--observed-through", type=date.fromisoformat, required=True)
    parser.add_argument("--origin-step-days", type=int, required=True)
    parser.add_argument("--horizons", type=int, nargs="+", required=True)
    parser.add_argument("--candidates", type=int, nargs="+", required=True)
    args = parser.parse_args()
    if args.origin_step_days < 1 or args.output.exists():
        parser.error("양의 기준일 간격과 새 출력 경로를 지정해 주세요")
    repo = Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(repo) or args.input.resolve() == args.output.resolve():
        parser.error("보고서는 저장소 밖 비공개 경로에 저장해 주세요")
    try:
        if args.input.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("정제 CSV 크기 제한")
        content = args.input.read_bytes()
        frame = pd.read_csv(BytesIO(content))
        if len(frame) > 100_000:
            raise ValueError("정제 행 수 제한")
        frame["stock_date"] = frame["stock_date"].map(date.fromisoformat)
        if frame["is_valid_observation"].dtype != bool:
            # 부울로 변환 불가한 값은 임의 True/False로 보충하지 않는다.
            values = frame["is_valid_observation"].map({"True": True, "False": False})
            if values.isna().any():
                raise ValueError("유효 관측 표식 오류")
            frame["is_valid_observation"] = values
        start, end = date(args.year, 5, 1), date(args.year, 7, 31)
        origins = tuple(start + timedelta(days=n) for n in range(0, (end - start).days, args.origin_step_days))
        report = audit_risk_samples(
            frame, period=Period(start=start, end=end), observed_through=args.observed_through,
            origins=origins, horizons=tuple(args.horizons), candidates=tuple(args.candidates),
        )
        report["input_sha256"] = hashlib.sha256(content).hexdigest()
        report["input_rows"] = len(frame)
        args.output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"profiles": len(report["profiles"]), "selected_min_samples": None,
                          "note": "가용성 조사 완료. 최소 기준은 자동 확정하지 않음"}, ensure_ascii=False))
    except (OSError, ValueError, TypeError, KeyError):
        print("정제 입력·기간·후보·출력 경로를 확인해 주세요. 원본 값은 로그에 남기지 않았습니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
