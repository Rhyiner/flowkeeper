import contextlib
import io
import os
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from flowkeeper.cli import main  # noqa: E402


def run(args: List[str], env: dict = None) -> Tuple[int, str, str]:
    """Run the CLI in-process and capture its output."""
    out, err = io.StringIO(), io.StringIO()
    old_env = dict(os.environ)
    os.environ.pop("NO_COLOR", None)
    os.environ.update(env or {})
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(args)
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    return code, out.getvalue(), err.getvalue()
