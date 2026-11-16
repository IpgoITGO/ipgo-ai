"""검증된19일 고정 모델을 비공개 디렉터리에 패키징한다. 학습/전송은 하지 않는다."""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from engine.model19 import FILES, POLICY, load_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regression", type=Path, required=True)
    parser.add_argument("--product-map", type=Path, required=True)
    parser.add_argument("--residuals", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in FILES:
        source = (args.product_map if name == "product-map.json" else args.residuals if name == "residuals.csv"
                  else args.regression / name)
        shutil.copyfile(source, args.output / name)
    hashes = {name: hashlib.sha256((args.output/name).read_bytes()).hexdigest() for name in FILES}
    fingerprint = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    version = f"lgbm19-{fingerprint}"
    manifest = dict(modelVersion=version, policy=POLICY, horizonDays=19, leadTimeDays=5, reviewCycleDays=14,
                    sourceCommit="66bffa0f0729a0365244c055bd9c4aeed830a649", sha256=hashes)
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    load_bundle(str(args.output.resolve()), version)
    print(json.dumps({"modelVersion":version,"validated":True}))


if __name__ == "__main__":
    main()
