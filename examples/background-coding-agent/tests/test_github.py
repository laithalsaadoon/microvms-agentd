# SPDX-License-Identifier: Apache-2.0
"""Publishing from exported changes, against an in-memory GitHub."""

import base64
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace as NS

import github_ops
import handler
import pytest
from github import GithubException

JOB = NS(id="0123456789abcdef", repo="octo/app", number=7, agent="claude-code")
META = {
    "kind": "implement",
    "base_sha": "base",
    "base_ref": "main",
    "title": "Fix it",
    "url": "https://github.com/octo/app/issues/7",
}


class Repo:
    owner = NS(login="octo")

    def __init__(self, drafts=True):
        self.drafts, self.blobs, self.trees, self.refs, self.pulls = (
            drafts,
            [],
            [],
            {},
            [],
        )

    def create_git_blob(self, content, encoding):
        assert encoding == "base64"
        self.blobs.append(base64.b64decode(content))
        return NS(sha=f"blob-{len(self.blobs)}")

    def get_git_commit(self, sha):
        return NS(sha=sha, tree="base-tree")

    def create_git_tree(self, elements, base):
        assert base == "base-tree"
        self.trees.append({e._identity["path"]: e._identity for e in elements})
        return NS(sha="tree")

    def create_git_commit(self, message, tree, parents):
        return NS(sha=f"commit-{len(self.trees)}")

    def create_git_ref(self, ref, sha):
        if ref in self.refs:
            raise GithubException(422, {"message": "Reference already exists"})
        self.refs[ref] = sha

    def get_git_ref(self, ref):
        return NS(edit=lambda sha, force: self.refs.__setitem__(f"refs/{ref}", sha))

    def get_pulls(self, state, head):
        return [pull for pull in self.pulls if pull.head == head]

    def create_pull(self, base, head, title, body, draft=False):
        if draft and not self.drafts:
            raise GithubException(
                422, {"message": "Draft pull requests are not supported"}
            )
        pull = NS(
            head=f"octo:{head}",
            body=body,
            draft=draft,
            html_url=f"pr-{len(self.pulls)}",
        )
        self.pulls.append(pull)
        return pull


# A git hook exports GIT_DIR and GIT_INDEX_FILE, which would point these fixture commands
# at the repository running the hook instead of the temporary one.
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, env=CLEAN_ENV
    )


def export(tmp_path, change) -> dict[str, bytes]:
    """Run the workflow's real export script on a baseline repo after `change`."""
    project, results = tmp_path / "project", tmp_path / "results"
    project.mkdir(parents=True)
    results.mkdir()
    (project / "keep.py").write_text("old\n")
    (project / "gone.py").write_text("bye\n")
    git(project, "init", "-q")
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    change(project)
    script = handler.EXPORT.replace("/workspace/results", str(results))
    subprocess.run(["bash", "-c", script], cwd=project, check=True, env=CLEAN_ENV)
    (results / "REPORT.md").write_text("Fixed it.\n")
    return {path.name: path.read_bytes() for path in results.iterdir()}


def edit(project: Path):
    (project / "keep.py").write_text("new\n")
    (project / "gone.py").unlink()
    (project / "tool.sh").write_text("#!/bin/sh\n")
    (project / "tool.sh").chmod(0o755)
    (project / "link").symlink_to("keep.py")


def test_changes_become_one_commit_and_a_retry_safe_draft(tmp_path, monkeypatch):
    repo = Repo()
    monkeypatch.setattr(github_ops, "client", lambda: NS(get_repo=lambda _: repo))
    bundle = export(tmp_path, edit)
    url = github_ops.open_pull_request(JOB, META, bundle)
    tree = repo.trees[0]
    assert tree["gone.py"] == {
        "path": "gone.py",
        "mode": "100644",
        "type": "blob",
        "sha": None,
    }
    assert tree["tool.sh"]["mode"] == "100755"
    assert tree["link"]["mode"] == "120000"
    assert sorted(repo.blobs) == [b"#!/bin/sh\n", b"keep.py", b"new\n"]
    [pull] = repo.pulls
    assert url == pull.html_url and pull.draft
    assert "Closes #7" in pull.body and github_ops.marker(JOB.id) in pull.body
    # A retried step force-updates the branch and reuses the PR.
    assert github_ops.open_pull_request(JOB, META, bundle) == url
    assert len(repo.pulls) == 1
    assert repo.refs == {"refs/heads/agent/issue-7-01234567": "commit-2"}


def test_private_plans_without_drafts_get_a_ready_pr(tmp_path, monkeypatch):
    repo = Repo(drafts=False)
    monkeypatch.setattr(github_ops, "client", lambda: NS(get_repo=lambda _: repo))
    github_ops.open_pull_request(JOB, META, export(tmp_path, edit))
    assert [pull.draft for pull in repo.pulls] == [False]


def test_no_changes_publish_nothing_and_leaks_are_refused(tmp_path, monkeypatch):
    repo = Repo()
    monkeypatch.setattr(github_ops, "client", lambda: NS(get_repo=lambda _: repo))
    assert (
        github_ops.open_pull_request(JOB, META, export(tmp_path / "a", lambda _: None))
        is None
    )

    def leak(project):
        (project / "keep.py").write_text("TOKEN = 'bedrock-api-key-" + "A" * 40 + "'\n")

    with pytest.raises(ValueError, match="credential"):
        github_ops.open_pull_request(JOB, META, export(tmp_path / "b", leak))
    assert repo.pulls == []
    assert (
        github_ops.redact("x bedrock-api-key-" + "B" * 40) == "x [redacted credential]"
    )


class Pull:
    def __init__(self):
        self.reviews = []

    def get_reviews(self):
        return self.reviews

    def create_review(self, commit, body, event, comments=()):
        assert event == "COMMENT"
        if comments:
            raise GithubException(422, {"message": "Line could not be resolved"})
        review = NS(body=body, html_url=f"review-{len(self.reviews)}")
        self.reviews.append(review)
        return review


def test_review_falls_back_to_inline_text_and_is_posted_once(monkeypatch):
    pull = Pull()
    repo = NS(get_pull=lambda _: pull, get_commit=lambda sha: sha)
    monkeypatch.setattr(github_ops, "client", lambda: NS(get_repo=lambda _: repo))
    comments = [{"path": "app.py", "line": 3, "body": "Off by one."}, {"bad": 1}]
    bundle = {
        "REPORT.md": b"Looks risky.",
        "comments.json": json.dumps(comments).encode(),
    }
    meta = {"kind": "review", "head_sha": "head"}
    assert github_ops.review_comments(bundle["comments.json"]) == []
    bundle["comments.json"] = json.dumps(comments[:1]).encode()
    url = github_ops.post_review(JOB, meta, bundle)
    [review] = pull.reviews
    assert "`app.py:3`: Off by one." in review.body
    assert github_ops.marker(JOB.id) in review.body
    assert github_ops.post_review(JOB, meta, bundle) == url
    assert len(pull.reviews) == 1


def test_restrip_and_pack():
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w:gz") as archive:
        for name in ("octo-app-abc", "octo-app-abc/src/app.py"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE if "." not in name[-4:] else tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(b""))
    with tarfile.open(
        fileobj=io.BytesIO(github_ops.restrip(source.getvalue()))
    ) as archive:
        assert archive.getnames() == ["src/app.py"]
    with tarfile.open(fileobj=io.BytesIO(github_ops.pack({"a.md": b"hi"}))) as archive:
        assert archive.extractfile("a.md").read() == b"hi"
