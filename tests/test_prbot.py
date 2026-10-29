"""Tests for prbot, the Claude Code guard hook and the Codex gh shim. Run: python3 -m unittest -v"""
import base64, importlib.util, json, os, subprocess, sys, tempfile, unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


prbot = load("prbot", ROOT / "prbot")


class Classify(unittest.TestCase):
    cases = [
        (["pr", "review", "15", "--comment", "-b", "Looks off"], True),
        (["pr", "review", "15", "--approve"], True),
        (["pr", "comment", "15", "--body-file", "review.md"], True),
        (["issue", "comment", "9", "-b", "note"], True),
        (["pr", "comment", "15", "-b", "@codex review"], False),
        (["pr", "comment", "15", "--body=@codex address that feedback"], False),
        (["pr", "view", "15", "--json", "reviews"], False),
        (["pr", "diff", "15"], False),
        (["api", "repos/o/r/pulls/15/comments"], False),
        (["api", "repos/o/r/pulls/15/reviews", "-f", "event=COMMENT", "-f", "body=x"], True),
        (["api", "-X", "POST", "repos/o/r/issues/15/comments", "--input", "c.json"], True),
        (["api", "--method=PATCH", "repos/o/r/issues/comments/7", "-f", "body=y"], True),
        (["api", "repos/o/r/pulls/comments/123/replies", "-f", "body=x"], True),
        (["api", "repos/o/r/labels", "-f", "name=bug"], False),
        (["api", "graphql", "-f", "query=mutation { addPullRequestReview(input:{}) { clientMutationId } }"], True),
        (["api", "graphql", "-f", "query=query { viewer { login } }"], False),
    ]

    def test_cases(self):
        for args, want in self.cases:
            with self.subTest(args=args):
                self.assertEqual(prbot.is_feedback_write(args), want)

    def test_codex_trigger_moves_to_bot_when_enabled(self):
        self.assertTrue(prbot.is_feedback_write(["pr", "comment", "1", "-b", "@codex review"], codex_triggers_as_bot=True))


class ShellCommands(unittest.TestCase):
    def count(self, cmd):
        return len(prbot.unrouted_writes(cmd))

    def test_shell_forms(self):
        self.assertEqual(self.count('cd repo && gh pr comment 1 -b "hi there" | cat'), 1)
        self.assertEqual(self.count("GH_DEBUG=1 gh pr review 1 --approve"), 1)
        self.assertEqual(self.count("/opt/homebrew/bin/gh pr comment 1 -b x; gh pr view 1"), 1)
        self.assertEqual(self.count("prbot gh claude -- pr comment 1 -b hi"), 0)
        self.assertEqual(self.count("/x/pr-review-bots/prbot gh claude -- pr review 1 --comment -b ok"), 0)
        self.assertEqual(self.count('echo "gh pr comment 1 -b x"'), 0)
        self.assertEqual(self.count("gh pr view 15 --comments"), 0)
        self.assertEqual(self.count('gh pr comment 1 -b "unbalanced'), 1)


class GitPush(unittest.TestCase):
    def test_git_env_uses_only_the_bot_token(self):
        # git sees exactly one github.com helper: ours, which reads the token from the environment
        env = prbot.git_env(token="TEST-TOKEN")
        r = subprocess.run(["git", "config", "--get-urlmatch", "credential.helper", "https://github.com/o/r.git"],
                           env=env, capture_output=True, text=True)
        self.assertIn("PRBOT_GIT_TOKEN", r.stdout)
        self.assertNotIn("gh auth", r.stdout)
        self.assertNotIn("osxkeychain", r.stdout)
        self.assertEqual(env["PRBOT_GIT_TOKEN"], "TEST-TOKEN")
        self.assertNotIn("TEST-TOKEN", r.stdout)  # the token is never written into git config

    def test_git_writes_parsing(self):
        found = list(prbot.git_writes("git add . && git commit -m x && git -C repo push origin main; git status"))
        self.assertEqual(found, [(["commit", "-m", "x"], None), (["-C", "repo", "push", "origin", "main"], "repo")])
        self.assertEqual(list(prbot.git_writes("prbot git claude -- commit -m x && prbot git claude -- push")), [])
        self.assertEqual(list(prbot.git_writes('echo "git push"')), [])

    def test_commits_get_the_bot_identity(self):
        # regression: commits authored as Claude <noreply@anthropic.com> showed Anthropic's claude account on
        # GitHub, not claude-pr-gh[bot]; the bot's own noreply email is what links a commit to the bot
        email = "349557468+claude-pr-gh[bot]@users.noreply.github.com"
        env = prbot.git_env(identity=("claude-pr-gh[bot]", email))
        for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
            out = subprocess.run(["git", "var", var], env=env, capture_output=True, text=True).stdout
            self.assertTrue(out.startswith(f"claude-pr-gh[bot] <{email}>"), out)
        self.assertEqual(prbot.agent_of(email=email), "claude")
        self.assertEqual(prbot.agent_of(email="349558466+codex-pr-gh[bot]@users.noreply.github.com"), "codex")


