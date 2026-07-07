"""File + shell tools. Paths resolve inside WORKSPACE only."""
import subprocess
from config import WORKSPACE


def _p(rel):
    p = (WORKSPACE / rel).resolve()
    if not str(p).startswith(str(WORKSPACE.resolve())):
        raise ValueError(f"Path escapes workspace: {rel}")
    return p


def read_file(path):
    return _p(path).read_text(errors="replace")[:20000]


def write_file(path, content):
    fp = _p(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)
    return f"Wrote {fp}"


def list_files(path="."):
    d = _p(path)
    if not d.exists():
        return "(empty)"
    items = sorted(d.iterdir())
    return "\n".join(f"{'d' if i.is_dir() else 'f'} {i.name}" for i in items) or "(empty)"


def run_shell(command, timeout=120):
    """Runs as the OS user Cortana lives under. Sandbox = that user account."""
    p = subprocess.run(command, shell=True, cwd=WORKSPACE,
                       capture_output=True, text=True, timeout=timeout)
    out = (p.stdout or "")
    if p.stderr:
        out += "\nSTDERR:\n" + p.stderr
    return out[-8000:] or f"(exit {p.returncode}, no output)"
