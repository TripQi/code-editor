from __future__ import annotations

from pathlib import Path
import importlib.util
import os
import shutil
import sys


def _make_lines(count: int) -> str:
    return "".join(f"line-{i}\n" for i in range(count))


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    temp_dir = Path(__file__).resolve().parent / ".tmp_logic_fixes"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(root))
    server_path = root / "server.py"
    spec = importlib.util.spec_from_file_location("code_editor_server", server_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load server module for tests.")
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)

    srv.set_root_path(str(root))
    assert Path(srv.fs_tools.__file__).resolve() == (root / "tools" / "filesystem.py").resolve()

    # P1: append with temp file chunking (behavioral check)
    append_file = temp_dir / "append.txt"
    srv.file_ops("write", file_path=str(append_file), content="a\n")
    srv.file_ops("append", file_path=str(append_file), content="b\n")
    assert append_file.read_text(encoding="utf-8") == "a\nb\n"

    # P1: dir_ops list flat max_items
    list_dir = temp_dir / "list_dir"
    list_dir.mkdir()
    for i in range(5):
        (list_dir / f"f{i}.txt").write_text("x", encoding="utf-8")
    entries = srv.dir_ops(
        action="list",
        dir_path=str(list_dir),
        format="flat",
        ignore_patterns=[],
        max_items=2,
    )
    assert len(entries) == 3
    assert entries[-1].get("truncated") is True

    # P2: read_file truncation notice
    read_file = temp_dir / "read.txt"
    read_file.write_text(_make_lines(1200), encoding="utf-8")
    os.environ["CODE_EDIT_FILE_READ_LINE_LIMIT"] = "1000"
    assert srv.fs_tools.get_file_read_line_limit() == 1000
    result = srv.read_file(str(read_file), offset=0, length=1500)
    content = result.get("content", "")
    if "[TRUNCATED]" not in content:
        print("CONTENT_PREFIX:", content[:200])
        raise AssertionError("Truncation notice missing")

    shutil.rmtree(temp_dir)
    print("PASS")


if __name__ == "__main__":
    main()
