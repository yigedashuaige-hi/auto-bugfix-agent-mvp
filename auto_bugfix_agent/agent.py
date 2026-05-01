#!/usr/bin/env python3
"""
Auto Bugfix Agent MVP

A local, cautious code-maintenance agent that runs a check command, asks an
OpenAI-compatible model for a unified diff, applies it, and verifies the result.
"""

from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None  # type: ignore


EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "dist", "build", "target", ".next", ".nuxt", ".cache", "coverage", "__pycache__",
}

EXCLUDE_GLOBS = [
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp", "*.ico", "*.pdf",
    "*.zip", "*.tar", "*.gz", "*.7z", "*.exe", "*.dll", "*.so", "*.dylib",
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
]

IMPORTANT_FILES = [
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "package.json",
    "tsconfig.json", "vite.config.ts", "next.config.js", "README.md", "Cargo.toml",
    "go.mod", "pom.xml", "build.gradle",
]


@dataclasses.dataclass
class CommandResult:
    command: str
    code: int
    stdout: str
    stderr: str

    @property
    def combined(self) -> str:
        return (self.stdout + "\n" + self.stderr).strip()


@dataclasses.dataclass
class AgentConfig:
    repo: Path
    test_cmd: str
    model: str
    max_iterations: int
    max_log_chars: int
    max_file_chars: int
    max_context_chars: int
    dry_run: bool
    allow_dirty: bool
    temperature: float
    command_timeout: int


class AgentError(RuntimeError):
    pass


def run_command(command: str, cwd: Path, timeout_seconds: int = 120) -> CommandResult:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        return CommandResult(command, proc.returncode, proc.stdout, proc.stderr)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return CommandResult(command, 124, stdout, stderr + f"\nCommand timed out after {timeout_seconds}s.")


def ensure_git_repo(repo: Path) -> None:
    result = run_command("git rev-parse --show-toplevel", repo)
    if result.code != 0:
        raise AgentError(f"Not a git repository: {repo}\n{result.combined}")


def ensure_clean_worktree(repo: Path, allow_dirty: bool) -> None:
    if allow_dirty:
        return
    result = run_command("git status --porcelain", repo)
    if result.code != 0:
        raise AgentError(result.combined)
    if result.stdout.strip():
        raise AgentError(
            "Git worktree is not clean. Commit or stash changes first, or pass --allow-dirty.\n"
            + result.stdout
        )


def is_excluded(path: Path) -> bool:
    if set(path.parts) & EXCLUDE_DIRS:
        return True
    return any(fnmatch.fnmatch(path.name, pat) for pat in EXCLUDE_GLOBS)


def is_probably_text(path: Path, sample_bytes: int = 4096) -> bool:
    try:
        data = path.read_bytes()[:sample_bytes]
    except OSError:
        return False
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def truncate_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:]


def list_repo_files(repo: Path, max_files: int = 300) -> list[str]:
    result = run_command("git ls-files", repo)
    if result.code != 0:
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        rel = Path(line)
        if not is_excluded(rel):
            files.append(line)
        if len(files) >= max_files:
            break
    return files


def extract_candidate_paths(log: str, repo_files: Sequence[str]) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r'File "([^"]+)", line \d+',
        r"([A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|php|cs|cpp|c|h|hpp)):\d+",
        r"(?:E|ERROR|FAIL).*?([A-Za-z0-9_./\\-]+\.(?:py|js|jsx|ts|tsx|go|rs|java|rb|php|cs|cpp|c|h|hpp))",
    ]
    for pat in patterns:
        for match in re.finditer(pat, log):
            raw = match.group(1).replace("\\", "/").lstrip("./")
            if raw not in candidates:
                candidates.append(raw)

    repo_file_set = set(repo_files)
    filtered = [p for p in candidates if p in repo_file_set]
    if not filtered:
        for p in repo_files:
            lower = p.lower()
            if "test" in lower or lower.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
                filtered.append(p)
            if len(filtered) >= 20:
                break
    return filtered[:20]


def read_file_context(repo: Path, rel_path: str, max_chars: int) -> str:
    root = repo.resolve()
    path = (root / rel_path).resolve()
    if not str(path).startswith(str(root)):
        return ""
    if not path.exists() or not path.is_file() or not is_probably_text(path):
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(errors="replace")
    return f"\n--- FILE: {rel_path} ---\n{truncate_middle(content, max_chars)}\n"


def collect_context(config: AgentConfig, failure_log: str) -> str:
    repo_files = list_repo_files(config.repo)
    sections = ["# Repository file list\n" + "\n".join(repo_files[:300])]

    for rel in IMPORTANT_FILES:
        if (config.repo / rel).exists():
            sections.append(read_file_context(config.repo, rel, config.max_file_chars))

    for rel in extract_candidate_paths(failure_log, repo_files):
        if rel not in IMPORTANT_FILES:
            sections.append(read_file_context(config.repo, rel, config.max_file_chars))

    return truncate_middle("\n".join(s for s in sections if s.strip()), config.max_context_chars)


