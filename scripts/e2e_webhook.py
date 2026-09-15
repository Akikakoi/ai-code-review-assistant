"""webhook 链路的真实端到端验证。

验签此前只有单测：拿手写的 payload 在函数级断言 `verify_signature` 的真假。
这条链路真正值得验的是**从 HTTP 请求到"评论真的发出去"**的全程：

    真实 ASGI 服务 → 真实签名 → 验签 → 归一化 → 入队 → worker → 审查 → 发布

覆盖的判定：

    1. 正确签名的投递被接受，并且最终在 PR 上出现本工具发的 review
    2. 篡改签名的投递被 401 拒绝，且不产生任何 review
    3. 同一 delivery id 重投 → 幂等跳过，不重复发评论

`payload.repository` 来自真实 API（`GET /repos/{o}/{r}`）。`clone_url` 刻意取其中的
**`ssh_url`** 而不是默认的 `https://github.com/...`：两个 URL 在真实 payload 里本来就
并存，本机 git 的 HTTPS 出不去（系统代理指向失效端口）而 SSH 是通的。
若 SSH 也不可用，用 `--clone-url <本地路径>` 退回本地传输。

用法：
    python scripts/e2e_webhook.py --branch <已推送的测试分支> [--base main] [--clone-url URL]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from acra.publish.factory import resolve_access  # noqa: E402
from acra.publish.renderer import BOT_MARKER  # noqa: E402
from acra.settings import get_settings  # noqa: E402

GIT_ID = ["-c", "core.autocrlf=false"]


def git(*args: str) -> str:
    proc = subprocess.run(["git", *GIT_ID, *args], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout.strip()


def owner_repo() -> tuple[str, str]:
    url = git("remote", "get-url", "origin")
    import re

    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if not match:
        raise SystemExit(f"无法从远端地址解析 owner/repo：{url}")
    return match.group(1), match.group(2)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sign(secret: str, body: bytes) -> str:
    """GitHub 的签名格式：`sha256=` + HMAC-SHA256(hex)，对**原始字节**计算。"""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class Api:
    def __init__(self, token: str) -> None:
        self.client = httpx.Client(
            base_url="https://api.github.com", timeout=30.0,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )

    def repo(self, owner: str, repo: str) -> dict:
        resp = self.client.get(f"/repos/{owner}/{repo}")
        if resp.status_code >= 400:
            raise SystemExit(f"读取仓库失败（HTTP {resp.status_code}）：{resp.text[:200]}")
        return resp.json()

    def open_pr(self, owner: str, repo: str, *, branch: str, base: str, title: str) -> dict:
        resp = self.client.post(f"/repos/{owner}/{repo}/pulls",
                                json={"title": title, "head": branch, "base": base,
                                      "body": "webhook 链路验证用，随后自动关闭。"})
        if resp.status_code >= 400:
            raise SystemExit(f"建 PR 失败（HTTP {resp.status_code}）：{resp.text[:300]}")
        return resp.json()

    def reviews(self, owner: str, repo: str, pr: int) -> list[dict]:
        resp = self.client.get(f"/repos/{owner}/{repo}/pulls/{pr}/reviews", params={"per_page": 100})
        return resp.json() if resp.status_code < 400 else []

    def close_pr(self, owner: str, repo: str, pr: int) -> int:
        return self.client.patch(f"/repos/{owner}/{repo}/pulls/{pr}", json={"state": "closed"}).status_code


def start_server(settings, workdir: Path, secret: str, port: int) -> subprocess.Popen:
    env = {
        **os.environ,
        "GITHUB_WEBHOOK_SECRET": secret,
        "ACRA_WORKDIR": str(workdir),
        "ACRA_INLINE_WORKER": "true",
        "ACRA_GITHUB_TOKEN": settings.acra_github_token or "",
        # 这几个置空，确保走的是静态 token 这条路（与 .env 里的 App 配置无关）
        "GITHUB_APP_ID": "",
        "GITHUB_APP_PRIVATE_KEY_PATH": "",
        "GITHUB_APP_INSTALLATION_ID": "",
    }
    cmd = [sys.executable, "-m", "uvicorn", "acra.main:app",
           "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    return subprocess.Popen(cmd, cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
            if resp.status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.4)
    return False


def deliver(port: int, *, event: str, delivery: str, body: bytes, secret: str | None) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": sign(secret, body) if secret else "sha256=" + "0" * 64,
    }
    return httpx.post(f"http://127.0.0.1:{port}/webhook/github", content=body, headers=headers, timeout=30.0)


def wait_for_review(api: Api, owner: str, repo: str, pr: int, baseline: int, timeout: float) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        reviews = [r for r in api.reviews(owner, repo, pr) if BOT_MARKER in (r.get("body") or "")]
        if len(reviews) > baseline:
            return reviews
        time.sleep(3.0)
    return api.reviews(owner, repo, pr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--branch", required=True, help="已推送到远端的测试分支")
    parser.add_argument("--base", default="main")
    parser.add_argument("--clone-url", default=None,
                        help="覆盖 payload 的 clone_url；默认取真实 payload 里的 ssh_url")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.acra_github_token:
        raise SystemExit("没有发布凭据（ACRA_GITHUB_TOKEN），无法验证链路终点")

    owner, repo = owner_repo()
    access_job = SimpleNamespace(platform="github", repo_full_name=f"{owner}/{repo}",
                                 repo_path=".", remote_url=None)
    import asyncio

    access = asyncio.run(resolve_access(settings, access_job))
    if not access.available:
        raise SystemExit(f"发布凭据不可用：{access.error}")

    port = free_port()
    secret = uuid.uuid4().hex
    workdir = settings.ensure_workdir() / "webhook-e2e"
    results: list[tuple[str, bool, str]] = []
    pr_number: int | None = None
    server: subprocess.Popen | None = None

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))

    api = Api(access.token)
    try:
        print(f"1. 启动真实 ASGI 服务（127.0.0.1:{port}，内联 worker 开启）")
        server = start_server(settings, workdir, secret, port)
        if not wait_ready(port):
            raise SystemExit("服务未能在 30s 内就绪")
        check("服务就绪", True, f"port={port}")

        print(f"2. 建 PR（{owner}/{repo}  {args.branch} -> {args.base}）")
        pr = api.open_pr(owner, repo, branch=args.branch, base=args.base,
                         title=f"e2e: webhook 链路验证（{args.branch}）")
        pr_number = pr["number"]
        head_sha = pr["head"]["sha"]
        print(f"   PR #{pr_number}  head={head_sha[:8]}")

        # repository 对象来自真实 API；clone_url 刻意取 ssh_url（见模块 docstring）
        repo_obj = api.repo(owner, repo)
        clone_url = args.clone_url or repo_obj.get("ssh_url") or repo_obj.get("clone_url")
        payload = {
            "action": pr.get("state") == "closed" and "closed" or "opened",
            "number": pr["number"],
            "pull_request": pr,
            "repository": {**repo_obj, "clone_url": clone_url},
            "sender": {"login": access.source},
        }
        body = json.dumps(payload).encode("utf-8")
        print(f"   payload.clone_url = {clone_url}（真实 repository 对象的 ssh_url）")

        print("3. 投递一次正确签名的 pull_request 事件")
        delivery = str(uuid.uuid4())
        resp = deliver(port, event="pull_request", delivery=delivery, body=body, secret=secret)
        check("投递被接受（2xx）", resp.status_code < 300, f"HTTP {resp.status_code} {resp.text[:120]}")

        print("4. 等待 worker 完成审查并发布（最长 300s）")
        reviews = wait_for_review(api, owner, repo, pr_number, baseline=0, timeout=300.0)
        check("PR 上出现本工具发的 review", len(reviews) >= 1, f"平台侧含标记 review={len(reviews)}")

        print("5. 篡改签名必须被拒绝")
        before = len(api.reviews(owner, repo, pr_number))
        bad = deliver(port, event="pull_request", delivery=str(uuid.uuid4()),
                      body=body, secret=None)
        check("伪造签名返回 401", bad.status_code == 401, f"HTTP {bad.status_code}")
        time.sleep(2.0)
        check("被拒的投递没有产生 review", len(api.reviews(owner, repo, pr_number)) == before)

        print("6. 同一 delivery 重投 → 幂等")
        again = deliver(port, event="pull_request", delivery=delivery, body=body, secret=secret)
        check("重投被接受", again.status_code < 300, f"HTTP {again.status_code}")
        time.sleep(3.0)
        after = [r for r in api.reviews(owner, repo, pr_number) if BOT_MARKER in (r.get("body") or "")]
        check("没有重复发布", len(after) == len(reviews), f"重投后含标记 review={len(after)}")
    finally:
        if pr_number is not None and not args.keep:
            print("7. 收尾：关 PR、删分支、停服务")
            print(f"   关闭 PR #{pr_number} -> HTTP {api.close_pr(owner, repo, pr_number)}")
            try:
                git("push", "origin", "--delete", args.branch)
                print(f"   已删除远端分支 {args.branch}")
            except SystemExit as exc:
                print(f"   ⚠ 删除分支失败（可手工删）：{exc}")
        elif args.keep:
            print("7. --keep：保留 PR 与分支")
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    failed = [r for r in results if not r[1]]
    print("")
    print(f"结果：{len(results) - len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