class Repo(unittest.TestCase):
    def test_repo_from_args(self):
        self.assertEqual(prbot.repo_from_args(["pr", "comment", "1", "-R", "a/b"]), "a/b")
        self.assertEqual(prbot.repo_from_args(["pr", "comment", "1", "--repo=a/b"]), "a/b")
        self.assertEqual(prbot.repo_from_args(["api", "repos/o/r/pulls/1/reviews"]), "o/r")
        self.assertEqual(prbot.repo_from_args(["pr", "review", "https://github.com/o/r/pull/15"]), "o/r")
        self.assertIsNone(prbot.repo_from_args(["pr", "comment", "1"]))

    def test_repo_option_before_separator_is_not_passed_to_gh(self):
        # regression: `prbot gh claude -R o/r -- api ...` handed -R to gh api, which has no -R flag
        self.assertEqual(prbot.split_gh_args(["-R", "o/r", "--", "api", "/installation/repositories"]),
                         ("o/r", ["api", "/installation/repositories"]))
        self.assertEqual(prbot.split_gh_args(["--", "pr", "comment", "1", "-R", "o/r"]),
                         (None, ["pr", "comment", "1", "-R", "o/r"]))
        self.assertEqual(prbot.split_gh_args(["pr", "view", "1"]), (None, ["pr", "view", "1"]))


def commit(login, email, body=""):
    return {"authors": [{"login": login, "email": email}], "messageBody": body}


# shapes taken from kennylhilljr/openclaw-ecc-orchestrator PRs 15, 18 and 13
PR15 = {"labels": [{"name": "agent:codex"}], "commits": [commit("codex", "codex@openai.com")]}
PR18 = {"labels": [], "commits": [commit("claude", "noreply@anthropic.com")]}
PR13 = {"labels": [], "commits": [commit("openclaw-ops-assist[bot]", "349299589+openclaw-ops-assist[bot]@users.noreply.github.com")]}


