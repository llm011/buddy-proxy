"""「不变量 → 测试」映射的自检。

``docs/architecture.md`` 末尾的 **Invariants and the tests that hold them** 表把
设计里最贵的那几条规则逐条挂到具体测试上。这份文档最大的风险不是写错，而是**悄悄
过期**：测试改名/拆分/删除以后，表里仍然写着它，读者以为有人守着，实际早就没了。

所以那张表必须可执行：本测试解析它，逐行检查

1. 每行都点名了「在哪段代码里生效」（可定位的文件/函数，不是一句空话）；
2. 每行引用了一个 ``tests/xxx.py::test_yyy`` 形式的测试；
3. 那个文件存在、那个测试函数真的存在（按 AST 收集，不依赖 pytest 收集顺序）；
4. 编号唯一且至少覆盖 10 条——数字只是防止有人把表删到只剩两条的护栏。

改名测试时这条测试会红，逼着把文档一起改——这正是让文档跟得上代码的机制。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ARCHITECTURE = _ROOT / "docs" / "architecture.md"

#: 表格行：``| I1 | 不变量 | 在哪生效 | 测试 |``。
_ROW = re.compile(r"^\|\s*(I\d+)\s*\|(.+?)\|(.+?)\|(.+?)\|\s*$")

#: 测试节点 id：``tests/test_x.py::test_y``（带反引号，避免扫到散文里的路径）。
_TEST_ID = re.compile(r"`(tests/[\w./-]+\.py)::([A-Za-z_]\w*)`")

#: 「在哪生效」列里至少要有一个反引号代码片段（文件或函数名）。
_CODE_SPAN = re.compile(r"`[^`]+`")

#: 表格至少要有这么多行——见模块文档第 4 条。
_MIN_ROWS = 10


def _table_rows() -> list[tuple[str, str, str]]:
    """返回 ``(编号, 在哪生效, 测试列)``。"""
    rows: list[tuple[str, str, str]] = []
    for line in _ARCHITECTURE.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            rows.append((match.group(1), match.group(3), match.group(4)))
    return rows


def _test_names(path: Path) -> set[str]:
    """文件里所有 ``def test_*`` 的名字（含类方法、嵌套函数）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def test_invariant_table_exists_and_is_not_thin():
    """表格存在、编号唯一、行数够多。"""
    rows = _table_rows()
    assert len(rows) >= _MIN_ROWS, (
        f"docs/architecture.md 的不变量表只有 {len(rows)} 行（至少 {_MIN_ROWS} 行）："
        "删规则请连测试一起删，别只删文档"
    )
    ids = [row[0] for row in rows]
    assert len(ids) == len(set(ids)), f"不变量编号重复：{sorted(ids)}"


def test_every_invariant_names_where_it_is_enforced():
    """「在哪生效」列必须可定位（至少一个反引号代码片段）。"""
    bad = [row[0] for row in _table_rows() if not _CODE_SPAN.search(row[1])]
    assert not bad, f"这些不变量没写清在哪段代码里生效（用反引号点名文件/函数）：{bad}"


def test_every_invariant_cites_exactly_one_test():
    """每行必须引用一个测试节点 id。"""
    bad = []
    for ident, _, test_cell in _table_rows():
        if len(_TEST_ID.findall(test_cell)) != 1:
            bad.append(ident)
    assert not bad, (
        f"这些不变量没有恰好引用一个 `tests/...::test_...`：{bad}"
    )


def test_cited_tests_exist():
    """引用的测试文件与测试函数都真实存在。

    按 AST 收集函数名，不跑 pytest 收集器：本测试要能在一个文件里独立给出失败原因
    （文件丢了 / 函数改名了），而不是抛一堆 collection error。
    """
    missing: list[str] = []
    for ident, _, test_cell in _table_rows():
        for rel_path, func_name in _TEST_ID.findall(test_cell):
            path = _ROOT / rel_path
            if not path.is_file():
                missing.append(f"{ident}: {rel_path} 不存在")
                continue
            if func_name not in _test_names(path):
                missing.append(f"{ident}: {rel_path} 里没有 {func_name}（改名后请同步文档）")
    assert not missing, "不变量表引用了不存在的测试：\n" + "\n".join(f"  {m}" for m in missing)
