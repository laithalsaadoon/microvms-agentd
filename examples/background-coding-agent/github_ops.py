# SPDX-License-Identifier: Apache-2.0
"""GitHub reads before the VM starts and writes after it stops.

The token stays in Lambda. The guest receives a source tarball and the task text,
so an agent steered by a hostile issue has no GitHub credential to misuse.
"""

import base64
import io
import json
import os
import re
import tarfile
from functools import cache
from itertools import islice

import boto3
import requests
from github import Auth, Github, GithubException, InputGitTreeElement

MAX_ARCHIVE = 200 * 1024 * 1024
MAX_DIFF = 2 * 1024 * 1024
MAX_FILES = 300
MAX_BODY = 60_000
MAX_COMMENTS = 50
# A Bedrock bearer token is installed in the guest; never publish one.
LEAK = re.compile(r"bedrock-api-key-[A-Za-z0-9+/=_-]{16,}")


def marker(job_id: str) -> str:
    return f"<!-- background-coding-agent:{job_id} -->"


@cache
def token() -> str:
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["GITHUB_SECRET"]
    )
    return secret["SecretString"].strip()


@cache
def client() -> Github:
    return Github(auth=Auth.Token(token()), timeout=30)


def redact(text: str) -> str:
    return LEAK.sub("[redacted credential]", text)


def download(url: str, limit: int, headers: dict | None = None) -> bytes:
    with requests.get(url, headers=headers, stream=True, timeout=60) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(1 << 20):
            data += chunk
            if len(data) > limit:
                raise ValueError(f"{url} exceeds {limit} bytes")
    return bytes(data)


