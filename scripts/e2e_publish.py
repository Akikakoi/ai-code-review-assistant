"""publish 链路的真实端到端验证（文档 §14.4）。

`GithubPublisher` 的单测很齐（`event` 必须是 COMMENT、幂等跳过、Check Run 结论映射…），
但**全部用 respx 拦截**。这个脚本做那件单测做不到的事：对**真实 GitHub PR**
发一次 review，然后从平台侧回读断言 —— 评论真的出现在 PR 上了吗？

流程（文档 §14.4 要求的正是这条）：

    1. 用已推送的分支建一个 PR
    2. `acra review --publish` 真发一次
    3. **从平台侧**断言：PR 上存在本工具发的 review、行级评论数对得上
    4. 再跑一次，断言幂等（同一 head_sha 不重复发布）
    5. 收尾：关 PR，删分支

为什么断言必须回读平台而不是看本地返回值：本地返回的是"我发了什么"，
平台侧才是"到底有没有到"。两者不一致的情况真实存在（422 整批被拒、
限流、锚定行不在 PR diff 里），而它们的共同表现是"本地没报错"。

前置：
  - 发布凭据（`ACRA_GITHUB_TOKEN`，或 App 的三项配置）
  - 分支已推送到远端，且包含至少一处可被审出的变更

用法：
    python scripts/e2e_publish.py --branch e2e-publish-probe
    python scripts/e2e_publish.py --branch xxx --keep     # 保留分支与 PR 以便人工查看
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from acra.models import ReviewJob  # noqa: E402
from acra.publish.factory import resolve_access  # noqa: E402
from acra.publish.renderer import BOT_MARKER  # noqa: E402
from acra.settings import get_settings  # noqa: E402

GIT_ID = ["-c", "core.autocrlf=false"]


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *GIT_ID, *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def owner_repo() -> tuple[str, str]:
    url = git("remote", "get-url", "origin")
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if not match:
        raise SystemExit(f"无法从远端地址解析 owner/repo：{url}")
    return match.group(1), match.group(2)


class Api:
    def __init__(self, token: str) -> None:
        self.client = httpx.Client(
            base_url="https://api.github.com",
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.client.close()

    def open_pr(self, owner: str, repo: str, *, branch: str, base: str, title: str) -> dict:
        resp = self.client.post(
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": branch, "base": base, "body": "publish 链路验证用，随后自动关闭。"},
        )
        if resp.status_code >= 400:
            raise SystemExit(f"建 PR 失败（HTTP {resp.status_code}）：{resp.text[:300]}")
        return resp.json()

    def reviews(self, owner: str, repo: str, pr: int) -> list[dict]:
        resp = self.client.get(f"/repos/{owner}/{repo}/pulls/{pr}/reviews", params={"per_page": 100})
        return resp.json() if resp.status_code < 400 else []

    def review_comments(self, owner: str, repo: str, pr: int) -> list[dict]:
        resp = self.client.get(
            f"/repos/{owner}/{repo}/pulls/{pr}/comments", params={"per_page": 100}
        )
        return resp.json() if resp.status_code < 400 else []

    def close_pr(self, owner: str, repo: str, pr: int) -> int:
        return self.client.patch(f"/repos/{owner}/{repo}/pulls/{pr}", json={"state": "closed"}).status_code

    def check_run(self, owner: str, repo: str, run_id: int) -> dict:
        resp = self.client.get(f"/repos/{owner}/{repo}/check-runs/{run_id}")
        return resp.json() if resp.status_code < 400 else {}


def run_review(*, owner: str, repo: str, base: str, branch: str, pr: int, out: Path) -> dict:
    cmd = [
        sys.executable, "-m", "acra.cli", "review",
        "--repo", ".", "--base", base, "--head", branch,
        "--pr", str(pr), "--repo-full-name", f"{owner}/{repo}",
        "--publish", "--format", "json", "--out", str(out),
    ]
    print("  → " + " ".join(cmd[4:]), flush=True)
    proc = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ},
    )
    if not out.exists():
        raise SystemExit(
            f"审查未产出结果（rc={proc.returncode}）：\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
        )
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--branch", required=True, help="已推送到远端的测试分支")
    parser.add_argument("--base", default="main", help="PR 的目标分支")
    parser.add_argument("--keep", action="store_true", help="保留 PR 与分支（便于人工查看）")
    args = parser.parse_args()

    settings = get_settings()
    owner, repo = owner_repo()
    branch = args.branch

    job = ReviewJob(
        job_id="e2e-publish",
        platform="github",
        repo_full_name=f"{owner}/{repo}",
        repo_path=".",
        remote_url=None,
    )
    access = asyncio.run(resolve_access(settings, job))
    if not access.available:
        raise SystemExit(
            f"没有可用的发布凭据（来源={access.source}，原因={access.error}）。\n"
            "配置 ACRA_GITHUB_TOKEN，或 GITHUB_APP_ID / _PRIVATE_KEY_PATH / _INSTALLATION_ID。"
        )

    out_dir = settings.ensure_workdir()
    out_path = out_dir / "e2e-publish.json"
    results: list[tuple[str, bool, str]] = []
    pr_number: int | None = None

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))

    with Api(access.token) as api:
        try:
            print(f"1. 建 PR：{owner}/{repo}  {branch} -> {args.base}")
            pr = api.open_pr(
                owner, repo, branch=branch, base=args.base,
                title=f"e2e: publish 链路验证（{branch}）",
            )
            pr_number = pr["number"]
            head_sha = pr["head"]["sha"]
            print(f"   PR #{pr_number}  head={head_sha[:8]}")

            print("2. 跑一次真实发布")
            first = run_review(
                owner=owner, repo=repo, base=args.base, branch=branch, pr=pr_number, out=out_path
            )
            published = (first.get("job") or {}).get("publish") or {}
            check("本地返回了 review_id", bool(published.get("review_id")), json.dumps(published, ensure_ascii=False))

            print("3. 从平台侧回读（关键：断言的是'到底有没有到'）")
            reviews = api.reviews(owner, repo, pr_number)
            mine = [r for r in reviews if BOT_MARKER in (r.get("body") or "")]
            check("PR 上存在本工具发的 review", len(mine) == 1, f"平台侧 review 数={len(reviews)}，含标记={len(mine)}")
            if mine:
                check(
                    "平台侧 review id 与本地一致",
                    mine[0].get("id") == published.get("review_id"),
                    f"平台={mine[0].get('id')} 本地={published.get('review_id')}",
                )
            comments = api.review_comments(owner, repo, pr_number)
            check(
                "行级评论数与本地一致",
                len(comments) == (published.get("comments") or 0),
                f"平台={len(comments)} 本地={published.get('comments')}",
            )
            if access.source == "app":
                # App 形态下 Check Run **应该**建得出来 —— 这正是"只有 App 才能回答"的问题
                # （实测细粒度 PAT 是 403）。所以这里失败要算 FAIL，不能只当警告。
                if published.get("check_run_id"):
                    cr = api.check_run(owner, repo, int(published["check_run_id"]))
                    check(
                        "App 形态建出了 Check Run（只有 App 能做到）",
                        bool(cr) and str(cr.get("head_sha") or "").startswith(head_sha[:8]),
                        f"id={cr.get('id')} head={str(cr.get('head_sha'))[:8]}",
                    )
                else:
                    check(
                        "App 形态建出了 Check Run（只有 App 能做到）",
                        False,
                        str(published.get("check_run_error"))[:200],
                    )
            elif published.get("check_run_error"):
                print(f"   ℹ Check Run 失败属预期（PAT 建不了）：{published['check_run_error'][:90]}")

            print("4. 幂等：同一 head_sha 再跑一次")
            second = run_review(
                owner=owner, repo=repo, base=args.base, branch=branch, pr=pr_number, out=out_path
            )
            again = (second.get("job") or {}).get("publish") or {}
            check(
                "重复发布被幂等跳过",
                again.get("skipped") is True and again.get("reason") == "already_reviewed",
                json.dumps(again, ensure_ascii=False),
            )
            check(
                "平台侧 review 数未增加",
                len(api.reviews(owner, repo, pr_number)) == len(reviews),
            )
        finally:
            if pr_number is not None:
                if args.keep:
                    print(f"5. --keep：保留 PR #{pr_number} 与分支 {branch}")
                else:
                    print("5. 收尾：关 PR、删分支")
                    print(f"   关闭 PR #{pr_number} -> HTTP {api.close_pr(owner, repo, pr_number)}")
                    try:
                        git("push", "origin", "--delete", branch)
                        print(f"   已删除远端分支 {branch}")
                    except SystemExit as exc:
                        print(f"   ⚠ 删除分支失败（可手工删）：{exc}")

    failed = [r for r in results if not r[1]]
    print("")
    print(f"结果：{len(results) - len(failed)}/{len(results)} 通过")
    (out_dir / "e2e-publish-result.json").write_text(
        json.dumps({"results": [{"name": n, "ok": o, "detail": d} for n, o, d in results]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
