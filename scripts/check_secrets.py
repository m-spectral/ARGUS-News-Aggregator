import re
import subprocess
import sys
from pathlib import Path


PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
    re.compile(r"(?:GEMINI_API_KEY|NEWSAPI_KEY|NEWSAPI_API_KEY|GNEWS_KEY|NEWSDATA_KEY|SLACK_WEBHOOK_URL)\s*=\s*['\"]?([A-Za-z0-9_./:-]{20,})"),
    re.compile(r"\bpub_[a-fA-F0-9]{24,64}\b"),
    re.compile(r"(?<![A-Za-z0-9])[a-fA-F0-9]{32}(?![A-Za-z0-9])"),
)


def main():
    all_files = "--all" in sys.argv
    command = ["git", "ls-files", "-z"] if all_files else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"]
    names = subprocess.check_output(command).decode().split("\0")
    failures = []
    for name in filter(None, names):
        if name == ".env" or (Path(name).name.startswith(".env.") and name != ".env.example"):
            failures.append(name)
            continue
        text = Path(name).read_text(errors="replace") if all_files else subprocess.check_output(["git", "show", ":" + name]).decode(errors="replace")
        if any(pattern.search(text) for pattern in PATTERNS):
            failures.append(name)
        if name.endswith(".ipynb"):
            import json
            notebook = json.loads(text)
            if any(cell.get("outputs") or cell.get("execution_count") is not None for cell in notebook["cells"] if cell["cell_type"] == "code"):
                failures.append(name + " (clear notebook outputs)")
    if failures:
        raise SystemExit("Blocked staged files: " + ", ".join(failures))
    print("Staged credential and notebook checks passed.")


if __name__ == "__main__":
    main()