class Routing(unittest.TestCase):
    def test_detect_author(self):
        self.assertEqual(prbot.detect_author(PR15), ("codex", "label agent:codex"))
        self.assertEqual(prbot.detect_author(PR18), ("claude", "commits by claude"))
        self.assertIsNone(prbot.detect_author(PR13)[0])
        mixed = {"labels": [], "commits": [commit("claude", "noreply@anthropic.com"), commit("codex", "codex@openai.com")]}
        self.assertEqual(prbot.detect_author(mixed), (None, "it has commits by both claude and codex"))
        both_labels = {"labels": [{"name": "agent:claude"}, {"name": "agent:codex"}], "commits": []}
        self.assertIsNone(prbot.detect_author(both_labels)[0])
        # a human commit co-authored by Claude counts as Claude's; the human co-author does not make it unclear
        trailer = {"labels": [], "commits": [commit("kennylhilljr", "kenny@example.com",
                                                    "fix\n\nCo-Authored-By: Claude <noreply@anthropic.com>")]}
        self.assertEqual(prbot.detect_author(trailer)[0], "claude")
        # the label beats the commits
        relabeled = {"labels": [{"name": "agent:claude"}], "commits": [commit("codex", "codex@openai.com")]}
        self.assertEqual(prbot.detect_author(relabeled)[0], "claude")

    def test_choose_reviewers(self):
        self.assertEqual(prbot.choose_reviewers("claude", [])[0], ["codex"])
        self.assertEqual(prbot.choose_reviewers("codex", ["agent:codex"])[0], ["claude"])
        self.assertEqual(prbot.choose_reviewers("codex", ["review:codex"])[0], ["codex"])
        self.assertEqual(prbot.choose_reviewers("claude", ["review:claude", "review:codex"])[0], ["claude", "codex"])
        self.assertEqual(prbot.choose_reviewers("claude", ["review:codex"], override="claude")[0], ["claude"])
        self.assertEqual(prbot.choose_reviewers(None, [], override="both")[0], ["claude", "codex"])
        self.assertEqual(prbot.choose_reviewers(None, []), ([], None))
        self.assertEqual(prbot.choose_reviewers(None, ["review:claude"])[0], ["claude"])

    def test_unclear_author_exits_3_and_asks(self):
        import io
        from contextlib import redirect_stderr
        orig = prbot.gh_read
        prbot.gh_read = lambda args: json.dumps(dict(PR13, number=13, title="t", url="u", headRefOid="abc"))
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                code = prbot.cmd_review("13", repo="o/r", mode="route")
        finally:
            prbot.gh_read = orig
        self.assertEqual(code, 3)
        self.assertIn("Ask the user which agent should review it", err.getvalue())


class Watch(unittest.TestCase):
    """One fake repo with a PR for each case the watcher has to decide."""
    SHA = {1: "a" * 40, 2: "b" * 40, 3: "c" * 40, 4: "d" * 40, 5: "e" * 40}
    VIEW = {1: PR15, 2: PR15, 3: PR18, 4: PR13, 5: PR15}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: getattr(prbot, k) for k in ("installed_repos", "gh_read", "cmd_review", "load_config", "WATCH_STATE")}
        prbot.WATCH_STATE = Path(self.tmp.name, "watch-state.json")
        prbot.load_config = lambda: {"claude": {"slug": "claude-pr-gh"}, "codex": {"slug": "codex-pr-gh"}}
        prbot.installed_repos = lambda bot: {"o/r"}
        prbot.gh_read = self.fake_gh
        self.reviews = {3: [["codex-pr-gh[bot]", "### Codex review\n<sub>Reviewed commit `" + "c" * 10 + "` · posted</sub>"]]}
        self.runs, self.fail = [], False

        def fake_review(n, repo, mode, only):
            self.runs.append((int(n), only[0]))
            if self.fail:
                raise SystemExit(2)
        prbot.cmd_review = fake_review

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(prbot, k, v)
        self.tmp.cleanup()

    def fake_gh(self, args):
        if args[:2] == ["pr", "list"]:
            return json.dumps([{"number": n, "isDraft": n == 1, "headRefOid": s} for n, s in self.SHA.items()])
        if args[:2] == ["pr", "view"]:
            return json.dumps(self.VIEW[int(args[2])])
        if args[0] == "api":
            n = int(args[1].split("/")[4])
            return "\n".join(json.dumps(r) for r in self.reviews.get(n, []))
        raise AssertionError(args)

    def run_pass(self, **kw):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            prbot.cmd_watch(**kw)
        return out.getvalue()

    def test_decisions(self):
        log = self.run_pass(limit=1)
        # draft 1 ignored; 2 (codex wrote it) gets a claude review; 3 already reviewed on this commit;
        # 4 unclear and skipped; 5 waits because the limit of 1 per pass is used up
        self.assertEqual(self.runs, [(2, "claude")])
        self.assertIn("o/r#4@dddddddddd: skipped, can't tell who wrote it", log)
        self.assertIn("o/r#5@eeeeeeeeee: claude review waits for the next pass", log)
        self.assertNotIn("#1@", log)
        # next pass: 2 now carries a claude review of its commit, 5 gets its turn, 4 is not logged again
        self.reviews[2] = [["claude-pr-gh[bot]", "Reviewed commit `" + "b" * 10 + "`"]]
        log = self.run_pass(limit=5)
        self.assertNotIn("#4@", log)
        self.assertEqual(self.runs, [(2, "claude"), (5, "claude")])

    def test_new_commit_gets_a_new_review(self):
        self.reviews[2] = [["claude-pr-gh[bot]", "Reviewed commit `" + "b" * 10 + "`"]]
        self.SHA = {**self.SHA, 2: "f" * 40}  # someone pushed to PR 2
        self.run_pass(limit=5)
        self.assertIn((2, "claude"), self.runs)

    def test_failed_review_waits_an_hour(self):
        self.fail = True
        self.run_pass(limit=5)
        self.assertEqual(sorted(self.runs), [(2, "claude"), (5, "claude")])
        self.run_pass(limit=5)
        self.assertEqual(len(self.runs), 2)  # not retried within RETRY_AFTER

    def test_baseline_skips_open_commits_but_not_new_ones(self):
        self.run_pass(baseline=True)
        self.run_pass(limit=5)
        self.assertEqual(self.runs, [])
        self.SHA = {**self.SHA, 5: "9" * 40}  # a push after the baseline
        self.run_pass(limit=5)
        self.assertEqual(self.runs, [(5, "claude")])

    def test_dry_run_posts_nothing_and_saves_nothing(self):
        log = self.run_pass(dry_run=True)
        self.assertEqual(self.runs, [])
        self.assertIn("would run a claude review", log)
        self.assertFalse(prbot.WATCH_STATE.exists())