def restrip(archive: bytes) -> bytes:
    """Repack a GitHub tarball without its `owner-repo-sha/` top directory."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as source,
        tarfile.open(fileobj=out, mode="w") as target,
    ):
        for member in source:
            _, _, name = member.name.partition("/")
            if not name or not (member.isfile() or member.isdir() or member.issym()):
                continue
            member.name = name
            target.addfile(
                member, source.extractfile(member) if member.isfile() else None
            )
    return out.getvalue()


def pack(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def untrusted(title: str, body: str | None) -> str:
    return (
        f"<untrusted-github-content>\n# {title}\n\n{body or '(no description)'}\n"
        "</untrusted-github-content>\n"
    )


def stage(repo_name: str, number: int, note: str | None):
    """Return (metadata, source tar, task tar) for an issue or an open PR."""
    repo = client().get_repo(repo_name)
    issue = repo.get_issue(number)
    notes = f"\n## Submitter note\n\n{note}\n" if note else ""
    if issue.pull_request:
        pull = repo.get_pull(number)
        if pull.state != "open":
            raise ValueError(f"{repo_name}#{number} is not an open pull request")
        ref = pull.head.sha
        diff = download(
            pull.url,
            MAX_DIFF,
            {
                "Authorization": f"Bearer {token()}",
                "Accept": "application/vnd.github.diff",
            },
        )
        request = (
            f"Pull request {pull.html_url}: `{pull.head.ref}` into `{pull.base.ref}` "
            f"at {ref}.\n\n" + untrusted(pull.title, pull.body) + notes
        )
        files = {"request.md": request.encode(), "pr.diff": diff}
        meta = {"kind": "review", "head_sha": ref}
    else:
        base_ref = repo.default_branch
        ref = repo.get_branch(base_ref).commit.sha
        comments = "".join(
            f"\n### Comment by @{comment.user.login}\n\n{comment.body}\n"
            # Iterate rather than slice: PyGithub's slice of an empty list raises.
            for comment in islice(issue.get_comments(), 30)
        )
        request = (
            f"Issue {issue.html_url} on `{base_ref}` at {ref}.\n\n"
            + untrusted(issue.title, (issue.body or "") + comments)
            + notes
        )
        files = {"request.md": request.encode()}
        meta = {"kind": "implement", "base_sha": ref, "base_ref": base_ref}
    meta |= {"title": issue.title, "url": issue.html_url}
    source = restrip(download(repo.get_archive_link("tarball", ref), MAX_ARCHIVE))
    return meta, source, pack(files)


def parse_raw(raw: bytes) -> list[tuple[str, str, str]]:
    """(status, new mode, path) from `git diff --raw -z --no-renames`."""
    fields = raw.split(b"\0")
    entries = []
    for header, path in zip(fields[::2], fields[1::2], strict=False):
        old, new, _, _, status = header.decode().split(" ")
        entries.append((status[0], old[1:] if status[0] == "D" else new, path.decode()))
    return entries


def open_pull_request(job, meta: dict, bundle: dict[str, bytes]) -> str | None:
    """Commit the agent's changes on a new branch and open a draft PR. Retry-safe."""
    entries = [e for e in parse_raw(bundle["changes.raw"]) if e[1] != "160000"]
    if not entries:
        return None
    if len(entries) > MAX_FILES:
        raise ValueError(f"agent changed {len(entries)} files; limit is {MAX_FILES}")
    with tarfile.open(fileobj=io.BytesIO(bundle["changes.tar"])) as archive:
        contents = {
            member.name: member.linkname.encode()
            if member.issym()
            else archive.extractfile(member).read()
            for member in archive
            if member.isfile() or member.issym()
        }
    repo = client().get_repo(job.repo)
    tree = []
    for status, mode, path in entries:
        if status == "D":
            tree.append(InputGitTreeElement(path, mode, "blob", sha=None))
            continue
        data = contents[path]
        if LEAK.search(data.decode("latin-1")):
            raise ValueError(f"{path} contains a credential; refusing to publish")
        blob = repo.create_git_blob(base64.b64encode(data).decode(), "base64")
        tree.append(InputGitTreeElement(path, mode, "blob", sha=blob.sha))
    base = repo.get_git_commit(meta["base_sha"])
    commit = repo.create_git_commit(
        f"{meta['title']} (#{job.number})\n\nGenerated by {job.agent} for {meta['url']}",
        repo.create_git_tree(tree, base.tree),
        [base],
    )
    branch = f"agent/issue-{job.number}-{job.id[:8]}"
    try:
        repo.create_git_ref(f"refs/heads/{branch}", commit.sha)
    except GithubException as error:
        if error.status != 422:
            raise
        repo.get_git_ref(f"heads/{branch}").edit(commit.sha, force=True)
    for existing in repo.get_pulls(state="all", head=f"{repo.owner.login}:{branch}"):
        return existing.html_url
    report = redact(bundle.get("REPORT.md", b"").decode(errors="replace"))
    body = f"{report[:MAX_BODY]}\n\nCloses #{job.number}\n\n{marker(job.id)}"
    title = f"{meta['title']} (#{job.number})"
    try:
        pull = repo.create_pull(
            base=meta["base_ref"], head=branch, title=title, body=body, draft=True
        )
    except GithubException as error:
        # Draft PRs are unavailable on some private-repository plans.
        if error.status != 422 or "draft" not in json.dumps(error.data).lower():
            raise
        pull = repo.create_pull(
            base=meta["base_ref"], head=branch, title=title, body=body
        )
    return pull.html_url


def review_comments(raw: bytes | None) -> list[dict]:
    try:
        items = json.loads(raw) if raw else []
        return [
            {
                "path": str(item["path"]),
                "line": int(item["line"]),
                "side": "RIGHT",
                "body": redact(str(item["body"])),
            }
            for item in items[:MAX_COMMENTS]
        ]
    except (ValueError, TypeError, KeyError):
        return []


def post_review(job, meta: dict, bundle: dict[str, bytes]) -> str:
    """Post one COMMENT review; never approves or requests changes. Retry-safe."""
    repo = client().get_repo(job.repo)
    pull = repo.get_pull(job.number)
    for review in pull.get_reviews():
        if marker(job.id) in (review.body or ""):
            return review.html_url
    report = redact(bundle.get("REPORT.md", b"").decode(errors="replace"))
    body = f"{report[:MAX_BODY]}\n\n{marker(job.id)}"
    commit = repo.get_commit(meta["head_sha"])
    comments = review_comments(bundle.get("comments.json"))
    try:
        review = pull.create_review(
            commit=commit, body=body, event="COMMENT", comments=comments
        )
    except GithubException as error:
        # GitHub rejects the whole review when one comment misses the diff.
        if error.status != 422 or not comments:
            raise
        inline = "".join(
            f"\n- `{c['path']}:{c['line']}`: {c['body']}" for c in comments
        )
        body = f"{report[:MAX_BODY]}\n\n### Line comments\n{inline[:5000]}\n\n{marker(job.id)}"
        review = pull.create_review(commit=commit, body=body, event="COMMENT")
    return review.html_url
