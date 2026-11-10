"""不依赖 pytest 的运行器：直接执行 test_eviltown.py 里的 test_* 函数。

用法: python -X utf8 tests/run_tests.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import test_eviltown as T  # noqa: E402


def main() -> int:
    names = [n for n in dir(T) if n.startswith("test_")]
    names.sort()
    passed, failed = 0, []
    for name in names:
        fn = getattr(T, name)
        try:
            fn()
        except Exception:
            failed.append(name)
            print(f"FAIL {name}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {name}")
    print(f"\n{passed} passed, {len(failed)} failed (total {len(names)})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