class Jwt(unittest.TestCase):
    def test_signature_verifies_with_public_key(self):
        with tempfile.TemporaryDirectory() as d:
            key, pub = Path(d, "k.pem"), Path(d, "p.pem")
            subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
            subprocess.run(["openssl", "rsa", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
            token = prbot.app_jwt("Iv23liTEST", key.read_text(), now=1_800_000_000)
            h, p, s = token.split(".")
            claims = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
            self.assertEqual(claims, {"iat": 1_799_999_940, "exp": 1_800_000_540, "iss": "Iv23liTEST"})
            Path(d, "msg").write_bytes(f"{h}.{p}".encode())
            Path(d, "sig").write_bytes(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
            r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", Path(d, "sig"), Path(d, "msg")],
                               capture_output=True, text=True)
            self.assertIn("Verified OK", r.stdout)

    def test_worker_spec_from_another_repo(self):
        # issue worker bots keep their specs and logos in their own repo and need contents write
        with tempfile.TemporaryDirectory() as d:
            Path(d, "claude-dev.json").write_text(json.dumps({
                "name": "claude-dev-gh", "url": "https://github.com/kennylhilljr", "description": "worker",
                "logo": "logos/claude-dev.png",
                "permissions": {"contents": "write", "pull_requests": "write", "issues": "write"}}))
            spec = prbot.load_bot_spec("claude-dev", str(Path(d, "claude-dev.json")))
            self.assertEqual(spec["logo_path"], str(Path(d).resolve() / "logos/claude-dev.png"))
            perms = prbot.manifest(spec, "http://127.0.0.1:1/callback")["default_permissions"]
            self.assertEqual(perms, {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read"})

    def test_worker_bot_prs_route_to_the_other_agent(self):
        pr = {"labels": [], "commits": [commit("claude-dev-gh[bot]", "1+claude-dev-gh[bot]@users.noreply.github.com")]}
        self.assertEqual(prbot.detect_author(pr)[0], "claude")
        self.assertEqual(prbot.choose_reviewers("claude", [])[0], ["codex"])
        self.assertEqual(prbot.agent_of(email="2+codex-dev-gh[bot]@users.noreply.github.com"), "codex")

    def test_manifest_has_no_webhook_and_least_permissions(self):
        m = prbot.manifest(prbot.load_bot_spec("claude"), "http://127.0.0.1:1/callback")
        self.assertFalse(m["hook_attributes"]["active"])
        self.assertEqual(m["default_events"], [])
        self.assertEqual(m["default_permissions"]["contents"], "read")


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, PRBOT_CONFIG_DIR=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, command):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        r = subprocess.run([sys.executable, ROOT / "integrations/claude-code/guard_gh_identity.py"],
                           input=payload, capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_denies_write_with_fix(self):
        out = self.run_hook('gh pr review 15 --comment -b "Two P2 issues"')
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("gh claude -- pr review 15 --comment -b 'Two P2 issues'", out["hookSpecificOutput"]["permissionDecisionReason"])

    def run_tool(self, tool):
        r = subprocess.run([sys.executable, ROOT / "integrations/claude-code/guard_gh_identity.py"],
                           input=json.dumps({"tool_name": tool, "tool_input": {}}), capture_output=True, text=True, env=self.env)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_mcp_github_feedback_writes_denied(self):
        for tool in ("mcp__github__create_pull_request_review", "mcp__github__add_issue_comment",
                     "mcp__a202bf68-1247-4951-b92e-08a04ad5c797__Github_SubmitPullRequestReview",
                     "mcp__a202bf68-1247-4951-b92e-08a04ad5c797__Github_CreateReplyForReviewComment"):
            with self.subTest(tool=tool):
                self.assertEqual(self.run_tool(tool)["hookSpecificOutput"]["permissionDecision"], "deny")
        for tool in ("mcp__github__get_pull_request_comments", "mcp__github__list_pull_requests",
                     "mcp__a202bf68-1247-4951-b92e-08a04ad5c797__Github_ListReviewCommentsOnPullRequest",
                     "mcp__memory__create_entities"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.run_tool(tool))

    def make_repo(self, owner):
        d = tempfile.mkdtemp(dir=self.tmp.name)
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "remote", "add", "origin", f"https://github.com/{owner}/demo.git"], check=True)
        return d

    def run_hook_in(self, command, cwd):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd})
        r = subprocess.run([sys.executable, ROOT / "integrations/claude-code/guard_gh_identity.py"],
                           input=payload, capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_git_push_to_bot_repos_goes_through_prbot(self):
        Path(self.tmp.name, "config.json").write_text(json.dumps({"claude": {"owner": "kennylhilljr", "slug": "claude-pr-gh"}}))
        mine, theirs = self.make_repo("kennylhilljr"), self.make_repo("someone-else")
        out = self.run_hook_in("git push origin main", mine)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("git claude -- push origin main", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.run_hook_in(f"git -C {mine} push", self.tmp.name)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.run_hook_in("git push origin main", theirs))
        self.assertIsNone(self.run_hook_in("prbot git claude -- push origin main", mine))
        self.assertIsNone(self.run_hook_in("git status && git log -1", mine))
        out = self.run_hook_in('git commit -m "fix"', mine)
        self.assertIn("git claude -- commit -m fix", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_allows_reads_and_routed_writes(self):
        self.assertIsNone(self.run_hook("gh pr view 15 --json reviews"))
        self.assertIsNone(self.run_hook("prbot gh claude -- pr comment 15 -b ok"))


class CodexShim(unittest.TestCase):
    def test_routes_writes_and_passes_reads(self):
        with tempfile.TemporaryDirectory() as cfg:
            env = dict(os.environ, PRBOT_CONFIG_DIR=cfg,
                       PATH=f"{ROOT / 'integrations/codex/bin'}{os.pathsep}{os.environ['PATH']}")
            env.pop("PRBOT_ACTIVE", None)
            read = subprocess.run(["gh", "--version"], capture_output=True, text=True, env=env)
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertIn("gh version", read.stdout)
            # with no registered bot, a routed write stops inside prbot instead of posting as the user
            write = subprocess.run(["gh", "pr", "comment", "1", "-R", "o/r", "-b", "x"], capture_output=True, text=True, env=env)
            self.assertNotEqual(write.returncode, 0)
            self.assertIn("bot 'codex' is not registered", write.stderr)


if __name__ == "__main__":
    unittest.main()
