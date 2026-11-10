"""pytest 配置：把项目根加入 sys.path，使 tests/ 内的用例能 import eviltown。

这样 `pytest -q`、`python -X utf8 -m pytest tests/test_eviltown.py`、
以及 `python -X utf8 tests/run_tests.py` 三条入口都能直接跑通。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
