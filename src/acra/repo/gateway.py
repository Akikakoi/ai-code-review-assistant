"""git 接入：克隆 / fetch / merge-base / diff / 读文件。

对应开发文档 §4.3。

统一使用 `git show <sha>:<path>` 读取文件内容，因此本地仓库与裸仓库缓存走完全相同的
读路径，不需要区分 worktree。这样"本地 CLI 模式"与"平台模式"的行为一致，
也避免了每次审查都做一次全量克隆。

认证：GitHub App installation token 通过 `http.extraHeader` 注入，不落盘
（文档 §4.3）。裸仓库按 repo_id 缓存复用。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from acra.errors import CloneError, GitError

_GIT_TIMEOUT = 180


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # 绝不交互式索要凭据（否则 CI 会挂住）
            "GIT_PAGER": "cat",
            "GIT_ASKPASS": "",
            "LC_ALL": "C",  # 固定英文输出，便于匹配错误信息
            "LANG": "C",
        }
    )
    return env


def repo_id_for(identifier: str | Path) -> str:
    """稳定仓库标识，用于裸仓库缓存目录名、幂等键与缓存键。"""
    normalized = str(identifier).replace("\\", "/").rstrip("/")
    if not normalized.startswith(("http://", "https://", "git@")):
        with contextlib.suppress(OSError):
            normalized = str(Path(normalized).resolve()).replace("\\", "/")
    return hashlib.sha1(normalized.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class RepoHandle:
    """一个可操作的 git 仓库视图。"""

    repo_id: str
    root: Path
    is_bare: bool = False
    remote_url: str | None = None
    token: str | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ 内部

    def _prefix(self) -> list[str]:
        if self.is_bare:
            return ["--git-dir", str(self.root)]
        return ["-C", str(self.root)]

    def _auth_args(self) -> list[str]:
        if not self.token:
            return []
        import base64

        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: int = _GIT_TIMEOUT,
        error_cls: type[GitError] = GitError,
    ) -> str:
        cmd = [
            "git",
            *self._prefix(),
            "-c",
            "core.quotepath=false",
            *self._auth_args(),
            *args,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=_git_env(),
            )
        except FileNotFoundError as exc:  # git 未安装
            raise GitError("git 可执行文件未找到，请先安装 git 并加入 PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise error_cls(f"git 超时（{timeout}s）：{_fmt(cmd)}") from exc

        if check and proc.returncode != 0:
            raise error_cls(
                f"git 失败（exit={proc.returncode}）：{_fmt(cmd)}\n{proc.stderr.strip()[:800]}"
            )
        return proc.stdout

    # ------------------------------------------------------------------ 查询

    def resolve_commit(self, ref: str) -> str:
        """把分支 / tag / 短 SHA 解析为 commit SHA。"""
        out = self._run(
            ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], check=False
        ).strip()
        if not out:
            raise GitError(f"无法解析引用：{ref}")
        return out

    def try_resolve_commit(self, ref: str) -> str | None:
        try:
            return self.resolve_commit(ref)
        except GitError:
            return None

    @property
    def is_shallow(self) -> bool:
        """浅克隆：历史被截断，任何需要共同祖先的操作（merge-base）都会失败。

        **只有远端克隆路径会遇到** —— CLI 用本地仓库，历史完整，本地怎么测都不会发现。
        """
        return (self.root / "shallow").is_file()

    def deepen(self, depth: int) -> bool:
        """把浅克隆加深到指定深度。失败返回 False，由调用方决定是否继续加深/降级。"""
        if not self.remote_url:
            return False
        args = ["fetch", "--no-tags", f"--depth={max(1, int(depth))}", "origin"]
        args += ["+refs/heads/*:refs/remotes/origin/*"]
        try:
            self._run(args, timeout=600, error_cls=CloneError)
        except CloneError:
            return False
        return True

    def merge_base(self, base: str, head: str) -> str:
        out = self._run(["merge-base", base, head], check=False).strip()
        if not out:
            raise GitError(f"无法计算 merge-base（{base} 与 {head} 可能无共同祖先）")
        return out

    def is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        proc_rc = self._returncode(["merge-base", "--is-ancestor", maybe_ancestor, descendant])
        return proc_rc == 0

    def _returncode(self, args: list[str]) -> int:
        cmd = ["git", *self._prefix(), *args]
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_GIT_TIMEOUT,
                env=_git_env(),
            ).returncode
        except (OSError, subprocess.TimeoutExpired):
            return 1

    def diff(self, merge_base_sha: str, head_sha: str, paths: list[str] | None = None) -> str:
        """三点 diff：`merge_base..head`，语义是"这个分支带来的变化"。"""
        args = [
            "diff",
            "--find-renames",
            "--find-copies",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            f"{merge_base_sha}..{head_sha}",
        ]
        if paths:
            args += ["--", *paths]
        return self._run(args)

    def changed_paths(self, merge_base_sha: str, head_sha: str) -> list[str]:
        out = self._run(
            ["diff", "--name-only", "-z", f"{merge_base_sha}..{head_sha}"]
        )
        return [p for p in out.split("\0") if p]

    def list_files(self, sha: str) -> list[str]:
        out = self._run(["ls-tree", "-r", "-z", "--name-only", sha])
        return [p for p in out.split("\0") if p]

    def show_file(self, sha: str, path: str) -> str | None:
        """读取指定 revision 下的文件内容；不存在或为二进制返回 None。"""
        out = self._run(["show", f"{sha}:{path}"], check=False)
        return out if out else None

    def file_lines(self, sha: str, path: str) -> list[str]:
        content = self.show_file(sha, path)
        if content is None:
            return []
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    def blob_sha(self, sha: str, path: str) -> str | None:
        out = self._run(["rev-parse", "--verify", "--quiet", f"{sha}:{path}"], check=False).strip()
        return out or None

    def head_sha(self) -> str:
        return self.resolve_commit("HEAD")

    def default_branch(self) -> str:
        """尽力探测默认分支：远端 HEAD → 常见分支名 → HEAD。"""
        for ref in ("refs/remotes/origin/HEAD", "refs/heads/main", "refs/heads/master"):
            out = self._run(["symbolic-ref", "--quiet", "--short", ref], check=False).strip()
            if out:
                return out.split("/")[-1]
        for candidate in ("main", "master"):
            if self.try_resolve_commit(candidate):
                return candidate
        return "HEAD"

    def describe(self) -> str:
        if self.is_bare:
            return f"bare:{self.root}"
        return f"local:{self.root}"


def _fmt(cmd: list[str]) -> str:
    """日志友好：隐藏可能包含凭据的 header。"""
    safe: list[str] = []
    for item in cmd:
        safe.append("http.extraHeader=***" if item.startswith("http.extraHeader=") else item)
    return " ".join(shlex.quote(x) for x in safe)


# ---------------------------------------------------------------------------- 构造


def discover_local(path: str | Path) -> RepoHandle:
    """把一个本地路径解析为仓库根（支持子目录）。"""
    p = Path(path).resolve()
    if not p.exists():
        raise GitError(f"路径不存在：{p}")
    probe = RepoHandle(repo_id="", root=p)
    top = probe._run(["rev-parse", "--show-toplevel"], check=False).strip()
    if top:
        root = Path(top).resolve()
    else:
        # 可能是裸仓库
        if (p / "HEAD").exists() and (p / "objects").exists():
            return RepoHandle(repo_id=repo_id_for(p), root=p, is_bare=True)
        raise GitError(f"不是 git 仓库：{p}")
    return RepoHandle(repo_id=repo_id_for(root), root=root, is_bare=False)


def bare_repo_path(workdir: Path, repo_id: str) -> Path:
    return workdir / "repos" / f"{repo_id}.git"


def ensure_bare_clone(
    remote_url: str,
    workdir: Path,
    *,
    token: str | None = None,
    depth: int = 1,
    refs: list[str] | None = None,
) -> RepoHandle:
    """按 repo_id 复用裸仓库，只 fetch 需要的 revision。

    对应文档 §4.3 的克隆策略：裸仓库缓存 + 浅克隆 + 按需 fetch，
    相比每次全量克隆省去绝大部分网络时间。
    """
    rid = repo_id_for(remote_url)
    root = bare_repo_path(workdir, rid)
    handle = RepoHandle(repo_id=rid, root=root, is_bare=True, remote_url=remote_url, token=token)

    if not (root / "HEAD").exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle._run(["init", "--bare", "--quiet", str(root)], check=True)
        except GitError as exc:
            raise CloneError(f"初始化裸仓库失败：{exc}") from exc
        handle._run(["remote", "add", "origin", remote_url], check=False)

    fetch_args = ["fetch", "--no-tags", f"--depth={max(1, depth)}", "origin"]
    fetch_args += list(refs) if refs else ["+refs/heads/*:refs/remotes/origin/*"]
    try:
        handle._run(fetch_args, timeout=600, error_cls=CloneError)
    except CloneError:
        raise
    return handle
