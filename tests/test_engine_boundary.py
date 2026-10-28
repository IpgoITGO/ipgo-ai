"""engine/ 패키지가 FastAPI · LLM · DB에 의존하지 않는지 검사한다 (AGENTS.md 구조 규칙)."""
import ast
from pathlib import Path

FORBIDDEN = {"fastapi", "starlette", "openai", "app", "insight", "psycopg", "sqlalchemy", "boto3"}
ENGINE_DIR = Path(__file__).resolve().parents[1] / "engine"


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_engine_has_no_framework_imports():
    bad = {
        p.name: sorted(_imported_roots(p) & FORBIDDEN)
        for p in ENGINE_DIR.glob("*.py")
        if _imported_roots(p) & FORBIDDEN
    }
    assert not bad, f"engine/에서 쓰면 안 되는 import: {bad}"