def extract_unified_diff(text: str) -> str:
    fenced = re.search(r"```(?:diff|patch)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    positions = [pos for marker in ("diff --git ", "--- ") if (pos := text.find(marker)) != -1]
    if positions:
        text = text[min(positions):].strip()
    if not ("--- " in text and "+++ " in text and "@@" in text):
        raise AgentError("Model did not return a valid unified diff patch.")
    return text.rstrip() + "\n"


def changed_files_from_diff(diff: str) -> list[str]:
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                continue
            if path.startswith("b/"):
                path = path[2:]
            if path not in files:
                files.append(path)
    return files


def validate_patch_paths(repo: Path, diff: str) -> None:
    root = repo.resolve()
    for rel in changed_files_from_diff(diff):
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise AgentError(f"Patch tries to touch unsafe path: {rel}")
        target = (root / rel).resolve()
        if not str(target).startswith(str(root)):
            raise AgentError(f"Patch escapes repository: {rel}")
        if is_excluded(Path(rel)):
            raise AgentError(f"Patch touches excluded/generated/binary-like file: {rel}")


def apply_patch(repo: Path, diff: str) -> CommandResult:
    validate_patch_paths(repo, diff)
    proc = subprocess.run(
        "git apply --whitespace=fix -",
        cwd=str(repo),
        shell=True,
        text=True,
        input=diff,
        capture_output=True,
    )
    return CommandResult("git apply --whitespace=fix -", proc.returncode, proc.stdout, proc.stderr)


def build_messages(config: AgentConfig, failure: CommandResult, context: str, previous_patch: str | None) -> list[dict[str, str]]:
    system = """
You are a senior software maintenance agent. Fix the failing repository with the smallest safe patch.

Rules:
- Return ONLY a unified diff patch. No prose.
- Do not edit lock files, generated files, vendor files, binaries, or files outside the repository.
- Prefer minimal, targeted fixes over rewrites.
- Preserve public APIs unless the failure clearly requires changing them.
- Add or adjust tests only if necessary.
""".strip()
    user = "\n\n".join([
        f"Repository path: {config.repo}",
        f"Test/check command: {config.test_cmd}",
        "# Failure output\n" + truncate_middle(failure.combined, config.max_log_chars),
        "# Relevant repository context\n" + context,
        ("# Previous patch that did not fully fix the issue\n" + previous_patch) if previous_patch else "",
    ]).strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def call_llm(config: AgentConfig, messages: list[dict[str, str]]) -> str:
    if OpenAI is None:
        raise AgentError("Missing dependency. Run: pip install -r requirements.txt")
    client = OpenAI()
    response = client.chat.completions.create(
        model=config.model,
        messages=messages,  # type: ignore[arg-type]
        temperature=config.temperature,
    )
    return response.choices[0].message.content or ""


def section(title: str, body: str | None = None) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)
    if body:
        print(body)


def git_diff(repo: Path) -> str:
    result = run_command("git diff -- .", repo)
    return result.stdout if result.code == 0 else result.combined


def run_agent(config: AgentConfig) -> int:
    config.repo = config.repo.resolve()
    if not config.repo.exists() or not config.repo.is_dir():
        raise AgentError(f"Repo path does not exist or is not a directory: {config.repo}")

    ensure_git_repo(config.repo)
    ensure_clean_worktree(config.repo, config.allow_dirty)

    previous_patch: str | None = None
    for i in range(1, config.max_iterations + 1):
        section(f"Iteration {i}: running checks")
        failure = run_command(config.test_cmd, config.repo, config.command_timeout)
        print(f"Command: {failure.command}")
        print(f"Exit code: {failure.code}")

        if failure.code == 0:
            section("Success", "Checks are passing.")
            diff = git_diff(config.repo)
            if diff.strip():
                section("Final uncommitted diff", diff)
            return 0

        section("Failure log", truncate_middle(failure.combined, config.max_log_chars))
        context = collect_context(config, failure.combined)
        messages = build_messages(config, failure, context, previous_patch)

        section("Requesting patch from model")
        patch = extract_unified_diff(call_llm(config, messages))
        previous_patch = patch
        section("Proposed patch", patch)

        if config.dry_run:
            section("Dry run", "Patch was not applied.")
            return 1

        apply_result = apply_patch(config.repo, patch)
        if apply_result.code != 0:
            raise AgentError("Failed to apply patch.\n" + apply_result.combined)
        print(apply_result.combined or "Patch applied.")

    section("Stopped", f"Reached max iterations. Current diff:\n\n{git_diff(config.repo)}")
    return 2


def parse_args(argv: Sequence[str]) -> AgentConfig:
    parser = argparse.ArgumentParser(description="Auto Bugfix Agent MVP")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--test-cmd", required=True, help='Example: "pytest -q"')
    parser.add_argument("--model", default=os.getenv("AUTO_BUGFIX_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--max-iterations", type=int, default=3)
    parser.add_argument("--max-log-chars", type=int, default=20000)
    parser.add_argument("--max-file-chars", type=int, default=12000)
    parser.add_argument("--max-context-chars", type=int, default=90000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--command-timeout", type=int, default=120)
    args = parser.parse_args(argv)
    return AgentConfig(**vars(args))


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run_agent(parse_args(argv or sys.argv[1:]))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except AgentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
