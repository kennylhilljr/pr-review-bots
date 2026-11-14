"""Tests for prbot, the Claude Code guard hook and the Codex gh shim. Run: python3 -B -m unittest discover -s tests -t . -v"""
import base64, importlib.util, json, os, shlex, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    loader = SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


if "tests" not in sys.modules:
    _spec = importlib.util.spec_from_file_location("tests", Path(__file__).resolve().parent / "__init__.py")
    _boot = importlib.util.module_from_spec(_spec)
    sys.modules["tests"] = _boot
    _spec.loader.exec_module(_boot)

prbot = load("prbot", ROOT / "prbot")

# issue #20: every gh alias and extension bypass from the issue and its plan reviews. Each one was classified as a
# read on main, so the Codex shim ran the real gh as Kenny. (name, gh argv, gh config.yml or None). The config only
# shows what real gh would expand; classify never reads it.
SHELL_ALIAS = "aliases:\n    human-review: '!gh pr review 1 --approve'\n"
ISSUE_20_BYPASSES = [
    ("alias set", ["alias", "set", "human-review", "pr review"], None),
    ("alias set after -R", ["-R", "o/r", "alias", "set", "x", "pr review"], None),
    ("alias import", ["alias", "import", "aliases.yml"], None),
    ("alias delete", ["alias", "delete", "co"], None),
    ("user alias", ["human-review", "19", "--approve", "-b", "looks good"],
     "aliases:\n    human-review: pr review\n"),
    ("user alias with no config", ["human-review", "19", "--approve"], None),
    ("user alias after -R", ["-R", "o/r", "human-review", "19", "--approve"],
     "aliases:\n    human-review: pr review\n"),
    ("nested alias", ["nested", "19", "--approve"], "aliases:\n    nested: human-review\n    human-review: pr review\n"),
    ("alias with $1", ["say", "3"], "aliases:\n    say: 'pr comment $1 -b hi'\n"),
    ("shell alias", ["human-review"], SHELL_ALIAS),
    ("default co", ["co", "19"], None),
    ("co override, quoted aliases key", ["co", "19", "--approve"], "'aliases':\n  co: pr review\n"),
    ("co override, double-quoted key", ["co", "19", "--approve"], '"aliases":\n  co: pr review\n'),
    ("co override, space before colon", ["co", "19", "--approve"], "aliases :\n  co: pr review\n"),
    ("co override, flow mapping", ["co", "19", "--approve"], "aliases: {co: pr review}\n"),
    ("co override, block scalar", ["co", "19", "--approve"], "aliases:\n  co: |-\n    pr review\n"),
    ("co override, anchor", ["co", "19", "--approve"], "x: &a pr review\naliases:\n  co: *a\n"),
    ("subcommand alias", ["pr", "human-review", "19", "--approve"], "aliases:\n  'pr human-review': pr review\n"),
    ("subcommand alias after -R", ["pr", "-R", "o/r", "human-review", "19", "--approve"],
     "aliases:\n  'pr human-review': pr review\n"),
    ("subcommand alias after a leading -R", ["-R", "o/r", "issue", "human-review", "19"],
     "aliases:\n  'issue human-review': pr review 19 --approve\n"),
    ("ext subcommand alias", ["ext", "human-review", "19", "--approve"],
     "aliases:\n  'extension human-review': pr review\n"),
    ("extension subcommand alias", ["extension", "human-review", "19", "--approve"],
     "aliases:\n  'ext human-review': pr review\n"),
    ("extensions subcommand alias", ["extensions", "human-review", "19", "--approve"],
     "aliases:\n  'extension human-review': pr review\n"),
    ("nested group alias", ["repo", "autolink", "human-review", "19"],
     "aliases:\n  'repo autolink human-review': pr review 19 --approve\n"),
    ("extension exec", ["extension", "exec", "reviewer"], None),
    ("ext exec", ["ext", "exec", "reviewer"], None),
    ("extension", ["reviewer", "pr", "review", "19", "--approve"], None),
    ("--help before a shell alias", ["--help", "human-review"], SHELL_ALIAS),
    ("-h before a shell alias", ["-h", "human-review"], SHELL_ALIAS),
    ("--version before a shell alias", ["--version", "human-review"], SHELL_ALIAS),
    ("--help between group and alias", ["pr", "--help", "human-review"],
     "aliases:\n  'pr human-review': '!gh pr review 1 --approve'\n"),
    ("shell alias named gh", ["gh", "--version"], "aliases:\n  gh: '!gh pr review 1 --approve'\n"),
    ("help runs an extension", ["help", "reviewer"], None),
    ("--web starts the configured browser", ["pr", "view", "1", "--web"], "browser: gh human-review\n"),
    ("-w=true", ["repo", "view", "o/r", "-w=true"], "browser: gh human-review\n"),
    ("-w in a long cluster", ["repo", "view", "o/r", "-wwww"], "browser: gh human-review\n"),
    ("-w before an attached -R value", ["pr", "view", "1", "-wRo/r"], "browser: gh human-review\n"),
    ("-w after -- given as an option value", ["pr", "diff", "1", "--exclude", "--", "-w"], "browser: gh human-review\n"),
    ("help runs an extension named like a subcommand", ["help", "view"], None),
    ("help runs an extension named like a subcommand alias", ["help", "co"], None),
]
# what the bots and skills run with plain gh: (gh argv, expected classify answer, True for a feedback write)
BOT_READS = [
    ["--version"], ["--help"], ["-h"], [], ["version"], ["help"], ["help", "pr", "review"], ["pr"], ["pr", "--help"],
    ["pr", "view", "15", "--json", "reviews"], ["pr", "view", "--help"], ["pr", "diff", "15"], ["pr", "list"],
    ["pr", "ls"], ["pr", "checks", "15"], ["pr", "status"], ["-R", "o/r", "pr", "view", "1", "--comments"],
    ["pr", "--repo", "o/r", "diff", "1"], ["issue", "view", "9", "--comments"], ["issue", "list"],
    ["repo", "view"], ["run", "view", "1"], ["run", "list"], ["run", "watch", "1"], ["workflow", "list"],
    ["release", "list"], ["label", "list"], ["auth", "status"], ["search", "prs", "x"],
    ["api", "repos/o/r/pulls/15/comments"], ["api", "graphql", "-f", "query=query { viewer { login } }"],
    ["ext", "list"], ["extension", "ls"], ["alias", "list"], ["config", "get", "git_protocol"],
    # writes that are not PR feedback stay on the user's account, as before
    ["pr", "create", "-t", "t", "-b", "x"], ["pr", "merge", "1", "--squash", "--delete-branch"],
    ["pr", "edit", "1", "-b", "x"], ["pr", "checkout", "1"], ["pr", "co", "1"], ["pr", "ready", "1"],
    ["pr", "close", "1"], ["issue", "create", "-t", "t", "-b", "x"], ["issue", "close", "1"],
    ["repo", "clone", "o/r"], ["label", "create", "bot:skip"], ["pr", "comment", "1", "-b", "@codex review"],
]


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

    # issue #19: every bypass of the @codex exemption, each a feedback write that must go through a bot
    CODEX_BYPASSES = [
        ["pr", "review", "1", "--approve", "-b", "@codex looks good"],
        ["pr", "review", "1", "-a", "-b", "@codex looks good"],
        ["pr", "review", "1", "--request-changes", "-b", "@codex P1: broken"],
        ["pr", "review", "1", "-r", "--body=@codex P1: broken"],
        ["pr", "review", "1", "--comment", "-b", "@codex review"],
        ["pr", "review", "1", "-b", "@codex review"],
        ["pr", "review", "1", "-b@codex"],
        ["-R", "o/r", "pr", "review", "1", "--approve", "-b", "@codex"],
        ["pr", "--repo", "o/r", "review", "1", "--body", "@codex review", "--approve"],
        ["pr", "comment", "1", "-b", "@codexfoo hi"],
        ["pr", "comment", "1", "-b", "@codex: review"],
        ["pr", "comment", "1", "-b", "@codex-bot review"],
        ["issue", "comment", "1", "-b", "@codex, please look"],
        ["pr", "close", "1", "-c", "@codexfoo"],
        ["pr", "comment", "1", "-b", " @codex P1: the parser drops quotes"],
        ["pr", "comment", "1", "-b", "\n@codex review"],
        ["issue", "close", "1", "-c", "\t@codex go"],
        # a trigger body with an option that changes what is posted or where it comes from
        ["pr", "comment", "1", "-b", "@codex review", "--body-file", "notes.md"],
        ["pr", "comment", "1", "-b", "@codex review", "-F", "notes.md"],
        ["pr", "comment", "1", "-b", "@codex review", "--edit-last"],
        ["pr", "comment", "1", "-b", "@codex review", "--delete-last"],
        ["pr", "comment", "1", "-eb", "@codex review"],
        ["pr", "comment", "1", "-b", "@codex review", "--web"],
        # gh api calls never get the exemption
        ["api", "repos/o/r/pulls/1/reviews", "-f", "event=APPROVE", "-f", "body=@codex looks good"],
        ["api", "repos/o/r/issues/1/comments", "-f", "body=@codex review"],
        ["api", "graphql", "-f", "query=mutation { addComment(input: {subjectId: \"x\", body: \"@codex review\"}) "
                                    "{ clientMutationId } }"],
    ]
    CODEX_TRIGGERS = [
        ["pr", "comment", "1", "-b", "@codex review"],
        ["pr", "comment", "1", "-b", "@codex"],
        ["pr", "comment", "1", "-b", "@codex\nreview the parser"],
        ["pr", "comment", "1", "-b@codex review"],
        ["pr", "comment", "1", "--body", "@codex review", "-R", "o/r"],
        ["issue", "comment", "1", "--hostname=h", "-b", "@codex look"],
        ["pr", "close", "1", "-d", "-c", "@codex review"],
    ]

    def test_codex_exemption_covers_only_plain_trigger_comments(self):
        for args in self.CODEX_BYPASSES:
            with self.subTest(args=args):
                self.assertIs(prbot.is_feedback_write(args), True)
        for args in self.CODEX_TRIGGERS:
            with self.subTest(args=args):
                self.assertIs(prbot.is_feedback_write(args), False)
                self.assertIs(prbot.is_feedback_write(args, codex_triggers_as_bot=True), True)

    def test_issue_19_acceptance(self):
        w = prbot.is_feedback_write
        self.assertTrue(w(["pr", "review", "1", "--approve", "-b", "@codex looks good"]))
        self.assertTrue(w(["pr", "review", "1", "-b", "@codex review"]))
        self.assertTrue(w(["pr", "comment", "1", "-b", "@codexfoo hi"]))
        self.assertFalse(w(["pr", "comment", "1", "-b", "@codex review"]))
        self.assertFalse(w(["pr", "comment", "1", "-b", "@codex"]))

    def test_codex_trigger_moves_to_bot_when_enabled(self):
        self.assertTrue(prbot.is_feedback_write(["pr", "comment", "1", "-b", "@codex review"], codex_triggers_as_bot=True))

    def test_feedback_option_regressions(self):
        cases = [
            # -R/--repo/--hostname before the group and between the group and the subcommand
            (["-R", "o/r", "pr", "comment", "1", "-b", "x"], True),
            (["pr", "-R", "o/r", "comment", "1", "-b", "x"], True),
            (["issue", "--repo", "o/r", "comment", "1", "-b", "x"], True),
            (["--repo=o/r", "pr", "review", "1", "--approve"], True),
            (["pr", "--repo=o/r", "review", "1", "--approve"], True),
            (["-Ro/r", "pr", "comment", "1", "-b", "x"], True),
            (["--hostname", "github.example.com", "issue", "comment", "1", "-b", "x"], True),
            (["pr", "--hostname=github.example.com", "comment", "1", "-b", "x"], True),
            (["-R", "comment", "pr", "view", "1"], False),  # an operand that looks like a subcommand
            (["pr", "-R", "comment", "view", "1"], False),
            (["-R", "o/r", "pr", "view", "1", "--comments"], False),
            (["pr", "--repo", "o/r", "diff", "1"], False),
            (["--hostname=h", "issue", "view", "9", "--comments"], False),
            # close with a comment posts that comment
            (["pr", "close", "1", "-c", "done"], True),
            (["issue", "close", "1", "--comment", "done"], True),
            (["issue", "close", "1", "--comment=done"], True),
            (["pr", "close", "1", "-cdone"], True),
            (["pr", "close", "1", "-dc", "done"], True),
            (["-R", "o/r", "pr", "close", "1", "-c", "done"], True),
            (["issue", "--repo", "o/r", "close", "1", "--comment", "done"], True),
            (["issue", "close", "1", "-c", "--reason"], True),  # an option-looking comment is still a comment
            (["pr", "close", "1", "--comment", "-R"], True),
            (["pr", "close", "1"], False),
            (["pr", "close", "1", "--delete-branch"], False),
            (["-R", "o/r", "issue", "close", "1", "--reason", "completed"], False),
            (["issue", "close", "1", "--reason", "-c"], False),  # -c is the reason's operand here
            (["issue", "close", "1", "-r", "--comment"], False),
            (["pr", "close", "1", "-R", "-c"], False),
            # attached api fields and methods
            (["api", "repos/o/r/pulls/1/reviews", "-fbody=x"], True),
            (["api", "repos/o/r/issues/1/comments", "-Fbody=x"], True),
            (["api", "-XPOST", "repos/o/r/issues/1/comments"], True),
            (["api", "-XPATCH", "repos/o/r/issues/comments/7", "-fbody=y"], True),
            (["api", "--field=body=x", "repos/o/r/pulls/1/comments"], True),
            (["api", "-XGET", "repos/o/r/pulls/1/comments", "-fper_page=100"], False),
            (["api", "-X", "GET", "repos/o/r/pulls/1/reviews", "-f", "per_page=1"], False),
            (["api", "--method=GET", "repos/o/r/issues/1/comments", "-Fpage=2"], False),
            (["api", "repos/o/r/labels", "-fname=bug"], False),
            (["api", "-XPOST", "repos/o/r/labels"], False),
            (["api", "-H", "X-Note: /comments", "repos/o/r/labels", "-fname=bug"], False),  # header operand, not the endpoint
            (["api", "-f", "x=/comments", "repos/o/r/labels"], False),
            # create, merge and edit stay on Kenny's account by design
            (["pr", "create", "-t", "t", "-b", "x"], False),
            (["issue", "create", "-t", "t", "-b", "x"], False),
            (["pr", "merge", "1", "--squash"], False),
            (["pr", "edit", "1", "-b", "x"], False),
            (["-R", "o/r", "pr", "create", "-b", "x"], False),
            (["issue", "--repo", "o/r", "edit", "1", "--body", "x"], False),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertIs(prbot.is_feedback_write(args), want)
        codex = [
            ["-R", "o/r", "pr", "comment", "1", "-b", "@codex review"],
            ["pr", "--repo", "o/r", "comment", "1", "--body=@codex review"],
            ["issue", "--hostname", "h", "comment", "1", "-b", "@codex look"],
            ["pr", "close", "1", "-c", "@codex review"],
            ["issue", "close", "1", "--comment=@codex fix this"],
            ["-R", "o/r", "issue", "close", "1", "-c@codex go"],
        ]
        for args in codex:
            with self.subTest(args=args):
                self.assertIs(prbot.is_feedback_write(args), False)
                self.assertIs(prbot.is_feedback_write(args, codex_triggers_as_bot=False), False)
                self.assertIs(prbot.is_feedback_write(args, codex_triggers_as_bot=True), True)

    def test_gh_alias_and_extension_bypasses_are_refused(self):
        """issue #20: classify_gh refuses every alias and extension bypass, whatever the gh config says."""
        for name, args, _ in ISSUE_20_BYPASSES:
            with self.subTest(name, args=args):
                self.assertIs(prbot.is_feedback_write(args), False, "main's classifier called this a read")
                with self.assertRaises(prbot.Undecided):
                    prbot.classify_gh(args)
                with self.assertRaises(prbot.Undecided):
                    prbot.classify_gh(args, codex_triggers_as_bot=True)

    def test_safe_list_keeps_every_builtin_answer(self):
        """Calls on the safe list get is_feedback_write's answer unchanged, so bot reads and routed writes still work."""
        for args in BOT_READS:
            with self.subTest(args=args):
                self.assertIs(prbot.classify_gh(args), False)
        self.assertIs(prbot.classify_gh(["pr", "comment", "1", "-b", "@codex review"], True), True)
        for args, want in self.cases:
            with self.subTest(args=args):
                self.assertIs(prbot.classify_gh(args), want)
        for args in self.CODEX_BYPASSES:
            with self.subTest(args=args):
                if "--web" in args:  # never runs as Kenny either way: --web is refused before the write check
                    self.assertRaises(prbot.Undecided, prbot.classify_gh, args)
                else:
                    self.assertIs(prbot.classify_gh(args), True)
        for args in self.CODEX_TRIGGERS:
            with self.subTest(args=args):
                self.assertIs(prbot.classify_gh(args), False)
                self.assertIs(prbot.classify_gh(args, codex_triggers_as_bot=True), True)
        for args in (["-R", "o/r", "pr", "review", "1", "--approve"], ["pr", "--repo=o/r", "review", "1", "-a"],
                     ["issue", "close", "1", "-c", "done"], ["api", "-XPOST", "repos/o/r/issues/1/comments"]):
            with self.subTest(args=args):
                self.assertIs(prbot.classify_gh(args), True)

    # every gh call the shim may run; a new entry in prbot's safe list is added here too, with its reason in review
    SAFE_LIST = {
        "pr": {"view", "diff", "list", "ls", "checks", "status", "create", "new", "merge", "edit", "checkout", "co",
               "ready", "close", "reopen", "comment", "review", "lock", "unlock", "update-branch", "revert"},
        "issue": {"view", "list", "ls", "status", "create", "new", "close", "reopen", "comment", "edit", "lock",
                  "unlock", "pin", "unpin", "develop"},
        "repo": {"view", "list", "ls", "clone", "fork", "sync", "set-default", "read-file", "read-dir", "create",
                 "new"},
        "run": {"view", "list", "ls", "watch", "download", "rerun", "cancel"},
        "workflow": {"view", "list", "ls", "run", "enable", "disable"},
        "release": {"view", "list", "ls", "download", "verify", "verify-asset"},
        "label": {"list", "ls", "create", "edit", "clone"},
        "search": {"code", "commits", "issues", "prs", "repos"},
        "auth": {"status"}, "cache": {"list", "ls"}, "gist": {"view", "list", "ls"}, "org": {"list", "ls"},
        "ruleset": {"view", "list", "ls", "check"}, "secret": {"list", "ls"}, "variable": {"get", "list", "ls"},
        "alias": {"list", "ls"}, "extension": {"list", "ls", "search"}, "config": {"get", "list", "ls"},
        "attestation": {"verify", "download", "trusted-root"}, "discussion": {"view", "list"},
        "project": {"view", "list", "ls", "field-list", "item-list"},
    }
    LEAVES = {"api", "status", "version", "completion", "licenses"}

    def test_every_safe_list_entry_is_pinned_and_passes(self):
        """The safe list is exactly the one pinned here, and every entry (plain, after -R, and through a built-in
        group name) gets is_feedback_write's answer instead of a refusal."""
        self.assertEqual(prbot.GH_SAFE_SUBCOMMANDS, self.SAFE_LIST)
        self.assertEqual(prbot.GH_LEAF_COMMANDS, self.LEAVES)
        self.assertEqual(prbot.GH_GROUP_ALIASES, {"ext": "extension", "extensions": "extension", "rs": "ruleset",
                                                  "at": "attestation"})
        names = {g: [g] for g in self.SAFE_LIST}
        for alias, group in prbot.GH_GROUP_ALIASES.items():
            names[group].append(alias)
        for group, subs in self.SAFE_LIST.items():
            for name in names[group]:
                for sub in sorted(subs):
                    for args in ([name, sub, "1"], ["-R", "o/r", name, sub, "1"], [name, "--repo", "o/r", sub, "1"]):
                        with self.subTest(args=args):
                            self.assertIs(prbot.classify_gh(args), prbot.is_feedback_write(args))
                    with self.subTest(help=[name, sub]):
                        self.assertIs(prbot.classify_gh(["help", name, sub]), False)
                with self.subTest(group=name):
                    self.assertIs(prbot.classify_gh([name]), False)
                    self.assertIs(prbot.classify_gh(["help", name]), False)
        for leaf in self.LEAVES:
            with self.subTest(leaf=leaf):
                self.assertIs(prbot.classify_gh([leaf]), prbot.is_feedback_write([leaf]))
                self.assertIs(prbot.classify_gh(["help", leaf]), False)

    def test_safe_list_refuses_gh_setup_tokens_and_unknown_subcommands(self):
        for args in (["auth", "token"], ["auth", "status", "-t"], ["auth", "status", "--show-token"],
                     ["auth", "status", "-at"], ["auth", "login"], ["auth", "setup-git"], ["config", "set", "pager", "x"],
                     ["config", "set", "editor", "x"], ["extension", "install", "o/gh-x"], ["ext", "upgrade", "--all"],
                     ["copilot"], ["codespace", "ssh"], ["cs", "list"], ["preview", "prompter"], ["agent-task", "create"],
                     ["skill", "install", "x"], ["pr", "human-review"], ["pr", "--foo", "view", "1"], ["--foo", "pr", "view"],
                     ["help", "human-review"], ["accessibility", "human-review"], ["PR", "review", "1", "--approve"],
                     ["browse"], ["browse", "1"], ["help", "pr", "human-review"], ["help", "pr", "view", "x"],
                     ["help", "--help"], ["help", "codespace"], ["help", "browse"],
                     # -w/--web starts the browser from gh's config, like gh browse
                     ["pr", "view", "1", "--web"], ["pr", "view", "1", "-w"], ["issue", "view", "1", "--web=true"],
                     ["repo", "view", "-w"], ["run", "view", "1", "--web"], ["workflow", "view", "x", "-w"],
                     ["pr", "create", "-w"], ["pr", "comment", "1", "-w"], ["search", "prs", "x", "--web"],
                     ["pr", "close", "1", "-dw"], ["-R", "o/r", "release", "view", "v1", "--web"],
                     ["repo", "view", "o/r", "-w=true"], ["repo", "view", "o/r", "-wwww"], ["pr", "create", "-dw"],
                     ["pr", "view", "1", "-cw"], ["issue", "close", "1", "-w", "-c", "x"],
                     ["pr", "view", "1", "-w=1"], ["pr", "view", "1", "--web=false"],
                     # `--` as another option's value does not end option parsing
                     ["pr", "diff", "1", "--exclude", "--", "-w"], ["pr", "diff", "1", "-e", "--", "-w"],
                     ["pr", "view", "1", "-R", "--", "--web"], ["auth", "status", "-h", "--", "-t"]):
            with self.subTest(args=args):
                with self.assertRaises(prbot.Undecided):
                    prbot.classify_gh(args)
        self.assertIs(prbot.classify_gh(["auth", "status", "-h", "github.com"]), False)
        self.assertIs(prbot.classify_gh(["pr", "checks", "1", "--watch"]), False)
        self.assertIs(prbot.classify_gh(["pr", "view", "1", "--json", "reviews", "-q", "now"]), False)
        self.assertIs(prbot.classify_gh(["pr", "view", "1", "--json", "title", "-qnow"]), False)
        self.assertIs(prbot.classify_gh(["pr", "view", "1", "-Rowner/www"]), False)
        self.assertIs(prbot.classify_gh(["pr", "close", "1", "-c@codex review"]), False)
        self.assertIs(prbot.classify_gh(["pr", "comment", "1", "-bwow"]), True)
        self.assertIs(prbot.classify_gh(["pr", "view", "1", "-c"]), False)
        self.assertIs(prbot.classify_gh(["pr", "comment", "1", "-b", "--web please"]), True)
        self.assertIs(prbot.classify_gh(["auth", "status", "--hostname", "github.com", "--active"]), False)

    def test_classify_gh_never_reads_gh_config(self):
        """The safe list does not depend on gh's config.yml, so no config, however it is written, changes an answer."""
        with mock.patch("builtins.open", side_effect=AssertionError("opened a file")), \
                mock.patch.object(Path, "read_text", side_effect=AssertionError("read a file")):
            for args in BOT_READS:
                with self.subTest(args=args):
                    self.assertIs(prbot.classify_gh(args), False)
            for _, args, _ in ISSUE_20_BYPASSES:
                with self.subTest(args=args), self.assertRaises(prbot.Undecided):
                    prbot.classify_gh(args)

    def test_graphql_route_and_mutation_regressions(self):
        for name in ("deleteIssueComment", "deletePullRequestReview", "deletePullRequestReviewComment",
                     "dismissPullRequestReview", "minimizeComment", "unminimizeComment"):
            with self.subTest(mutation=name):
                self.assertTrue(prbot.is_feedback_write(
                    ["api", "graphql", "-f", "query=mutation { %s(input: {id: \"x\"}) { clientMutationId } }" % name]))
        add = "query=mutation { addComment(input: {subjectId: \"x\", body: \"y\"}) { clientMutationId } }"
        for endpoint in ("graphql?x=1", "graphql/?x=1", "graphql#x", "https://api.github.com/graphql?x=1"):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(prbot.is_feedback_write(["api", endpoint, "-f", add]))
        self.assertTrue(prbot.is_feedback_write(["api", "-XPOST", "repos/o/r/issues/1/comments#f", "-f", "body=x"]))
        self.assertFalse(prbot.is_feedback_write(["api", "graphql?x=1", "-f", "query=query { viewer { login } }"]))
        self.assertFalse(prbot.is_feedback_write(["api", "graphql?x=1", "-XGET", "-f", add]))

    def test_graphql_payload_file_regressions(self):
        mutation = 'mutation { addComment(input: {subjectId: "x", body: "y"}) { clientMutationId } }'
        viewer = "query { viewer { login } }"
        label = 'mutation { addLabelsToLabelable(input: {labelableId: "x", labelIds: []}) { clientMutationId } }'
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PRBOT_CONFIG_DIR": d}):
            try:
                os.chdir(d)
                Path("mutation.graphql").write_text(mutation)
                Path("viewer.graphql").write_text(viewer)
                Path("label.graphql").write_text(label)
                Path("body.json").write_text(json.dumps({"query": mutation, "variables": {}}))
                Path("escaped.json").write_text('{"query": "mutation { \\u0061ddComment(input: {}) { clientMutationId } }"}')
                Path("viewer.json").write_text(json.dumps({"query": viewer}))
                Path("label.json").write_text(json.dumps({"query": label}))
                Path("latin1.graphql").write_bytes(b"query { viewer { login } } \xff\xfe")
                Path("folder").mkdir()
                os.mkfifo("pipe")
                absolute = os.path.join(d, "mutation.graphql")

                def forms(name):
                    return [["api", "graphql", "-F", f"query=@{name}"], ["api", "graphql", "-f", f"query=@{name}"],
                            ["api", "graphql", f"-Fquery=@{name}"], ["api", "graphql", f"-fquery=@{name}"],
                            ["api", "graphql", "--field", f"query=@{name}"], ["api", "graphql", f"--field=query=@{name}"],
                            ["api", "graphql", "--raw-field", f"query=@{name}"], ["api", "graphql", f"--raw-field=query=@{name}"],
                            ["api", "-XPOST", "graphql", "-F", f"query=@{name}"]]

                def inputs(name):
                    return [["api", "graphql", "--input", name], ["api", "graphql", f"--input={name}"],
                            ["api", "--method", "POST", "graphql", "--input", name]]

                writes = forms("mutation.graphql") + forms(absolute) + inputs("body.json") + inputs("escaped.json")
                reads = forms("viewer.graphql") + forms("label.graphql") + inputs("viewer.json") + inputs("label.json")
                unreadable = (forms("missing.graphql") + forms("latin1.graphql") + forms("folder") + forms("pipe")
                              + forms("-") + forms("/dev/stdin") + inputs("missing.json") + inputs("folder")
                              + inputs("pipe") + inputs("-") + inputs("/dev/stdin"))
                gets = [["api", "graphql", "-X", "GET", "-F", "query=@mutation.graphql"],
                        ["api", "-XGET", "graphql", "-F", "query=@missing.graphql"],
                        ["api", "--method=GET", "graphql", "--input", "missing.json"],
                        ["api", "graphql", "--method", "get", "-f", "query=@-"]]
                for args in writes + unreadable:
                    with self.subTest(args=args):
                        self.assertIs(prbot.is_feedback_write(args), True)
                for args in reads + gets:
                    with self.subTest(args=args):
                        self.assertIs(prbot.is_feedback_write(args), False)
                # the example from the issue, with a relative path
                self.assertIs(prbot.is_feedback_write(["api", "graphql", "-F", "query=@mutation.graphql"]), True)
                with mock.patch("os.open", wraps=os.open) as opened:  # stdin references are never opened
                    for args in forms("-") + forms("/dev/stdin") + inputs("-") + inputs("/dev/stdin"):
                        self.assertIs(prbot.is_feedback_write(args), True)
                    self.assertEqual(opened.call_count, 0)
                with mock.patch("os.fdopen", side_effect=OSError("read failed")):
                    self.assertIs(prbot.is_feedback_write(["api", "graphql", "-F", "query=@viewer.graphql"]), True)
                    self.assertIs(prbot.is_feedback_write(["api", "graphql", "--input", "viewer.json"]), True)
                with mock.patch("os.fstat", side_effect=OSError("stat failed")):
                    self.assertIs(prbot.is_feedback_write(["api", "graphql", "--input", "viewer.json"]), True)
            finally:
                os.chdir(cwd)


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

    def test_redirection_and_substitution_regressions(self):
        once = [
            "gh >/dev/null pr comment 13 -b x",
            "gh > /dev/null pr comment 13 -b x",
            "gh 2>&1 pr comment 13 -b x",
            "gh 2>/dev/null pr comment 13 -b x",
            "gh &>/dev/null pr comment 13 -b x",
            "gh <in pr comment 13 -b x",
            "gh pr comment 13 -b x >log 2>&1",
            "gh pr comment 13 -b x >",
            "nohup >/tmp/log gh pr comment 13 -b x",
            "env >/dev/null gh pr comment 13 -b x",
            "echo hi;>x gh pr comment 13 -b x",
            "diff <(gh pr comment 13 -b x) y",
            'echo "$(gh pr comment 13 -b x)"',
            'x="$(gh pr comment 13 -b x)"',
            'echo "$(echo $(gh pr comment 13 -b x))"',
            "echo `echo \\`gh pr comment 13 -b x\\``",
            "x=`echo \\`echo \\\\\\`gh pr comment 13 -b x\\\\\\`\\``",
        ]
        for cmd in once:
            with self.subTest(cmd=cmd):
                self.assertEqual(prbot.unrouted_writes(cmd), [["pr", "comment", "13", "-b", "x"]])
        self.assertEqual(prbot.unrouted_writes("gh pr comment 13 -b 'a>b'"), [["pr", "comment", "13", "-b", "a>b"]])
        for cmd in ("gh pr view 13 > out.txt", "gh pr view 13 2>&1 | cat", "echo '$(gh pr comment 13 -b x)'",
                    'echo "\\$(gh pr comment 13 -b x)"', "echo '`gh pr comment 13 -b x`'", "cat <in >out"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.count(cmd), 0)

    def test_guarded_shell_regressions(self):
        positives = [
            "gh -R o/r pr comment 1 -b x",
            "gh pr -R o/r comment 1 -b x",
            "gh issue --repo o/r comment 1 -b x",
            "for i in 1 2; do gh pr comment $i -b x; done",
            "if true; then gh pr comment 1 -b x; fi",
            "if false; then :; elif gh pr comment 1 -b x; then :; fi",
            "if false; then :; else gh pr comment 1 -b x; fi",
            "{ gh pr comment 1 -b x; }",
            "! gh pr comment 1 -b x",
            "while gh pr comment 1 -b x; do :; done",
            'bash -c "gh pr comment 1 -b x"',
            "sh -c 'gh pr comment 1 -b x'",
            "zsh -c 'gh pr comment 1 -b x'",
            "bash -lc 'gh pr comment 1 -b x'",
            "/bin/bash -e -o pipefail -c 'gh pr comment 1 -b x' name",
            "bash -c 'for i in 1; do gh pr comment $i -b x; done'",
            "env -u FOO gh pr comment 1 -b x",
            "env -uFOO gh pr comment 1 -b x",
            "env --unset FOO gh pr comment 1 -b x",
            "env --unset=FOO gh pr comment 1 -b x",
            "env -i PATH=/usr/bin gh pr comment 1 -b x",
            "env --ignore-environment A=1 gh pr comment 1 -b x",
            "/usr/bin/env -- gh pr comment 1 -b x",
            "env -S 'gh pr comment 1' -b x",
            "time -p gh pr comment 1 -b x",
            "time gh pr comment 1 -b x",
            "command -p gh pr comment 1 -b x",
            "command gh pr comment 1 -b x",
            "nohup gh pr comment 1 -b x",
            "nohup -- gh pr comment 1 -b x",
            "sudo gh pr comment 1 -b x",
            "sudo -n -E -H gh pr comment 1 -b x",
            "sudo -nEH gh pr comment 1 -b x",
            "sudo -u kenny gh pr comment 1 -b x",
            "sudo -ukenny gh pr comment 1 -b x",
            "sudo --user kenny gh pr comment 1 -b x",
            "sudo --user=kenny gh pr comment 1 -b x",
            "sudo -g staff -h localhost -p pw: -C 3 gh pr comment 1 -b x",
            "sudo --group=staff --host=h --prompt=p --close-from=3 gh pr comment 1 -b x",
            "sudo --group staff --host h --prompt p --close-from 3 gh pr comment 1 -b x",
            "sudo -- gh pr comment 1 -b x",
            "echo 1 | xargs gh pr comment -b x",
            "echo 1 | xargs -0 -r -t -p -x gh pr comment -b x",
            "echo 1 | xargs -0rtx gh pr comment -b x",
            "echo 1 | xargs -I {} gh pr comment {} -b x",
            "echo 1 | xargs -I{} gh pr comment {} -b x",
            "echo 1 | xargs -n 1 -L 1 -P 4 -s 100 -E EOF -d , gh pr comment -b x",
            "echo 1 | xargs -n1 -L1 -P4 -s100 -EEOF -d, gh pr comment -b x",
            "echo 1 | xargs --replace gh pr comment {} -b x",
            "echo 1 | xargs --replace={} gh pr comment {} -b x",
            "echo 1 | xargs --max-args 1 --max-procs 2 --max-chars 100 --delimiter , gh pr comment -b x",
            "echo 1 | xargs --max-args=1 --max-procs=2 --max-chars=100 --delimiter=, gh pr comment -b x",
            "echo 1 | xargs --max-lines --eof gh pr comment -b x",
            "echo 1 | xargs --max-lines=1 --eof=EOF gh pr comment -b x",
            "echo 1 | xargs -- gh pr comment -b x",
            "echo 1 | xargs -J % gh pr comment % -b x",
            "echo 1 | xargs -R 1 -I % gh pr comment % -b x",
            "echo 1 | xargs -S 255 -I % gh pr comment % -b x",
            "echo 1 | xargs -J% -R1 -S255 gh pr comment % -b x",
            "env -i sudo -n nohup time -p command -p gh pr comment 1 -b x",
            "sudo -u k env FOO=1 xargs -n1 gh pr comment -b x",
            "nohup bash -c 'sudo gh pr comment 1 -b x'",
            "xargs -I{} sh -c 'gh pr comment {} -b x'",
            "`gh pr comment 1 -b x`",
            'echo "`gh pr comment 1 -b x`"',
            "x=`gh pr comment 1 -b x`",
            'gh pr comment 1 -b "see \\`ls\\`"',
            "gh api repos/o/r/pulls/1/reviews -fbody=x",
            "gh api repos/o/r/pulls/1/reviews -Fbody=x",
            "gh api -XPOST repos/o/r/issues/1/comments -f body=x",
            'gh pr close 1 -c "done"',
            'gh issue close 1 --comment "done"',
            "if true; then sudo gh -R o/r pr close 1 -c done; fi",
            "echo ok\ngh pr comment 1 -b x",
            "for i in 1 2\ndo\n  gh pr comment $i -b x\ndone",
            "if true\nthen\n  gh pr comment 1 -b x\nfi",
            "{\n  gh pr comment 1 -b x\n}",
            "gh pr comment 1 \\\n  -b x",
            "env -S gh -R o/r pr comment 1 -b x",
            "env -S 'gh -R o/r pr comment 1' -b x",
            "env -iS 'gh -R o/r pr comment 1 -b x'",
            "env --split-string='gh --repo o/r pr comment 1 -b x'",
            "env -S '-u FOO gh -R o/r pr comment 1 -b x'",
        ]
        for cmd in positives:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.count(cmd), 1)
        negatives = [
            'echo "gh pr comment 1 -b x"',
            "echo 'bash -c \"gh pr comment 1 -b x\"'",
            "echo 'sudo gh pr comment 1 -b x'",
            "echo '`gh pr comment 1 -b x`'",
            'echo "\\`gh pr comment 1 -b x\\`"',
            "gh pr view 15 --comments",
            "sudo gh pr view 1 --comments",
            "echo 1 | xargs gh pr view",
            "bash -c 'gh pr view 1 --comments'",
            "for i in 1; do gh pr view $i; done",
            "{ gh pr diff 1; }",
            "`gh pr view 1`",
            "gh pr close 1",
            "prbot gh claude -- pr comment 1 -b hi",
            "bash -c 'prbot gh claude -- pr comment 1 -b hi'",
            "sudo prbot gh claude -- pr comment 1 -b x",
            "echo 1 | xargs prbot gh claude -- pr comment -b x",
            "for i in 1; do prbot gh claude -- pr comment $i -b x; done",
            "command -v gh",
            "bash -c 'echo gh pr comment 1 -b x'",
            "env -u gh echo pr comment 1 -b x",
            "sudo -u gh echo pr comment 1 -b x",
            "xargs -I gh echo pr comment 1 -b x",
            "time -o gh echo pr comment 1 -b x",
            "gh pr create -b x -t y",
            "sudo gh pr merge 1 --squash",
        ]
        for cmd in negatives:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.count(cmd), 0)
        codex = [
            'gh pr comment 1 -b "@codex review"',
            "bash -c 'gh pr comment 1 -b \"@codex review\"'",
            "sudo gh -R o/r pr comment 1 -b '@codex review'",
            "for i in 1; do gh pr close $i -c '@codex review'; done",
            "`gh pr comment 1 -b '@codex go'`",
            "echo 1 | xargs gh pr comment -b '@codex review'",
        ]
        for cmd in codex:
            with self.subTest(cmd=cmd):
                self.assertEqual(self.count(cmd), 0)
                self.assertEqual(len(prbot.unrouted_writes(cmd, codex_triggers_as_bot=True)), 1)

        def nest(cmd, levels, shell="bash"):
            for _ in range(levels):
                cmd = f"{shell} -c {shlex.quote(cmd)}"
            return cmd
        for shell in ("bash", "sh", "zsh"):
            with self.subTest(shell=shell):
                for levels in range(1, 5):
                    self.assertEqual(self.count(nest("gh pr comment 1 -b x", levels, shell)), 1)
                self.assertEqual(self.count(nest("gh pr comment 1 -b x", 5, shell)), 0)  # recursion stops
        self.assertEqual(self.count(nest("`gh pr comment 1 -b x`", 3)), 1)
        self.assertEqual(self.count(nest("`gh pr comment 1 -b x`", 4)), 0)
        self.assertEqual(self.count(nest("prbot gh claude -- pr comment 1 -b x", 4)), 0)
        self.assertEqual(prbot.unrouted_writes("bash -c 'gh -R o/r pr comment 1 -b x'"),
                         [["-R", "o/r", "pr", "comment", "1", "-b", "x"]])
        self.assertEqual(prbot.unrouted_writes("env -S gh -R o/r pr comment 1 -b x"),
                         [["-R", "o/r", "pr", "comment", "1", "-b", "x"]])
        self.assertEqual(prbot.unrouted_writes('gh pr comment 1 \\\n  -b "two\nlines"'),
                         [["pr", "comment", "1", "-b", "two\nlines"]])  # a quoted newline stays in the body
        self.assertEqual(self.count('echo "gh pr comment 1\n-b x"'), 0)


class Guard(unittest.TestCase):
    """guard_bash: the safe list the Claude Code hook applies to Bash commands that name gh, git or prbot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mine = self.repo("kennylhilljr")
        self.theirs = self.repo("someone-else")

    def repo(self, owner):
        d = tempfile.mkdtemp(dir=self.tmp.name)
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "remote", "add", "origin", f"https://github.com/{owner}/demo.git"], check=True)
        return d

    def guard(self, command, cwd=None):
        return prbot.guard_bash(command, cwd or self.mine, {"kennylhilljr"})

    def test_safe_list_allows_reads_tests_and_routed_calls(self):
        for command in (
                "gh pr view 12 --json body,comments", "gh pr diff 12 | head -100", "gh pr list --state open",
                "gh pr checks 12", "gh issue view 4 --comments", "gh issue list -l bug", "gh -R o/r pr view 1",
                "gh pr view 1 -R o/r --json title --jq .title", "gh api repos/o/r/pulls/1",
                "gh api -XGET repos/o/r/issues/1/comments", "gh api --method GET repos/o/r/issues",
                "gh api repos/o/r/pulls --paginate --jq '.[].number'", "gh api -i repos/o/r", "gh search prs foo",
                "gh run view 1 --log", "gh auth status", "gh --version",
                "git status", "git log --oneline -5 origin/main..HEAD", "git diff origin/main...HEAD --stat",
                "git show HEAD:prbot | head", "git rev-parse HEAD", "git -C sub status", "git --no-pager log -1",
                "git add -A", "git checkout -b x", "git fetch -q origin", "git worktree add /tmp/x origin/main",
                "git branch -a", "git config --get remote.origin.url", "git stash list",
                "cat README.md | grep -n gh", "rg -n 'git push' DESIGN.md", "ls -la .git", "wc -l prbot",
                "echo 'run gh pr comment later'", "gh pr view 1 2>&1 | jq .", "git log > /dev/null 2>&1",
                "cd sub && git status && cd ..", "python3 -B -m unittest discover -s tests -t . -v",
                "scripts/check.sh && git status", "./scripts/check.sh --check tests", "GH_PAGER=cat gh pr view 1",
                "prbot gh claude -- pr comment 1 -b x", "./prbot git claude -- push origin main",
                "bash -c 'prbot git claude -- push origin main'", "command -v gh", "which git",
                "gh pr comment 1 -b '@codex review'", "gh pr close 3", "gh pr create -t x -b y",
                "gh pr merge 3 --squash --delete-branch", "gh pr create --title x --body \"$(cat <<'EOF'\nbody\nEOF\n)\"",
                "git log -1  # it's the last one", "gh pr view 1 <<< x"):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command))

    def test_commits_and_pushes_go_through_prbot_only_in_bot_repos(self):
        for command in ("git push origin main", "git commit -m x", "git add -A && git commit -m x",
                        "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\"", "git merge x", "git pull", "git rebase main",
                        "git cherry-pick abc", "git revert HEAD"):
            with self.subTest(command=command):
                self.assertTrue(self.guard(command)["git"])
                self.assertIsNone(self.guard(command, self.theirs))
        self.assertTrue(self.guard(f"git -C {shlex.quote(self.mine)} push", self.theirs)["git"])
        self.assertTrue(self.guard(f"cd {shlex.quote(self.mine)} && git push", self.theirs)["git"])
        self.assertTrue(self.guard("git push https://github.com/kennylhilljr/x.git main", self.theirs)["git"])
        for command in ("cd $D && git push", "cd .. && cd - && git push", "git push", "git commit -m x",
                        "git worktree add ../w && cd ../w && git push",
                        "git clone https://github.com/kennylhilljr/x && cd x && git push",
                        "git remote set-url origin https://github.com/kennylhilljr/x && git push origin"):
            with self.subTest(command=command):
                cwd = os.path.join(self.tmp.name, "missing") if command in ("git push", "git commit -m x") else self.theirs
                self.assertIn("cannot tell which repository", self.guard(command, cwd)["reasons"][0])

    def test_feedback_writes_are_named_for_the_routing_fix(self):
        for command, args in (
                ("gh pr comment 1 -b x", ["pr", "comment", "1", "-b", "x"]),
                ("g\\h pr comment 1 -b x", ["pr", "comment", "1", "-b", "x"]),
                ("'g'h pr comment 1 -b x", ["pr", "comment", "1", "-b", "x"]),
                ("bash -c 'gh pr comment 1 -b x'", ["pr", "comment", "1", "-b", "x"]),
                ("eval 'gh pr comment 1 -b x'", ["pr", "comment", "1", "-b", "x"]),
                ("gh api repos/o/r/issues/1/comments -fbody=x", ["api", "repos/o/r/issues/1/comments", "-fbody=x"]),
                ("gh pr comment 1 -b \"$(cat f)\"", ["pr", "comment", "1", "-b", "<run-time value>"])):
            with self.subTest(command=command):
                self.assertEqual(self.guard(command)["feedback"], [args])

    def test_bypasses_found_in_review_are_blocked(self):
        """Every reproducer from the deny-list review rounds on #4, and the obvious variants, is refused."""
        commands = [
            "# don't post yet\ngh pr comment 13 -b x  # it's fine", "# don't push yet\ngit push origin HEAD",
            "gh $(true) pr comment 13 -b x", "git $(true) push origin HEAD", "gh >$(printf /dev/null) pr comment 13 -b x",
            "env -C /repo git push", "env --chdir=/repo git push", "sudo -D /repo git push",
            "gh api -XPOST 'repos/o/r/issues/1/comments#f' -f body=x", "echo \"$( # )\ngh pr comment 13 -b x\n)\"",
            "echo start\ngh pr comment 1 -b hi", "echo hi\ngit push origin main",
            "gh api repos/a/b/issues/1/comments -Fbody=@f", "gh api -XPATCH repos/o/r/issues/comments/1",
            "for i in 1; do gh pr comment 1 -b x; done", "{ gh pr comment 1 -b x; }", "(gh pr comment 1 -b x)",
            "env -i gh pr comment 1 -b x", "nohup gh pr comment 1 -b x", "`gh pr comment 1 -b x`",
            "echo `gh pr comment 1 -b x`", "x=$(gh pr comment 1 -b x)", "time gh pr comment 1 -b x",
            "xargs gh pr comment 1 -b < /dev/null", "env -S 'gh pr comment 1 -b x'", "gh pr view 1 > out.txt",
            "gh api graphql -f query='mutation { addComment(input: {}) { clientMutationId } }'",
            "gh api graphql --input q.json", "gh api -X POST repos/o/r/issues/1/comments",
            "gh api repos/o/r/issues/1/comments -H 'X-HTTP-Method-Override: POST'", "gh label create x",
            "git -c alias.x='!gh pr comment 1 -b x' x", "git --git-dir=/repo/.git push",
            "git rebase -x 'gh pr comment 1 -b x' main", "git grep -Ogh pr", "git clone -u 'gh pr comment' u",
            "git fetch --upload-pack='gh pr comment' origin", "git diff --output=/tmp/hook.py",
            "git config alias.x '!gh'", "git x", "GH_TOKEN=t gh pr view 1", "PATH=/tmp gh pr view 1",
            "gh pr view 1 && make post", "python3 -c \"import os; os.system('gh pr comment 1 -b x')\"",
            "cat > post.sh <<'EOF'\ngh pr comment 1 -b x\nEOF", "if true; then git push origin main; fi",
            "case x in x) gh pr comment 1 -b x;; esac", "gh pr view 1 'unclosed",
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNotNone(self.guard(command))

    def test_review_round_one_bypasses_are_blocked(self):
        """Safe-list review round 1: case-insensitive names, bash -co, git long-option prefixes, globs in
        unparseable text, remote URL variants, rg --pre and pushd -n."""
        k = subprocess.run(["git", "-C", self.theirs, "remote", "add", "k", "git@github.com:KennyLHillJr/x.git"])
        self.assertEqual(k.returncode, 0)
        for command in (
                "GH pr comment 1 -b hi", "Gh pr comment 1 -b hi", "GIT push https://github.com/kennylhilljr/x HEAD",
                "bash -co pipefail 'gh pr comment 1 -b hi'", "bash -oc pipefail 'gh pr comment 1 -b hi'",
                "sh -Oc extglob 'gh pr comment 1 -b hi'", "git clone --upl 'gh pr comment 1 -b hi #' /tmp/x y",
                "git fetch --upl 'gh pr comment 1 -b hi #' /tmp/x", "git grep --open-f 'gh pr comment 1 -b x #' foo",
                "git clone --conf core.sshCommand='gh pr comment 1 -b hi #' git@github.com:a/b.git",
                "git rebase --exe 'gh pr comment 1 -b hi #' HEAD~1",
                "f(){ :; }; /opt/homebrew/bin/g? pr comment 1 -b hi",
                "case x in x) ;; esac; /opt/homebrew/bin/g[h] pr comment 1 -b hi",
                "f(){ :; }; /opt/homebrew/bin/g* pr comment 1 -b hi",
                "git push https://github.com/KennyLHillJr/foo.git HEAD", "git push https://GITHUB.COM/kennylhilljr/foo.git HEAD",
                "git push https://github.com:443/kennylhilljr/foo.git HEAD",
                "git push ssh://git@github.com:22/kennylhilljr/foo.git HEAD",
                "git push -o x https://github.com/kennylhilljr/foo.git HEAD",
                "git push --push-option x https://github.com/kennylhilljr/foo.git HEAD",
                "git push https://github.com//kennylhilljr/foo.git HEAD", "git push k", "git push -o x k main",
                "git push --repo=k", "git status; rg --pre sh . post.sh"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.guard(command, self.theirs))
        for command in ("gh auth status --show-token", "git status; bash -c 'python3 x.py'"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.guard(command, self.theirs))
        for command in ("pushd -n /tmp && git commit -m x", "pushd && git commit -m x"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.guard(command))

    def test_review_round_two_bypasses_are_blocked(self):
        """Safe-list review round 2: stdin path spellings, more shells, runners and xargs with run-time
        commands, code stored for arithmetic, PS4 or ${x@P}, make with a makefile, cd that may not run, other
        remotes and remote URL hosts, and env option clusters."""
        fork = self.repo("someone-else")
        subprocess.run(["git", "-C", fork, "remote", "add", "up", "https://github.com/kennylhilljr/demo.git"], check=True)
        other = tempfile.mkdtemp(dir=self.tmp.name)
        cases = [(c, other) for c in (
            "printf '\\147h pr comment 1 -b hi' | bash /dev/./stdin", "printf '\\147h pr comment 1 -b hi' | sh /dev//stdin",
            "printf '\\147h pr comment 1 -b hi' | source /dev/./stdin",
            "a=$(printf '\\147h'); bash /dev/fd/3 3<<EOF2\n$a pr comment 1 -b hi\nEOF2",
            "printf '\\147h pr comment 1 -b hi' | csh", "csh -c \"$(printf '\\147h pr comment 1 -b hi')\"",
            "arch -arm64 $(printf '\\147h') pr comment 1 -b hi",
            "find . -maxdepth 0 -exec $(printf '\\147h') pr comment 1 -b hi ';'",
            "printf '\\147h\\0pr\\0comment\\0--help\\0' | xargs -0 nohup",
            "x='a[$(gh pr comment 1 -b hi)]'; echo $((x))", "x='a[$(gh pr comment 1 -b hi)]'; echo $[x]",
            "PS4='$(gh pr comment 1 -b hi) '; set -x; true", "x='$(gh pr comment 1 -b hi)'; echo \"${x@P}\"",
            "make test -f /dev/stdin <<'EOF2'\ntest:\n\t@gh pr comment 1 -b hi\nEOF2",
            "git push https://github.com./kennylhilljr/x.git main", "git push git@github-kenny:kennylhilljr/x.git main",
            "git push ssh://git@140.82.112.3/kennylhilljr/x.git main", "y=g z=h env -iS '${y}${z} pr comment 1 -b hi'",
            "xargs -tI{} {} pr comment 1", "x=$'\\x24(gh)'; [[ x -eq 1 ]]", "read x <<< '$(gh pr comment 1)'",
            "python3 -c \"$CODE\"")]
        cases += [(c, self.mine) for c in (
            "false && cd /tmp; git commit -m x", "true || cd /tmp; git commit -m x", "cd /tmp & git commit -m x",
            "cd /tmp | cat; git commit -m x", "echo $(cd /tmp); git commit -m x")]
        cases += [("git push", fork), ("git pull", fork)]
        for command, cwd in cases:
            with self.subTest(command=command):
                self.assertIsNotNone(self.guard(command, cwd))
        for command in ("python3 x.py $f", "while IFS= read -r line; do echo \"$line\"; done < f", "echo $((1+2))",
                        "IFS=$'\\n'; echo x", "git commit -m \"$(cat <<'EOF'\nmsg `code`\nEOF\n)\"",
                        "x=$(date); echo $x", "find . -name '*.pyc' -delete", "gh repo clone o/r", "gh pr ready 3"):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command, other))

    def test_review_round_three_bypasses_are_blocked(self):
        """Safe-list review round 3: for-do bodies, escaped braces in ${...}, arithmetic on subscripts, logical
        cd, gh repo clone, branch remotes given as URLs, commits in repos whose only bot remote is not origin."""
        plain = os.path.join(self.tmp.name, "plain")
        os.mkdir(plain)
        os.symlink(plain, os.path.join(self.mine, "link"))
        empty = tempfile.mkdtemp(dir=self.tmp.name)
        branch = self.repo("someone-else")
        subprocess.run(["git", "-C", branch, "config", "branch.main.remote", "https://github.com/kennylhilljr/x.git"],
                       check=True)
        upstream = tempfile.mkdtemp(dir=self.tmp.name)
        subprocess.run(["git", "init", "-q", upstream], check=True)
        subprocess.run(["git", "-C", upstream, "remote", "add", "upstream", "https://github.com/kennylhilljr/x.git"],
                       check=True)
        cases = [(c, self.theirs) for c in (
            "for x do gh pr comment 1 -b hi; done", "echo ${x:-\\{} ; gh pr comment 1 -b hi ; echo }",
            "let 'a[$($(printf \"\\147h\") pr comment 1 -b hi)]'", "unset 'a[$($(printf \"\\147h\") pr comment 1 -b hi)]'",
            "[[ 'a[$($(printf \"\\147h\") pr comment 1 -b hi)]' -eq 0 ]]", "test -v 'a[$(gh pr comment 1 -b hi)]'",
            "y=$(cat f); [[ y -eq 0 ]]", "echo ${a[y]}", "rg --hostname-bin x foo; git status",
            "unset 'a[b[0]`gh pr comment 1 -b x`]'", "test -v 'a[b[0]`gh pr comment 1 -b x`]'",
            "[[ 'a[b[0]`gh pr comment 1 -b x`]' == 1 ]]")]
        cases += [("for x do git commit --allow-empty -m x; done", self.mine),
                  ("echo ${x:-\\{} ; git commit -m x ; echo }", self.mine), ("cd link/.. && git commit -m x", self.mine),
                  ("cd -P link/.. && git commit -m x", self.mine), ("set -P; cd link/..; git commit -m x", self.mine),
                  ("cd -P link; cd ..; git push", self.mine),
                  ("gh repo clone kennylhilljr/x . && git commit --allow-empty -m x", empty), ("git push", branch),
                  ("git commit -m x", upstream)]
        for command, cwd in cases:
            with self.subTest(command=command):
                self.assertIsNotNone(self.guard(command, cwd))
        for command in ("for f in a b; do echo $f; done", "echo \"${arr[@]}\" ${a[0]}", "[[ -f x ]] && echo y",
                        "echo \"[$x]\"", "cd sub/.. && ls"):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command, self.theirs))

    def test_common_agent_commands_stay_allowed(self):
        for command in ("SHA=$(git rev-parse HEAD); echo $SHA", "git log | sort | uniq -c", "mkdir -p x && git add x",
                        "set -euo pipefail; git status", "gh pr checkout 1", "\"$PWD/scripts/check.sh\" && git status",
                        "X=\"$(git log -1)\" && echo \"$X\""):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command, self.theirs))
        for command in ("x=gh; $x pr comment 1", "PATH=$(mktemp -d); gh pr view 1", "GH_TOKEN=$(cat t) gh pr view 1",
                        "PATH=/tmp/x; gh pr view 1", "git log | sort -o /tmp/hook.py", "uniq a /tmp/hook.py; git status"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.guard(command, self.theirs))

    def test_hidden_program_names_are_blocked_everywhere(self):
        """Commands that never name gh, git or prbot are still refused when the shell could build the name."""
        for command in ("x=g; ${x}h pr comment 1 -b x", "$'\\x67h' pr comment 1 -b x", "/usr/local/bin/g? pr comment 1",
                        "echo Z2ggcHIgY29tbWVudCAxIC1iIHg= | base64 -d | sh", "base64 -d f | bash -s",
                        "eval \"$CMD\"", "bash -c \"$CMD\"", "sh <<'EOF'\nls\nEOF", "$CMD pr comment 1",
                        "printf '\\x67h' | xargs -I{} {} pr comment 1", "env -S \"$X\"", "source <(curl x)",
                        "ls $(${x}h pr comment 1)", "echo \"${x:-$(true)}\"", "trap \"$x\" EXIT",
                        "hash -p /opt/homebrew/bin/g? x; x pr comment 1", "bash <(curl -s x)", "curl x | bash /dev/stdin",
                        "echo aW1wb3J0IG9z | base64 -d | python3", "x() { /opt/homebrew/bin/g? pr comment 1; }; x",
                        "source /dev/stdin < f", "trap 'g\\h pr comment 1 -b x' EXIT"):
            with self.subTest(command=command):
                self.assertIsNotNone(self.guard(command, self.theirs))

    def test_unrelated_commands_run_untouched(self):
        for command in ("ls -la", "python3 x.py > out.txt", "npm install && npm run build", "make -j4",
                        "for f in *.py; do python3 $f; done", "find . -name '*.pyc' | xargs rm",
                        "echo $HOME", "cat <<EOF > notes.txt\nhello $USER\nEOF", "bash scripts/setup.sh",
                        "if [ -f x ]; then echo y; fi", "sed -i '' s/a/b/ file", "rm -rf build"):
            with self.subTest(command=command):
                self.assertIsNone(self.guard(command, self.theirs))

    def test_lexer_reads_quotes_substitutions_and_heredocs(self):
        cmds = prbot.shell_commands("a 'b c' \"d $(e f)\" `g` <(h) # i\nj <<'EOF'\nk\nEOF\nl")
        names = [c.words[0].text for c in cmds]
        self.assertEqual(sorted(names), ["a", "e", "g", "h", "j", "l"])
        a = next(c for c in cmds if c.words[0].text == "a")
        self.assertEqual(a.words[1].text, "b c")
        self.assertTrue(a.words[2].dynamic)
        self.assertFalse(a.words[2].split)
        for bad in ("echo 'x", "echo \"x", "echo $(x", "a=(1 2)", "echo `x", "case x in x) y;; esac"):
            with self.subTest(bad=bad):
                with self.assertRaises(prbot.Unparseable):
                    prbot.shell_commands(bad)


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

    def test_git_option_and_redirection_regressions(self):
        cases = [
            ("git >/dev/null push origin HEAD", [(["push", "origin", "HEAD"], None)]),
            ("git 2>&1 commit -m x", [(["commit", "-m", "x"], None)]),
            ("git --git-dir .git push origin HEAD", [(["--git-dir", ".git", "push", "origin", "HEAD"], ".git")]),
            ("git --git-dir=other/.git push", [(["--git-dir=other/.git", "push"], "other/.git")]),
            ("git -C repo --git-dir .git push", [(["-C", "repo", "--git-dir", ".git", "push"], "repo/.git")]),
            ("git --work-tree . commit -m x", [(["--work-tree", ".", "commit", "-m", "x"], None)]),
            ("git --namespace n push", [(["--namespace", "n", "push"], None)]),
            ("git --config-env a.b=C push", [(["--config-env", "a.b=C", "push"], None)]),
            ("git log > push", []),
            ("git --git-dir push status", []),
        ]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(list(prbot.git_writes(cmd)), want)
        self.assertEqual(prbot.git_subcommand(["--work-tree", "push", "status"]), (2, "status"))

    def test_nested_git_write_regressions(self):
        commit, push = ["commit", "-m", "x"], ["-C", "repo", "push", "origin", "main"]
        cases = [
            ("for d in a; do git commit -m x; done", [(commit, None)]),
            ("if true; then git -C repo push origin main; fi", [(push, "repo")]),
            ("if false; then :; elif true; then :; else git commit -m x; fi", [(commit, None)]),
            ("{ git -C repo push origin main; }", [(push, "repo")]),
            ("! git commit -m x", [(commit, None)]),
            ("env -u FOO git commit -m x", [(commit, None)]),
            ("env -i A=1 git -C repo push origin main", [(push, "repo")]),
            ("sudo -u kenny git -C repo push origin main", [(push, "repo")]),
            ("sudo -n git commit -m x", [(commit, None)]),
            ("nohup git -C repo push origin main", [(push, "repo")]),
            ("time -p git commit -m x", [(commit, None)]),
            ("command -p git -C repo push origin main", [(push, "repo")]),
            ("echo main | xargs -I{} git -C repo push origin {}", [(["-C", "repo", "push", "origin", "{}"], "repo")]),
            ("echo x | xargs -n1 git commit -m", [(["commit", "-m"], None)]),
            ("echo main | xargs -J % git -C repo push origin %", [(["-C", "repo", "push", "origin", "%"], "repo")]),
            ("bash -c 'git -C repo push origin main'", [(push, "repo")]),
            ("sh -c 'git commit -m x'", [(commit, None)]),
            ("zsh -c 'git -C repo push origin main'", [(push, "repo")]),
            ("bash -lc 'git commit -m x && git -C repo push origin main'", [(commit, None), (push, "repo")]),
            ("sudo bash -c 'env -u X git -C repo push origin main'", [(push, "repo")]),
            ("`git commit -m x`", [(commit, None)]),
            ("git status\ngit commit -m x", [(commit, None)]),
            ("env -S 'git -C repo push origin main'", [(push, "repo")]),
            ("bash -c 'echo git push'", []),
            ("bash -c 'prbot git claude -- push'", []),
            ("sudo git status", []),
            ("echo 1 | xargs git log", []),
            ("for d in a; do prbot git claude -- commit -m x; done", []),
        ]
        for cmd, want in cases:
            with self.subTest(cmd=cmd):
                self.assertEqual(list(prbot.git_writes(cmd)), want)

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


class OpenClaw(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (prbot.OPENCLAW_EVENTS, prbot.OPENCLAW_RUNTIME)
        prbot.OPENCLAW_EVENTS = Path(self.tmp.name, "openclaw-events.jsonl")

    def tearDown(self):
        prbot.OPENCLAW_EVENTS, prbot.OPENCLAW_RUNTIME = self.saved
        self.tmp.cleanup()

    @unittest.skipUnless(Path.home().joinpath(".local/share/openclaw-ecc-orchestrator/runtime/src").is_dir(),
                         "OpenClaw runtime not installed")
    def test_event_lands_through_the_runtime_writer(self):
        ok = prbot.openclaw_attention("review findings: kennylhilljr/demo#20",
                                      {"url": "https://github.com/x", "token": "ghp_" + "a" * 36})
        self.assertTrue(ok)
        prbot.openclaw_attention("plan ready: kennylhilljr/demo#1", {"url": "https://github.com/y"})
        lines = [json.loads(l) for l in prbot.OPENCLAW_EVENTS.read_text().splitlines()]
        self.assertEqual([e["seq"] for e in lines], [1, 2])
        e = lines[0]
        self.assertEqual((e["type"], e["run_id"], e["unit_id"]), ("attention.required", "github", None))
        self.assertEqual(e["data"]["reason"], "review findings: kennylhilljr/demo#20")
        self.assertEqual(e["data"]["detail"]["token"], "[REDACTED]")   # the runtime's redaction applies

    def test_missing_or_broken_runtime_never_raises(self):
        prbot.OPENCLAW_RUNTIME = Path(self.tmp.name, "no-runtime")
        self.assertFalse(prbot.openclaw_attention("x", {}))
        broken = Path(self.tmp.name, "broken")
        (broken / "openclaw_ecc_orchestrator" / "plugin").mkdir(parents=True)
        (broken / "openclaw_ecc_orchestrator" / "__init__.py").write_text("")
        (broken / "openclaw_ecc_orchestrator" / "plugin" / "__init__.py").write_text("")
        (broken / "openclaw_ecc_orchestrator" / "plugin" / "events.py").write_text("raise RuntimeError('broken')")
        prbot.OPENCLAW_RUNTIME = broken
        cached = {m: sys.modules.pop(m) for m in list(sys.modules) if m.startswith("openclaw_ecc_orchestrator")}
        import io
        from contextlib import redirect_stderr
        try:
            with redirect_stderr(io.StringIO()):
                self.assertFalse(prbot.openclaw_attention("x", {}))
        finally:
            if str(broken) in sys.path:
                sys.path.remove(str(broken))
            for m in [m for m in sys.modules if m.startswith("openclaw_ecc_orchestrator")]:
                sys.modules.pop(m)
            sys.modules.update(cached)

    def test_count_findings(self):
        review = "### P1: crash on empty input\n...\n### P2: slow path\n### P3: naming\nCP2 is not a priority."
        self.assertEqual(prbot.count_findings(review), 2)
        self.assertEqual(prbot.count_findings("Nothing worth reporting."), 0)


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
        self.saved = {k: getattr(prbot, k) for k in ("installed_repos", "gh_read", "cmd_review", "load_config", "WATCH_STATE",
                                                       "QUEUE_DIR")}
        prbot.WATCH_STATE = Path(self.tmp.name, "watch-state.json")
        prbot.QUEUE_DIR = Path(self.tmp.name, "queue")
        Path(self.tmp.name, prbot.QUEUE_MARKER).write_text("")  # the event router's marker next to the queue
        prbot.load_config = lambda: {"claude": {"slug": "claude-pr-gh"}, "codex": {"slug": "codex-pr-gh"}}
        prbot.installed_repos = lambda bot: {"o/r"}
        prbot.gh_read = self.fake_gh
        self.reviews = {3: [["codex-pr-gh[bot]", "### Codex review\n<sub>Reviewed commit `" + "c" * 10 + "` · posted</sub>"]]}
        self.runs, self.reviews_fail = [], False  # not self.fail, which would hide TestCase.fail

        def fake_review(n, repo, mode, only):
            self.runs.append((int(n), only[0]))
            if self.reviews_fail:
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
        self.reviews_fail = True
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

    def test_full_pass_still_prunes_state_to_open_prs(self):
        prbot.WATCH_STATE.write_text(json.dumps({"o/r#99@" + "0" * 10: {"skipped": "closed long ago"}}))
        self.run_pass(limit=5)
        state = json.loads(prbot.WATCH_STATE.read_text())
        self.assertNotIn("o/r#99@" + "0" * 10, state)
        self.assertIn("o/r#4@" + "d" * 10, state)

    def test_full_pass_skips_while_another_run_holds_the_lock(self):
        import fcntl
        with open(prbot.WATCH_STATE.with_name("watch.lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            log = self.run_pass(limit=5)
        self.assertEqual(self.runs, [])
        self.assertIn("another watch run holds the lock", log)
        self.run_pass(limit=5)  # released: the next pass runs normally
        self.assertEqual(sorted(self.runs), [(2, "claude"), (5, "claude")])

    def test_failed_listing_keeps_that_repos_state(self):
        # Codex review of PR 1: a 502 on one repo's PR list dropped its baseline and retry-delay entries
        import time
        prbot.installed_repos = lambda bot: {"o/r", "o/down"}
        down = {"o/down#7@" + "7" * 10: {"baseline": True}, "o/down#8@" + "8" * 10: {"failed_claude": time.time()}}
        gone = "o/r#99@" + "9" * 10   # o/r was listed and #99 is no longer open: pruned as before
        prbot.WATCH_STATE.write_text(json.dumps({**down, gone: {"skipped": "x"}}))
        real_gh = self.fake_gh
        def gh(args):
            if args[:2] == ["pr", "list"] and args[3] == "o/down":
                prbot.die("gh pr list failed: HTTP 502")
            return real_gh(args)
        prbot.gh_read = gh
        import io
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            log = self.run_pass(limit=5)
        self.assertIn("o/down: could not list PRs", log)
        state = json.loads(prbot.WATCH_STATE.read_text())
        self.assertEqual({k: state[k] for k in down}, down)
        self.assertNotIn(gone, state)

    def test_incomplete_inventory_never_prunes_state(self):
        gone = "o/r#99@" + "9" * 10
        prbot.WATCH_STATE.write_text(json.dumps({gone: {"skipped": "x"}}))
        def lookup(bot):
            if bot == "codex":
                prbot.die("GitHub GET /app/installations failed: HTTP 502")
            return {"o/r"}
        prbot.installed_repos = lookup
        import io
        from contextlib import redirect_stderr, redirect_stdout
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = prbot.cmd_watch(limit=5)
        self.assertEqual(code, 2)
        self.assertIn("bot inventory is incomplete", out.getvalue())
        self.assertIn(gone, json.loads(prbot.WATCH_STATE.read_text()))
        self.assertEqual(sorted(self.runs), [(2, "claude"), (5, "claude")])  # the known repos are still reviewed

    def test_malformed_state_entries_are_dropped(self):
        # Codex review of PR 1: {"o/r#2@bbbbbbbbbb": null} crashed every full pass at entry.get(...)
        import time
        b, d = "o/r#2@" + "b" * 10, "o/r#4@" + "d" * 10
        good = {"o/r#5@" + "e" * 10: {"failed_claude": time.time() - 60}, "o/r#3@" + "c" * 10: {"baseline": True},
                d: {"skipped": "no agent commits"}}
        bad = {b: None, "o/r#9@" + "9" * 10: {"failed_claude": "yesterday"},
               "o/r#8@" + "8" * 10: {"failed_codex": time.time() + 3 * 86400},
               "o/r#7@" + "7" * 10: {"failed_claude": float("nan")}, "o/r#6@" + "6" * 10: {"failed_claude": True},
               "o/r#5@" + "5" * 10: {"baseline": False}, "o/r#4@" + "4" * 10: {"surprise": 1},
               "not a key": {}, "o/r#3@" + "3" * 10: {"skipped": 7}}
        prbot.WATCH_STATE.write_text(json.dumps({**good, **bad}))
        log = self.run_pass(limit=5)
        for key in bad:
            self.assertIn(f"watch state: dropping entry {key!r}", log)
        self.assertEqual(log.count("dropping entry"), len(bad))
        self.assertIn("pass done", log)
        # 2 has no usable entry, so it is reviewed; 5 is still inside its retry delay; 4 stays quietly skipped
        self.assertEqual(self.runs, [(2, "claude")])
        self.assertNotIn(d + ": skipped", log)
        state = json.loads(prbot.WATCH_STATE.read_text())
        self.assertEqual(set(bad) & set(state), {b})  # 2 is still open, so it gets a fresh entry
        self.assertEqual(state[b], {})
        self.assertEqual(prbot.valid_state_entries(state), state)

    def test_skipped_pr_is_reviewed_once_it_is_labeled(self):
        # regression (codex review of PR 1): a PR skipped as unattributed stayed skipped after an agent label
        # was added on the same commit, because the skipped marker returned before labels were read
        self.run_pass(limit=5)
        self.VIEW = {**self.VIEW, 4: dict(PR13, labels=[{"name": "agent:claude"}])}
        log = self.run_pass(limit=5)
        self.assertIn((4, "codex"), self.runs)
        self.assertNotIn("skipped", log)


class WatchQueue(unittest.TestCase):
    """watch --from-queue over the same fake repo as Watch, plus PR 6 (closed) and PR 9 (unreadable)."""
    SHA, VIEW = Watch.SHA, Watch.VIEW
    tearDown = Watch.tearDown
    CLOSED = {6}

    def setUp(self):
        Watch.setUp(self)
        self.queue = Path(self.tmp.name, "queue")
        self.queue.mkdir()
        self.fail_on, self.seq, self.listed = set(), 0, []

        def fake_review(n, repo, mode, only):
            self.runs.append((int(n), only[0]))
            if int(n) in self.fail_on:
                raise SystemExit(2)
            # a posted review shows up in the PR's reviews, as on GitHub
            self.reviews.setdefault(int(n), []).append(
                [f"{only[0]}-pr-gh[bot]", f"Reviewed commit `{self.SHA[int(n)][:10]}`"])
        prbot.cmd_review = fake_review

    def fake_gh(self, args):
        if args[:2] == ["pr", "list"]:
            self.listed.append(args[3])
        if args[:2] == ["pr", "view"] and "state" in args[-1]:
            n = int(args[2])
            if n == 9:
                prbot.die("gh pr view 9 failed: HTTP 502")
            return json.dumps({"number": n, "state": "CLOSED" if n in self.CLOSED else "OPEN",
                               "isDraft": n == 1, "headRefOid": self.SHA.get(n, "6" * 40)})
        return Watch.fake_gh(self, args)

    def event(self, number, event="opened", repo="o/r", **extra):
        self.seq += 1
        name = f"20261103T1415{self.seq:02d}123Z-1730643302_12345{self.seq}-{repo.replace('/', '__')}-pull_request-{number}-{event}.json"
        body = {"repo": repo, "kind": "pull_request", "number": number, "event": event,
                "received_at": "2026-11-03T14:15:02.123Z", "slack_message_id": "1730643302.123456",
                "slack_channel": "C0CEMPH1L0N", "source": "github-event-router", "version": 1, **extra}
        path = self.queue / name
        path.write_text(json.dumps(body))
        return path

    def run_queue(self, **kw):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = prbot.cmd_watch(queue=str(self.queue), **kw)
        return code, out.getvalue()

    def left(self):
        return sorted(p.name for p in self.queue.iterdir())

    def test_each_pr_is_handled_once_and_its_files_deleted(self):
        self.event(2, "opened")
        self.event(5, "opened")
        self.event(2, "synchronize")
        self.event(2, "comment")
        code, log = self.run_queue(limit=5)
        self.assertEqual(code, 0)
        self.assertEqual(self.runs, [(2, "claude"), (5, "claude")])  # oldest first, PR 2 once despite 3 events
        self.assertEqual(self.left(), [])
        self.assertIn("queue done: 2 PRs, 2 reviews run, 0 failed", log)

    def test_files_are_deleted_when_there_is_nothing_to_do(self):
        # 1 is a draft, 3 already has a codex review of its head commit, 4 can't be attributed, 6 is closed
        for n in (1, 3, 4, 6):
            self.event(n, "closed" if n == 6 else "unknown")
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left()), (0, [], []))
        self.assertIn("o/r#4@dddddddddd: skipped, can't tell who wrote it", log)

    def names(self, kind):
        d = prbot.queue_sibling(self.queue, kind)
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def read(self, kind, name):
        return json.loads(Path(prbot.queue_sibling(self.queue, kind), name).read_text())

    def age(self, path, text, seconds=prbot.QUEUE_SETTLE + 1):
        """Write path and backdate its mtime, as if the producer finished with it that long ago."""
        path.write_text(text)
        t = path.stat().st_mtime - seconds
        os.utime(path, (t, t))
        return path

    def test_unparsable_event_waits_while_young_then_is_rejected(self):
        # Codex review of PR 1: a file read mid-write must not be rejected before its producer finishes
        young = Path(self.queue, "20261103T141502123Z-young.json")
        young.write_text('{"repo": "o/r", "kind": "pull_re')
        before = young.read_text()
        prbot.installed_repos = lambda bot: (_ for _ in ()).throw(AssertionError("no GitHub call for a young file"))
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left(), self.names("rejected")), (0, [young.name], []))
        self.assertEqual(young.read_text(), before)
        self.assertIn("leaving 20261103T141502123Z-young.json for now: not valid JSON yet", log)
        # the producer finishes the write: the event is handled normally
        young.write_text(json.dumps({"repo": "o/r", "kind": "pull_request", "number": 2}))
        prbot.installed_repos = lambda bot: {"o/r"}
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left()), (0, [(2, "claude")], []))
        # a file still unparsable after the settle time is rejected, so it can't relaunch the job for long
        old = self.age(Path(self.queue, "20261103T141503123Z-old.json"), '{"repo": "o/r"')
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left(), self.names("rejected")), (0, [], [old.name]))
        self.assertIn(f"{old.name}: not valid JSON, written", log)

    def test_damaged_watch_state_is_moved_aside(self):
        # Codex review of PR 1: a truncated state file crashed every queue run and every hourly pass
        for damaged in ('{"o/r#2":', "[1, 2]"):
            with self.subTest(damaged=damaged):
                for p in self.tmp_files("watch-state.json.corrupt-"):
                    p.unlink()
                prbot.WATCH_STATE.write_text(damaged)
                self.event(5)
                code, log = self.run_queue(limit=5)
                self.assertEqual((code, self.left()), (0, []))
                self.assertIn("watch state was damaged; moved it to", log)
                corrupt = self.tmp_files("watch-state.json.corrupt-")
                self.assertEqual(len(corrupt), 1)
                self.assertRegex(corrupt[0].name, r"^watch-state\.json\.corrupt-\d{8}T\d{6}Z$")
                self.assertEqual(corrupt[0].read_text(), damaged)
                self.assertIsInstance(json.loads(prbot.WATCH_STATE.read_text()), dict)
        prbot.WATCH_STATE.write_text('{"o/r#2":')  # the hourly pass recovers the same way
        log = Watch.run_pass(self, limit=5)
        self.assertIn("watch state was damaged", log)
        self.assertIn("pass done", log)

    def tmp_files(self, prefix):
        return sorted(p for p in Path(self.tmp.name).iterdir() if p.name.startswith(prefix))

    def test_state_and_config_writes_never_leave_a_partial_file(self):
        prbot.WATCH_STATE.write_text(json.dumps({"keep": {}}))
        real_replace = os.replace
        def crash(src, dst):
            raise OSError("disk full")
        os.replace = crash
        try:
            with self.assertRaises(OSError):
                prbot.save_watch_state({"new": {"x": 1}})
        finally:
            os.replace = real_replace
        self.assertEqual(json.loads(prbot.WATCH_STATE.read_text()), {"keep": {}})
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir() if p.name.endswith(".tmp")], [])
        saved = (prbot.CONFIG, prbot.CONFIG_DIR)
        prbot.CONFIG_DIR = Path(self.tmp.name, "cfg")
        prbot.CONFIG = prbot.CONFIG_DIR / "config.json"
        try:
            prbot.save_config({"claude": {"slug": "claude-pr-gh"}})
            self.assertEqual(json.loads(prbot.CONFIG.read_text()), {"claude": {"slug": "claude-pr-gh"}})
            self.assertEqual(sorted(p.name for p in prbot.CONFIG_DIR.iterdir()), ["config.json"])
        finally:
            prbot.CONFIG, prbot.CONFIG_DIR = saved

    def test_side_directories_sit_next_to_the_queue_not_inside_it(self):
        for kind in ("deferred", "rejected"):
            d = prbot.queue_sibling(self.queue, kind)
            self.assertEqual(d.parent, self.queue.parent)
            self.assertEqual(d.name, f"queue-{kind}")
        self.assertEqual(prbot.queue_sibling(Path("/x/github-events/prbot"), "rejected"),
                         Path("/x/github-events/prbot-rejected"))

    def test_relabeled_pr_on_the_same_commit_gets_reviewed(self):
        # regression (codex review of PR 1): PR 4 was skipped as unattributed; a later agent:codex label on the
        # same head commit produced a queue event that was deleted as handled without reading the labels
        self.event(4, "opened")
        self.run_queue(limit=5)
        self.assertIn("skipped", json.loads(prbot.WATCH_STATE.read_text())["o/r#4@" + "d" * 10])
        self.VIEW = {**self.VIEW, 4: dict(PR13, labels=[{"name": "agent:codex"}])}
        self.event(4, "labeled")
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left()), (0, [(4, "claude")], []))
        self.assertNotIn("skipped", json.loads(prbot.WATCH_STATE.read_text())["o/r#4@" + "d" * 10])

    def test_config_problem_goes_to_stderr_and_moves_nothing(self):
        """A broken config makes a queue run print the one-line config message on stderr, log it, and exit 2
        before it moves anything: event, malformed and unsupported files all stay in the queue."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        def broken():
            raise prbot.ConfigError(f"{prbot.CONFIG}: not valid JSON; fix or remove it")
        prbot.load_config = broken
        Path(self.queue, "junk.json").write_text("{bad")
        os.utime(Path(self.queue, "junk.json"), (0, 0))  # settled long ago, so a healthy run would reject it
        Path(self.queue, "subdir").mkdir()
        names = sorted([self.event(2).name, self.event(5).name, "junk.json", "subdir"])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prbot.cmd_watch(queue=str(self.queue), limit=5)
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(), f"prbot: {prbot.CONFIG}: not valid JSON; fix or remove it\n")
        self.assertIn("queue: config problem, leaving every file in the queue", out.getvalue())
        self.assertNotIn("ConfigError(", out.getvalue())
        self.assertEqual((self.runs, self.left(), self.names("deferred"), self.names("rejected")),
                         ([], names, [], []))

    def test_empty_queue_with_a_broken_config_exits_2(self):
        """An empty queue still reports a broken config: exit 2 with the one line, not a silent 0."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        def broken():
            raise prbot.ConfigError(f"{prbot.CONFIG}: not valid JSON; fix or remove it")
        prbot.load_config = broken
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(prbot.main(["watch", "--from-queue", str(self.queue)]), 2)
        self.assertEqual(err.getvalue(), f"prbot: {prbot.CONFIG}: not valid JSON; fix or remove it\n")

    def test_config_problem_is_reported_even_when_another_run_holds_the_lock(self):
        """A queue run that finds the lock taken still reports a broken config: exit 2 with the line, not 0."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        def broken():
            raise prbot.ConfigError(f"{prbot.CONFIG}: not valid JSON; fix or remove it")
        prbot.load_config = broken
        self.addCleanup(setattr, prbot, "watch_lock", prbot.watch_lock)
        prbot.watch_lock = lambda: None
        name = self.event(2).name
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(prbot.main(["watch", "--from-queue", str(self.queue)]), 2)
        self.assertEqual(err.getvalue(), f"prbot: {prbot.CONFIG}: not valid JSON; fix or remove it\n")
        self.assertEqual((self.left(), self.names("deferred")), ([name], []))

    def test_config_breaking_mid_run_is_reported_and_retried(self):
        """If the config breaks after the check at the start, the line goes to stderr and the events are deferred
        for a retry, as for any lookup failure: nothing is rejected or reviewed."""
        import io
        from contextlib import redirect_stderr, redirect_stdout
        good, calls = prbot.load_config, []

        def flaky():
            calls.append(1)
            if len(calls) > 2:
                raise prbot.ConfigError(f"{prbot.CONFIG}: not valid JSON; fix or remove it")
            return good()
        prbot.load_config = flaky
        names = sorted([self.event(2).name, self.event(5).name])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prbot.cmd_watch(queue=str(self.queue), limit=5)
        self.assertEqual(code, 2)
        self.assertGreater(len(calls), 2)
        self.assertEqual(err.getvalue(), f"prbot: {prbot.CONFIG}: not valid JSON; fix or remove it\n")
        self.assertIn("queue: config problem during the run, deferring the event files", out.getvalue())
        self.assertEqual((self.runs, self.left(), self.names("deferred"), self.names("rejected")),
                         ([], [], names, []))

    def run_queue_capturing(self, **kw):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = prbot.cmd_watch(queue=str(self.queue), **kw)
        return code, out.getvalue(), err.getvalue()

    def test_config_problem_in_the_installation_lookup_is_printed_once_and_retried(self):
        """A config error raised inside the installation lookup prints the config line once on stderr; the events
        are deferred for a retry as for any lookup failure."""
        line = f"{prbot.CONFIG}: not valid JSON; fix or remove it"

        def broken(bot):
            raise prbot.ConfigError(line)
        prbot.installed_repos = broken
        names = sorted([self.event(2).name, self.event(5).name])
        code, _, err = self.run_queue_capturing(limit=5)
        self.assertEqual(code, 2)
        self.assertEqual(err, f"prbot: {line}\n")
        self.assertEqual((self.runs, self.left(), self.names("deferred")), ([], [], names))

    def test_config_problem_during_a_review_is_printed_once_and_retried(self):
        """A config error raised by a review prints the config line once on stderr, however many PRs hit it."""
        line = f"{prbot.CONFIG}: not valid JSON; fix or remove it"

        def broken(n, repo, mode, only):
            self.runs.append((int(n), only[0]))
            raise prbot.ConfigError(line)
        prbot.cmd_review = broken
        names = sorted([self.event(2).name, self.event(5).name])
        code, _, err = self.run_queue_capturing(limit=5)
        self.assertEqual(code, 2)
        self.assertEqual(err, f"prbot: {line}\n")
        self.assertEqual((self.left(), self.names("deferred")), ([], names))

    def test_full_pass_reports_a_broken_config_even_when_the_lock_is_held(self):
        """A plain watch run that finds the lock taken still reports a broken config: exit 2 with the line."""
        import io
        from contextlib import redirect_stderr, redirect_stdout

        def broken():
            raise prbot.ConfigError(f"{prbot.CONFIG}: not valid JSON; fix or remove it")
        prbot.load_config = broken
        self.addCleanup(setattr, prbot, "watch_lock", prbot.watch_lock)
        prbot.watch_lock = lambda: None
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            self.assertEqual(prbot.main(["watch"]), 2)
        self.assertEqual(err.getvalue(), f"prbot: {prbot.CONFIG}: not valid JSON; fix or remove it\n")

    def test_failed_pr_is_deferred_and_the_rest_go_on(self):
        prbot.WATCH_STATE.write_text(json.dumps({"other/repo#1@" + "1" * 10: {"skipped": "x"}}))
        self.fail_on = {2}
        moved = [self.event(2).name, self.event(9).name]
        self.event(5)
        code, log = self.run_queue(limit=5)
        self.assertEqual(code, 2)
        self.assertEqual(self.runs, [(2, "claude"), (5, "claude")])
        self.assertEqual((self.left(), self.names("deferred")), ([], sorted(moved)))
        self.assertIn("o/r#9: could not handle the PR", log)
        self.assertEqual(self.read("deferred", moved[0])["attempts"], 1)
        self.assertEqual(self.read("deferred", moved[0])["slack_message_id"], "1730643302.123456")
        state = json.loads(prbot.WATCH_STATE.read_text())
        self.assertIn("failed_claude", state["o/r#2@" + "b" * 10])
        self.assertIn("other/repo#1@" + "1" * 10, state)  # a queue run keeps state for PRs it did not visit
        # back in the queue, PR 2 is inside its retry delay: no model run, deferred again, and not a new failure
        Path(prbot.queue_sibling(self.queue, "deferred"), moved[1]).unlink()
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            prbot.requeue_deferred(self.queue)
        self.assertEqual((self.left(), self.names("deferred")), ([moved[0]], []))
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, len(self.runs), self.left()), (0, 2, []))
        self.assertEqual(self.read("deferred", moved[0])["attempts"], 2)

    def test_review_over_the_limit_stays_in_the_queue_untouched(self):
        self.event(2)
        wait = self.event(5)
        before = wait.read_text()
        code, log = self.run_queue(limit=1)
        self.assertEqual((code, self.runs, self.left(), self.names("deferred")), (0, [(2, "claude")], [wait.name], []))
        self.assertEqual(wait.read_text(), before)
        self.assertNotIn("attempts", json.loads(wait.read_text()))
        self.assertIn("o/r#5: over this run's limit; leaving 1 file in the queue", log)

    def test_limit_bound_runs_always_make_progress(self):
        # PR 2 fails and PR 5 succeeds; each relaunch must take at least one PR out of the queue, even at limit 0
        self.fail_on = {2}
        self.event(2)
        self.event(5)
        code, _ = self.run_queue(limit=0)
        self.assertEqual((code, self.runs, len(self.left()), len(self.names("deferred"))), (2, [(2, "claude")], 1, 1))
        code, _ = self.run_queue(limit=0)
        self.assertEqual((code, self.runs, self.left()), (0, [(2, "claude"), (5, "claude")], []))

    def test_repository_lookup_failure_defers_everything(self):
        names = [self.event(2).name, self.event(2, repo="someone/else").name]
        self.age(Path(self.queue, "bad.json"), "{oops")
        def lookup_fails(bot):
            prbot.die("GitHub GET /app/installations failed: HTTP 502")
        prbot.installed_repos = lookup_fails
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left()), (2, [], []))
        self.assertEqual(self.names("deferred"), sorted(names))
        self.assertEqual(self.names("rejected"), ["bad.json"])  # malformed needs no lookup to reject
        self.assertEqual(self.read("deferred", names[1])["attempts"], 1)
        self.assertIn("could not read where the claude bot is installed", log)
        self.assertIn("someone/else is not in the bot inventory, which is incomplete", log)

    def test_event_is_rejected_after_too_many_attempts(self):
        self.fail_on = {2}
        name = self.event(2, attempts=prbot.MAX_ATTEMPTS - 1).name
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left(), self.names("deferred"), self.names("rejected")), (2, [], [], [name]))
        self.assertEqual(self.read("rejected", name)["attempts"], prbot.MAX_ATTEMPTS)
        self.assertIn(f"giving up after {prbot.MAX_ATTEMPTS} attempts", log)

    def test_malformed_events_are_rejected(self):
        bad = {"not-json.json": "{oops", "list.json": "[1, 2]", "issue.json": json.dumps({"repo": "o/r", "kind": "issue", "number": 2}),
               "no-number.json": json.dumps({"repo": "o/r", "kind": "pull_request"}),
               "string-number.json": json.dumps({"repo": "o/r", "kind": "pull_request", "number": "2"}),
               "bool-number.json": json.dumps({"repo": "o/r", "kind": "pull_request", "number": True}),
               "bad-repo.json": json.dumps({"repo": "o", "kind": "pull_request", "number": 2})}
        for name, text in bad.items():
            self.age(Path(self.queue, name), text)
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left(), self.names("rejected")), (0, [], [], sorted(bad)))
        self.assertEqual(self.read("rejected", "list.json"), [1, 2])  # kept unchanged as evidence
        self.assertIn("not-json.json: not valid JSON, written", log)
        for name in bad:
            if name != "not-json.json":
                self.assertIn(f"{name}: not a pull_request event", log)

    def test_events_for_repos_the_bots_do_not_cover_are_rejected(self):
        name = self.event(2, repo="someone/else").name
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left(), self.names("rejected")), (0, [], [], [name]))
        self.assertIn("someone/else is not a repository the bots are installed on", log)

    def test_unsupported_entries_leave_the_queue_without_calling_github(self):
        # regression (codex review of PR 1): launchd's QueueDirectories counts any entry, so an ignored
        # .DS_Store or directory left behind would restart the job forever
        for name in (".DS_Store", ".partial.json", "notes.txt", "event.json.tmp"):
            Path(self.queue, name).write_text("{}")
        target = Path(self.tmp.name, "elsewhere.json")
        target.write_text('{"keep": "me"}')
        Path(self.queue, "link.json").symlink_to(target)
        Path(prbot.queue_sibling(self.queue, "rejected")).mkdir()
        Path(prbot.queue_sibling(self.queue, "rejected"), "notes.txt").write_text("older")
        def no_github(bot):
            raise AssertionError("a queue with no event files must not call GitHub")
        prbot.installed_repos = no_github
        code, log = self.run_queue()
        self.assertEqual((code, self.left()), (0, []))
        self.assertEqual(self.names("rejected"), [".DS_Store", ".partial.json", "event.json.tmp", "link.json",
                                                  "notes.txt", "notes.txt-1"])
        self.assertIn("notes.txt: not an event file", log)
        # the link itself moved; what it points at did not
        moved = Path(prbot.queue_sibling(self.queue, "rejected"), "link.json")
        self.assertTrue(moved.is_symlink())
        self.assertEqual(os.readlink(moved), str(target))
        self.assertEqual(target.read_text(), '{"keep": "me"}')

    def test_subdirectory_in_a_checked_queue_moves_to_rejected_intact(self):
        # left in place it would keep launchd relaunching the job every ThrottleInterval
        sub = Path(self.queue, "sub.json")
        (sub / "deeper").mkdir(parents=True)
        Path(sub, "deeper", "inner.txt").write_text("x")
        self.event(2)
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left(), self.names("rejected"), self.runs), (0, [], ["sub.json"], [(2, "claude")]))
        self.assertIn("sub.json: a directory", log)
        self.assertEqual(Path(prbot.queue_sibling(self.queue, "rejected"), "sub.json", "deeper", "inner.txt").read_text(), "x")

    def run_refused(self, qdir):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = prbot.cmd_watch(queue=str(qdir), limit=5)
        return code, err.getvalue()

    def test_queue_directory_must_be_the_default_or_next_to_the_marker(self):
        # Codex review of PR 1: `prbot watch --from-queue .` in a checkout moved .git and the sources out
        plain = Path(self.tmp.name, "plain")
        checkout = plain / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / "README.md").write_text("hello")
        (checkout / "event.json").write_text(json.dumps({"repo": "o/r", "kind": "pull_request", "number": 2}))
        code, err = self.run_refused(checkout)
        self.assertEqual(code, 2)
        self.assertIn("refusing --from-queue", err)
        self.assertIn(".github-events-queue", err)
        self.assertEqual(sorted(p.name for p in checkout.iterdir()), [".git", "README.md", "event.json"])
        self.assertEqual(sorted(p.name for p in plain.iterdir()), ["checkout"])  # no -rejected, no -deferred
        # a marker that is not a regular file does not count
        (plain / prbot.QUEUE_MARKER).mkdir()
        self.assertEqual(self.run_refused(checkout)[0], 2)
        # the default queue is accepted even without a marker
        saved = prbot.DEFAULT_QUEUE_DIR
        prbot.DEFAULT_QUEUE_DIR = checkout
        try:
            code, log = self.run_queue_at(checkout)
        finally:
            prbot.DEFAULT_QUEUE_DIR = saved
        self.assertEqual((code, self.runs), (0, [(2, "claude")]))
        # the marker parent is accepted (every other test in this class uses it)
        self.event(5)
        self.assertEqual((self.run_queue(limit=5)[0], self.left()), (0, []))

    def run_queue_at(self, qdir):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = prbot.cmd_watch(queue=str(qdir), limit=5)
        return code, out.getvalue()

    def test_symlinked_or_non_directory_queue_is_refused(self):
        self.event(2)
        link = Path(self.tmp.name, "queue-link")
        link.symlink_to(self.queue)
        code, err = self.run_refused(link)
        self.assertEqual(code, 2)
        self.assertIn("is a symlink", err)
        afile = Path(self.tmp.name, "afile")
        afile.write_text("")
        code, err = self.run_refused(afile)
        self.assertEqual(code, 2)
        self.assertIn("is not a directory", err)
        self.assertEqual((len(self.left()), self.runs), (1, []))

    def test_hourly_pass_refuses_an_unsafe_queue_but_still_reviews(self):
        plain = Path(self.tmp.name, "plain", "prbot")
        deferred = prbot.queue_sibling(plain, "deferred")
        deferred.mkdir(parents=True)
        (deferred / "e.json").write_text("{}")
        prbot.QUEUE_DIR = plain
        import io
        from contextlib import redirect_stderr, redirect_stdout
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = prbot.cmd_watch(limit=5)
        self.assertEqual(code, 2)
        self.assertIn("not moving deferred events back", err.getvalue())
        self.assertEqual(sorted(self.runs), [(2, "claude"), (5, "claude")])
        self.assertEqual([p.name for p in deferred.iterdir()], ["e.json"])
        self.assertFalse(plain.exists())

    def test_pr_needing_an_unavailable_bot_is_deferred_not_dropped(self):
        # Codex review of PR 1: with only the codex bot on the repo, a codex-written PR's event was deleted unreviewed
        prbot.installed_repos = lambda bot: {"o/r"} if bot == "codex" else {"o/elsewhere"}
        name = self.event(2).name
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left(), self.names("deferred")), (0, [], [], [name]))
        self.assertIn("needs a claude review, but the claude bot is not installed on o/r", log)
        # the claude bot's inventory is empty: same outcome
        prbot.installed_repos = lambda bot: {"o/r"} if bot == "codex" else set()
        Path(prbot.queue_sibling(self.queue, "deferred"), name).rename(Path(self.queue, name))
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left(), self.names("deferred")), (0, [], [], [name]))
        self.assertIn("the claude bot is not registered or its installations are unknown", log)

    def test_incomplete_inventory_defers_instead_of_rejecting(self):
        # Codex review of PR 1: a missing config or a missing bot inventory made every repo look unknown
        saved = prbot.load_config
        try:
            for case, setup in (("no config", lambda: setattr(prbot, "load_config", lambda: {})),
                                ("one bot unregistered",
                                 lambda: setattr(prbot, "load_config", lambda: {"codex": {"slug": "codex-pr-gh"}})),
                                ("one lookup fails", lambda: setattr(prbot, "installed_repos", self.claude_lookup_fails)),
                                ("one inventory empty", lambda: setattr(prbot, "installed_repos",
                                                                         lambda b: set() if b == "claude" else {"o/r"}))):
                with self.subTest(case=case):
                    prbot.load_config, prbot.installed_repos = saved, (lambda bot: {"o/r"})
                    for d in ("deferred", "rejected"):
                        for p in Path(prbot.queue_sibling(self.queue, d)).glob("*") if prbot.queue_sibling(self.queue, d).is_dir() else []:
                            p.unlink()
                    setup()
                    other = self.event(2, repo="o/maybe").name
                    sweep = self.sweep("o/maybe", name="sweep-20261103T150000000Z-aaaaaa.json")
                    code, log = self.run_queue(limit=5)
                    self.assertEqual(code, 2)
                    self.assertEqual((self.left(), self.names("rejected")), ([], []))
                    self.assertEqual(self.names("deferred"), sorted([other, sweep]))
                    self.assertIn("bot inventory is incomplete", log)
        finally:
            prbot.load_config = saved
        # with every inventory read, the same repo is rejected as unknown
        prbot.installed_repos = lambda bot: {"o/r"}
        name = self.event(2, repo="o/maybe").name
        self.run_queue(limit=5)
        self.assertEqual(self.names("rejected"), [name])

    @staticmethod
    def claude_lookup_fails(bot):
        if bot == "claude":
            prbot.die("GitHub GET /app/installations failed: HTTP 502")
        return {"o/r"}

    def test_null_sweep_with_incomplete_inventory_is_deferred_after_its_pass(self):
        prbot.installed_repos = self.claude_lookup_fails
        name = self.sweep(None, name="sweep-20261103T150000000Z-bbbbbb.json")
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left(), self.names("deferred")), (2, [], [name]))
        self.assertIn("could not reach repositories missing from the incomplete bot inventory", log)

    def test_sweep_kept_when_any_pr_hit_the_limit(self):
        # Codex review of PR 1: PR 2 needs an unavailable bot ("later"), PR 3 and PR 5 need reviews and the limit
        # is 1. The worst-result ranking said "later" and the sweep was deleted with PR 5 never reviewed
        self.VIEW = {**self.VIEW, 2: PR18, 3: PR15}   # 2 needs codex, 3 and 5 need claude
        self.reviews = {}
        prbot.installed_repos = lambda bot: {"o/r"} if bot == "claude" else {"o/elsewhere"}
        name = self.sweep("o/r", name="sweep-20261103T150000000Z-cccccc.json")
        code, log = self.run_queue(limit=1)
        self.assertIn("needs a codex review, but the codex bot is not installed on o/r", log)
        self.assertEqual(self.runs, [(3, "claude")])
        self.assertEqual((code, self.left()), (0, [name]))
        code, log = self.run_queue(limit=1)
        self.assertEqual((code, self.runs, self.left()), (0, [(3, "claude"), (5, "claude")], []))

    def test_sweep_with_an_unlistable_repo_is_deferred_and_keeps_its_state(self):
        prbot.installed_repos = lambda bot: {"o/r", "o/down"}
        kept = {"o/down#7@" + "7" * 10: {"baseline": True}}
        prbot.WATCH_STATE.write_text(json.dumps(kept))
        real_gh = prbot.gh_read
        def gh(args):
            if args[:2] == ["pr", "list"] and args[3] == "o/down":
                prbot.die("gh pr list failed: HTTP 502")
            return real_gh(args)
        prbot.gh_read = gh
        name = self.sweep(None, name="sweep-20261103T150000000Z-dddddd.json")
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left(), self.names("deferred")), (2, [], [name]))
        self.assertEqual(json.loads(prbot.WATCH_STATE.read_text())["o/down#7@" + "7" * 10], {"baseline": True})

    def test_unique_sweeps_are_claimed_and_a_new_one_survives(self):
        # the router names sweeps sweep-<UTC>-<hex>.json; a run deletes only the ones it listed at the start
        first = self.sweep("o/r", name="sweep-20261103T150000000Z-3fa9c1.json")
        second = self.sweep(None, name="sweep-20261103T150001000Z-0b12de.json")
        late = "sweep-20261103T150002000Z-77aa01.json"
        real_gh = prbot.gh_read
        def gh_and_new_sweep(args):
            if args[:2] == ["pr", "list"] and not Path(self.queue, late).exists():
                self.sweep(None, name=late)  # the router publishes a new sweep while the pass runs
            return real_gh(args)
        prbot.gh_read = gh_and_new_sweep
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left()), (0, [late]))
        self.assertIn("queue: delete " + first, log)
        self.assertIn("queue: delete " + second, log)
        prbot.gh_read = real_gh
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left()), (0, []))

    def test_deferral_moves_the_event_before_rewriting_it(self):
        # Codex review of PR 1: writing the copy first and unlinking after could leave the event in two places
        import io
        from contextlib import redirect_stdout
        src = self.event(2)
        original = src.read_text()
        dest = prbot.queue_sibling(self.queue, "deferred")
        real = prbot.write_json_atomic
        def crash(*a, **kw):
            raise OSError("disk full")
        prbot.write_json_atomic = crash
        try:
            with redirect_stdout(io.StringIO()) as out:
                target = prbot.move_into(src, dest, {"attempts": 1})
        finally:
            prbot.write_json_atomic = real
        self.assertFalse(src.exists())
        self.assertEqual((self.left(), self.names("deferred")), ([], [src.name]))
        self.assertEqual(target.read_text(), original)  # intact, and in exactly one place
        self.assertIn("could not update it", out.getvalue())
        moved = prbot.move_into(target, self.queue, {"attempts": 2})  # the normal path rewrites after the move
        self.assertEqual((json.loads(moved.read_text()), self.names("deferred")), ({"attempts": 2}, []))

    def test_unparsable_file_with_a_future_mtime(self):
        # Codex review of PR 1: a negative age counted as young forever. Within CLOCK_SKEW it still waits;
        # further ahead it counts as settled and is rejected
        def future(name, seconds):
            p = Path(self.queue, name)
            p.write_text("{oops")
            t = p.stat().st_mtime + seconds
            os.utime(p, (t, t))
        future("near.json", 120)
        future("far.json", prbot.CLOCK_SKEW + 300)
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left(), self.names("rejected")), (0, ["near.json"], ["far.json"]))
        self.assertIn("leaving near.json for now", log)

    def test_side_directories_must_be_real_directories(self):
        keep = self.event(2).name
        elsewhere = Path(self.tmp.name, "elsewhere")
        elsewhere.mkdir()
        deferred, rejected = prbot.queue_sibling(self.queue, "deferred"), prbot.queue_sibling(self.queue, "rejected")
        deferred.symlink_to(elsewhere)
        code, err = self.run_refused(self.queue)
        self.assertEqual(code, 2)
        self.assertIn("queue-deferred exists but is not a directory", err)
        # the hourly pass refuses the requeue the same way
        import io
        from contextlib import redirect_stderr, redirect_stdout
        e2 = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(e2):
            self.assertEqual(prbot.cmd_watch(dry_run=False, limit=0), 2)
        self.assertIn("not moving deferred events back", e2.getvalue())
        deferred.unlink()
        rejected.write_text("a file")
        code, err = self.run_refused(self.queue)
        self.assertEqual(code, 2)
        self.assertIn("queue-rejected exists but is not a directory", err)
        self.assertEqual((self.left(), list(elsewhere.iterdir()), rejected.read_text()), ([keep], [], "a file"))

    def sweep(self, repo, name="sweep.json"):
        body = {"kind": "sweep", "repo": repo, "received_at": "2026-11-03T14:15:02.123Z",
                "slack_message_id": "1730643302.123456", "slack_channel": "C0CEMPH1L0N",
                "source": "github-event-router", "version": 1}
        Path(self.queue, name).write_text(json.dumps(body))
        return name

    def test_sweep_of_one_repo_runs_the_pass_there_and_is_deleted(self):
        prbot.installed_repos = lambda bot: {"o/r", "o/other"}
        self.sweep("o/r")
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.listed, self.runs, self.left()), (0, ["o/r"], [(2, "claude"), (5, "claude")], []))
        self.assertIn("queue: sweep of o/r", log)

    def test_sweep_with_null_repo_covers_every_repo(self):
        prbot.installed_repos = lambda bot: {"o/r", "o/other"}
        self.sweep(None)
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.listed, self.left()), (0, ["o/other", "o/r"], []))
        self.assertIn("sweep of every covered repository", log)

    def test_failed_sweep_is_deferred(self):
        self.fail_on = {2}
        name = self.sweep(None)
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.left(), self.names("deferred")), (2, [], [name]))
        self.assertEqual(self.read("deferred", name)["attempts"], 1)
        self.assertEqual(self.read("deferred", name)["kind"], "sweep")

    def test_sweep_of_an_unknown_repo_is_rejected(self):
        name = self.sweep("someone/else")
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.listed, self.left(), self.names("rejected")), (0, [], [], [name]))
        bad = self.sweep(5)  # repo must be a name or null
        self.run_queue(limit=5)
        self.assertEqual(self.names("rejected"), sorted([name, f"{Path(bad).stem}-1.json"]))

    def test_limit_bound_sweep_makes_progress_each_run(self):
        self.sweep(None)
        code, log = self.run_queue(limit=1)
        self.assertEqual((code, self.runs, self.left()), (0, [(2, "claude")], ["sweep.json"]))
        self.assertIn("sweep is over this run's limit", log)
        code, log = self.run_queue(limit=1)
        self.assertEqual((code, self.runs, self.left()), (0, [(2, "claude"), (5, "claude")], []))

    def test_pr_events_run_before_the_sweep_and_are_not_reviewed_twice(self):
        self.sweep(None)
        self.event(5)
        code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left()), (0, [(5, "claude"), (2, "claude")], []))
        self.assertLess(log.index("running a claude review"), log.index("queue: sweep of"))

    def test_empty_and_missing_queues_exit_quietly(self):
        self.assertEqual(self.run_queue(), (0, ""))
        self.queue.rmdir()
        self.assertEqual(self.run_queue(), (0, ""))
        self.assertEqual(prbot.main(["watch", "--from-queue", str(self.queue)]), 0)

    def test_dry_run_moves_nothing_and_saves_no_state(self):
        Path(self.queue, ".DS_Store").write_text("")
        keep = [self.event(2).name, self.event(5).name, self.event(2, repo="someone/else").name, ".DS_Store"]
        code, log = self.run_queue(dry_run=True, limit=1)
        self.assertEqual((code, self.runs, self.left()), (0, [], sorted(keep)))
        self.assertEqual((self.names("deferred"), self.names("rejected")), ([], []))
        self.assertIn("would run a claude review", log)
        self.assertFalse(prbot.WATCH_STATE.exists())

    def test_hourly_pass_moves_deferred_files_back_after_reviewing(self):
        self.fail_on = {2}
        name = self.event(2).name
        self.run_queue(limit=5)
        self.assertEqual(self.names("deferred"), [name])
        Watch.run_pass(self, dry_run=True)  # a dry run moves nothing
        self.assertEqual((self.left(), self.names("deferred")), ([], [name]))
        self.fail_on = set()
        log = Watch.run_pass(self, limit=5)
        self.assertEqual((self.left(), self.names("deferred")), ([name], []))
        self.assertEqual(json.loads(Path(self.queue, name).read_text())["attempts"], 1)
        self.assertLess(log.index("pass done"), log.index("moved 1 deferred event file back"))
        # no deferred directory at all is fine
        Path(self.queue, name).unlink()
        prbot.queue_sibling(self.queue, "deferred").rmdir()
        self.assertNotIn("deferred", Watch.run_pass(self, limit=5))

    LOCK_PROBE = ("import fcntl, sys\nf = open(sys.argv[1], 'w')\ntry:\n    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                  "    print('free')\nexcept BlockingIOError:\n    print('held')\n")

    def lock_state(self):
        """'held' or 'free', as another process sees the watch lock right now."""
        r = subprocess.run([sys.executable, "-c", self.LOCK_PROBE, str(prbot.WATCH_STATE.with_name("watch.lock"))],
                           capture_output=True, text=True)
        return r.stdout.strip()

    def test_lock_held_by_another_process_means_nothing_is_touched(self):
        # Codex review of PR 1: the queue had to be checked and moved only under the lock
        keep = self.event(2).name
        Path(self.queue, ".DS_Store").write_text("")
        deferred = prbot.queue_sibling(self.queue, "deferred")
        deferred.mkdir()
        Path(deferred, "old.json").write_text("{}")
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\nf = open(sys.argv[1], 'w')\n"
                                   "fcntl.flock(f, fcntl.LOCK_EX)\nprint('locked', flush=True)\ntime.sleep(60)\n",
                                   str(prbot.WATCH_STATE.with_name("watch.lock"))], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            def no_check(*a):
                raise AssertionError("the queue must not be checked or read without the lock")
            saved, prbot.checked_queue_dir = prbot.checked_queue_dir, no_check
            try:
                code, log = self.run_queue(limit=5)
                self.assertEqual(code, 0)
                import io
                from contextlib import redirect_stdout
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(prbot.cmd_watch(limit=5), 0)
            finally:
                prbot.checked_queue_dir = saved
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertEqual((self.runs, self.left(), self.names("deferred")), ([], sorted([keep, ".DS_Store"]), ["old.json"]))
        self.assertFalse(prbot.queue_sibling(self.queue, "rejected").exists())

    def test_lock_is_held_through_queue_checks_and_the_requeue(self):
        # Codex review of PR 1: the hourly pass released the lock before moving deferred files back
        seen = []
        real_check, real_requeue = prbot.checked_queue_dir, prbot.requeue_deferred
        def check(q):
            seen.append(("check", self.lock_state()))
            return real_check(q)
        def requeue(q):
            seen.append(("requeue", self.lock_state()))
            return real_requeue(q)
        prbot.checked_queue_dir, prbot.requeue_deferred = check, requeue
        try:
            self.event(2)
            self.run_queue(limit=5)
            Watch.run_pass(self, limit=5)
        finally:
            prbot.checked_queue_dir, prbot.requeue_deferred = real_check, real_requeue
        self.assertEqual(seen, [("check", "held"), ("check", "held"), ("requeue", "held")])
        self.assertEqual(self.lock_state(), "free")

    def test_invalid_attempt_counts_start_again_from_zero(self):
        # Codex review of PR 1: "attempts": -1000000 would have put off rejection about a million retries
        self.fail_on = {2}
        deferred = prbot.queue_sibling(self.queue, "deferred")
        for bad in (-1000000, "7", 2.5, True, None):
            with self.subTest(bad=bad):
                for p in (deferred.glob("*") if deferred.is_dir() else []):
                    p.unlink()
                name = self.event(2, attempts=bad).name
                code, log = self.run_queue(limit=5)
                self.assertEqual((code, self.names("deferred")), (2, [name]))
                self.assertEqual(self.read("deferred", name)["attempts"], 1)
                self.assertIn(f"invalid attempts value {bad!r}; counting from 0", log)
                prbot.WATCH_STATE.unlink()   # forget the failure, so the next case fails afresh instead of waiting
        name = self.event(2, attempts=prbot.MAX_ATTEMPTS - 1).name  # a valid count is still honored
        self.run_queue(limit=5)
        self.assertIn(name, self.names("rejected"))

    def test_queue_waits_while_another_run_holds_the_lock(self):
        import fcntl
        keep = self.event(2).name
        with open(prbot.WATCH_STATE.with_name("watch.lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, log = self.run_queue(limit=5)
        self.assertEqual((code, self.runs, self.left()), (0, [], [keep]))
        self.assertIn("another watch run holds the lock", log)


class ReviewTimeout(unittest.TestCase):
    """A reviewer CLI that hangs, with a child that holds its output pipe, the way a stuck codex run did."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake = Path(self.tmp.name, "claude")
        fake.write_text("#!/bin/sh\nsleep 30 &\nsleep 30\n")
        fake.chmod(0o755)
        self.saved = (os.environ.get("PRBOT_REVIEW_TIMEOUT"), os.environ["PATH"])
        os.environ["PRBOT_REVIEW_TIMEOUT"] = "1"
        os.environ["PATH"] = f"{self.tmp.name}{os.pathsep}{os.environ['PATH']}"
        self.pr = {"url": "https://github.com/o/r/pull/2", "title": "t"}

    def tearDown(self):
        timeout, os.environ["PATH"] = self.saved
        if timeout is None:
            os.environ.pop("PRBOT_REVIEW_TIMEOUT", None)
        else:
            os.environ["PRBOT_REVIEW_TIMEOUT"] = timeout
        self.tmp.cleanup()

    def test_bad_timeout_setting_fails_clearly_and_only_where_it_is_used(self):
        # Codex review of PR 1: PRBOT_REVIEW_TIMEOUT=15m broke even `prbot --help` with an import-time traceback
        for bad in ("abc", "15m", "0", "-5"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as cfg:
                env = dict(os.environ, PRBOT_REVIEW_TIMEOUT=bad, PRBOT_CONFIG_DIR=cfg)
                run = lambda *args: subprocess.run([sys.executable, ROOT / "prbot", *args], capture_output=True,
                                                   text=True, env=env)
                r = run("--help")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("prbot watch", r.stdout)
                self.assertEqual(run("classify", "--", "pr", "view", "1").returncode, 1)
                for args in (("watch", "--dry-run"), ("review", "1", "-R", "o/r", "--route-only")):
                    r = run(*args)
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertIn(f"PRBOT_REVIEW_TIMEOUT must be a positive whole number of seconds, not {bad!r}",
                                  r.stderr)
                    self.assertNotIn("Traceback", r.stderr)

    def test_hung_review_is_stopped_and_fails(self):
        import io, time
        from contextlib import redirect_stderr
        err, start = io.StringIO(), time.monotonic()
        with redirect_stderr(err), self.assertRaises(SystemExit):
            prbot.run_review("claude", self.pr, "diff", self.tmp.name)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("claude review timed out after 1s", err.getvalue())

    def test_watch_moves_on_to_the_next_pr_after_a_timeout(self):
        import io, time
        from contextlib import redirect_stderr
        w = Watch("run_pass")
        w.setUp()
        try:
            def review(n, repo, mode, only):
                w.runs.append((int(n), only[0]))
                if int(n) == 2:
                    prbot.run_review("claude", self.pr, "diff", self.tmp.name)
            prbot.cmd_review = review
            start = time.monotonic()
            with redirect_stderr(io.StringIO()):
                log = w.run_pass(limit=5)
            self.assertLess(time.monotonic() - start, 10)
            self.assertEqual(w.runs, [(2, "claude"), (5, "claude")])
            self.assertIn("o/r#2@bbbbbbbbbb: claude review failed", log)
            state = json.loads(prbot.WATCH_STATE.read_text())
            self.assertIn("failed_claude", state["o/r#2@" + "b" * 10])  # not reviewed, retried after the delay
        finally:
            w.tearDown()


class ReviewPrompt(unittest.TestCase):
    """Both reviewer CLIs get the same full prompt, so Codex reviews are as thorough as Claude's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        for name in ("claude", "codex"):
            fake = Path(self.tmp.name, name)
            # record the arguments, then reply like a reviewer: claude on stdout, codex in the -o file
            fake.write_text(f"#!/usr/bin/env python3\nimport json, sys\n"
                            f"json.dump(sys.argv[1:], open({str(Path(self.tmp.name, name + '.args'))!r}, 'w'))\n"
                            "a = sys.argv\n"
                            "if '-o' in a: open(a[a.index('-o') + 1], 'w').write('Nothing worth reporting.')\n"
                            "else: print('Nothing worth reporting.')\n")
            fake.chmod(0o755)
        self.saved = os.environ["PATH"]
        os.environ["PATH"] = f"{self.tmp.name}{os.pathsep}{os.environ['PATH']}"
        self.pr = {"url": "https://github.com/o/r/pull/2", "title": "Fix {braces} in titles"}

    def tearDown(self):
        os.environ["PATH"] = self.saved
        self.tmp.cleanup()

    def test_both_reviewers_get_every_check(self):
        prompts = {}
        for reviewer in ("claude", "codex"):
            self.assertEqual(prbot.run_review(reviewer, self.pr, "diff", self.tmp.name), "Nothing worth reporting.")
            args = json.loads(Path(self.tmp.name, reviewer + ".args").read_text())
            prompts[reviewer] = next(a for a in args if a.startswith("You are reviewing"))
        self.assertEqual(prompts["claude"], prompts["codex"])
        prompt = prompts["claude"]
        self.assertIn('pull request https://github.com/o/r/pull/2: "Fix {braces} in titles"', prompt)
        for check in ("AGENTS.md", "Silent failures:", "Error messages:", "Security:", "Tests:", "Comments and docs:",
                      "Project rules:", "at least 80% sure"):
            self.assertIn(check, prompt)
        # count_findings relies on the reviewer labelling findings P1/P2/P3 and using the labels for nothing else
        self.assertIn("P1 breaks something, P2 likely bug, P3 minor", prompt)
        self.assertIn("Use the labels P1, P2 and P3 only to label findings", prompt)


class BackupReviewWorkflow(unittest.TestCase):
    """The GitHub Actions backup reviewer must ask for the same review as prbot."""

    def test_workflow_prompt_matches_prbot(self):
        import re, textwrap
        text = (ROOT / "integrations" / "github-actions" / "backup-review.yml").read_text()
        block = text[text.index("          prompt: |\n") + len("          prompt: |\n"):text.index("          claude_args:")]
        workflow = textwrap.dedent(block)
        start, end = "First read the repository", "Use the labels P1, P2 and P3 only to label findings"
        shared = prbot.REVIEW_PROMPT[prbot.REVIEW_PROMPT.index(start):prbot.REVIEW_PROMPT.index(end) + len(end)]
        self.assertIn(shared, workflow)
        # it finds earlier reviews by the line prbot writes
        self.assertTrue(prbot.REVIEWED.search("Reviewed commit `9e7cb5f291`"))
        self.assertIn("(Reviewed commit|Backup review of commit)", text)
        # the reviewer gets read-only tools; only the posting step has a token that can write
        self.assertRegex(text, r"--allowedTools Read,Grep,Glob\n")
        self.assertIn("--disallowedTools Bash,Edit,Write", text)


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

    def test_codex_exemption_bypasses_denied(self):
        """issue #19: an @codex body never lets a review or a non-trigger comment through on the human account."""
        for command in ("gh pr review 1 --approve -b '@codex looks good'",
                        "gh pr review 1 --request-changes -b '@codex P1: broken'",
                        "gh pr review 1 -b '@codex review'",
                        "gh -R o/r pr review 1 --approve --body='@codex'",
                        "gh pr comment 1 -b '@codexfoo hello'",
                        "gh pr comment 1 -b ' @codex P1: the parser drops quotes'",
                        "gh pr comment 1 -b '@codex review' --edit-last",
                        "gh api repos/o/r/pulls/1/reviews -f event=APPROVE -f body='@codex looks good'"):
            with self.subTest(command=command):
                self.assertEqual(self.run_hook(command)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.run_hook("gh pr comment 1 -b '@codex review'"))
        self.assertIsNone(self.run_hook("gh pr comment 1 -b '@codex'"))

    def test_allows_reads_and_routed_writes(self):
        self.assertIsNone(self.run_hook("gh pr view 15 --json reviews"))
        self.assertIsNone(self.run_hook("prbot gh claude -- pr comment 15 -b ok"))

    HOOK = ROOT / "integrations/claude-code/guard_gh_identity.py"

    def reason(self, out):
        self.assertIsNotNone(out)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def break_config(self):
        Path(self.tmp.name, "config.json").write_text("{bad")

    def run_raw(self, stdin):
        r = subprocess.run([sys.executable, self.HOOK], input=stdin, capture_output=True, text=True, env=self.env)
        return r

    def run_with_stub(self, setup, payload):
        """Run the hook in a subprocess after `setup` (Python source) patches `module`.

        `payload` is a Python expression, evaluated after `setup`, so it can use `tmp`."""
        d = tempfile.mkdtemp(dir=self.tmp.name)
        runner = Path(d, "runner.py")
        runner.write_text(
            "import importlib.util, io, json, os, sys, types\n"
            "from importlib.machinery import SourceFileLoader\n"
            f"loader = SourceFileLoader('guard', {str(self.HOOK)!r})\n"
            "module = importlib.util.module_from_spec(importlib.util.spec_from_loader('guard', loader))\n"
            "loader.exec_module(module)\n"
            f"tmp = {d!r}\n"
            + setup +
            f"\nsys.stdin = io.StringIO(json.dumps({payload}))\n"
            "sys.exit(module.main())\n")
        return subprocess.run([sys.executable, "-B", runner], capture_output=True, text=True, env=self.env), d

    def test_broken_config_feedback_write_gets_routing_fix_and_guard_note(self):
        """(a) A broken config still gets the routing fix for a gh write, plus a guard note without config contents."""
        self.break_config()
        reason = self.reason(self.run_hook("gh pr comment 1 -b x"))
        self.assertIn("gh claude -- pr comment 1 -b x", reason)
        self.assertIn("prbot guard failed", reason)
        self.assertIn("~/.config/prbot/config.json", reason)
        self.assertNotIn("{bad", reason)
        self.assertNotIn("Traceback", reason)

    def test_broken_config_denies_routed_prbot_call_bare_token(self):
        """(b) With a broken config even a routed prbot call is refused on the bare token."""
        self.break_config()
        reason = self.reason(self.run_hook("./prbot gh claude -- pr comment 1 -b x"))
        self.assertTrue(reason.startswith("prbot guard failed"), reason)
        self.assertIn("~/.config/prbot/config.json", reason)

    def test_broken_config_denies_executable_path_gh_read(self):
        """(c) With a broken config a gh read called by its full path is refused."""
        self.break_config()
        reason = self.reason(self.run_hook("/usr/bin/gh pr view 1"))
        self.assertTrue(reason.startswith("prbot guard failed"), reason)

    def test_broken_config_denies_git_push_and_commit(self):
        """(d) With a broken config git push and commit are refused as candidates; other commands pass."""
        self.break_config()
        repo = self.make_repo("kennylhilljr")
        for command in ("git push origin main", "git commit -m fix"):
            with self.subTest(command=command):
                reason = self.reason(self.run_hook_in(command, repo))
                self.assertIn("prbot guard failed", reason)
                self.assertIn("git commit/push", reason)
        self.assertIsNone(self.run_hook_in("ls -la", repo))

    def test_config_is_a_directory_behaves_like_broken_config(self):
        """(e) A config.json that is a directory is handled like a broken config and named in the reason."""
        os.makedirs(Path(self.tmp.name, "config.json"))
        reason = self.reason(self.run_hook("gh pr comment 1 -b x"))
        self.assertIn("gh claude -- pr comment 1 -b x", reason)
        self.assertIn("IsADirectoryError", reason)

    def test_non_object_payload_allowed(self):
        """(f) A JSON payload that is not an object is allowed silently."""
        for stdin in ("[]", '"string"', "42"):
            with self.subTest(stdin=stdin):
                r = self.run_raw(stdin)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "")

    def test_non_string_tool_name_allowed(self):
        """(g) A tool_name that is not a string is allowed silently."""
        r = self.run_raw(json.dumps({"tool_name": 42, "tool_input": {"command": "gh pr comment 1 -b x"}}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")

    def test_non_string_command_treated_as_empty(self):
        """(h) A command that is not a string is treated as empty and allowed."""
        r = self.run_raw(json.dumps({"tool_name": "Bash", "tool_input": {"command": 123}}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")

    def test_non_object_tool_input_allowed(self):
        """(i) A tool_input that is not an object is allowed silently."""
        r = self.run_raw(json.dumps({"tool_name": "Bash", "tool_input": "oops"}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")

    def test_unexpected_import_failure_denies_bash_writes_without_config_wording(self):
        """(j) A prbot import failure denies gh writes without config advice and allows unrelated commands."""
        setup = "module.load_prbot = lambda: (_ for _ in ()).throw(ImportError('simulated'))\n"
        r, _ = self.run_with_stub(setup, repr({"tool_name": "Bash", "tool_input": {"command": "gh pr comment 1 -b x"}}))
        self.assertEqual(r.returncode, 0, r.stderr)
        reason = self.reason(json.loads(r.stdout))
        self.assertTrue(reason.startswith("prbot guard failed (ImportError: simulated)"), reason)
        self.assertNotIn("~/.config/prbot/config.json", reason)
        r, _ = self.run_with_stub(setup, repr({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")

    def test_unloadable_prbot_denies_commands_that_can_spell_gh(self):
        """(j2) When prbot cannot load, commands that can reach gh without a gh/prbot/git token are denied."""
        setup = "module.load_prbot = lambda: (_ for _ in ()).throw(ImportError('simulated'))\n"
        for command in ("g\\h pr comment 12 -b x", "'g'h pr comment 12 -b x", "\"g\"h pr comment 12 -b x",
                        "x=g; ${x}h pr comment 12 -b x", "$'\\x67h' pr comment 12 -b x", "/usr/local/bin/g? pr comment 1",
                        "echo Z2ggcHIgY29tbWVudCAxIC1iIHg= | base64 -d | sh", "ls $(echo)", "ls `true`",
                        "PATH=/tmp ls", "env ls", "bash -c ls", "(ls)", "ls; make post", "grep 'a;b' f",
                        "rg --pre ./post x", "ls | sort --compress-program=./post", "printf -v PATH .; ls pr comment 1",
                        "echo x {PATH}>/dev/null; ls pr comment 1", "file -z x.gz", "diff -l a b", "egrep x f"):
            with self.subTest(command=command):
                r, _ = self.run_with_stub(setup, repr({"tool_name": "Bash", "tool_input": {"command": command}}))
                self.assertEqual(r.returncode, 0, r.stderr)
                reason = self.reason(json.loads(r.stdout))
                self.assertTrue(reason.startswith("prbot guard failed (ImportError: simulated)"), reason)

    def test_untrustworthy_configs_deny_git_push_to_bot_repos(self):
        """(j4) Configs the owner check could silently ignore (wrong owner type, dangling symlink, symlink loop,
        huge integer) make the guard fail closed for a git push to a bot repo."""
        config = Path(self.tmp.name, "config.json")
        repo = self.make_repo("kennylhilljr")
        cases = {
            "integer owner": lambda: config.write_text(json.dumps({"claude": {"owner": 17}})),
            "bot entry not an object": lambda: config.write_text(json.dumps({"claude": "kennylhilljr"})),
            "missing owner": lambda: config.write_text(json.dumps({"claude": {}})),
            "dangling symlink": lambda: config.symlink_to(Path(self.tmp.name, "missing.json")),
            "symlink loop": lambda: config.symlink_to(config),
        }
        cases["huge integer"] = lambda: config.write_text('{"x": ' + "9" * 5000 + "}")  # refused on every Python
        for name, make in cases.items():
            with self.subTest(name):
                make()
                reason = self.reason(self.run_hook_in("git push origin main", repo))
                self.assertIn("prbot guard failed", reason)
                config.unlink()

    def test_prbot_raising_systemexit_still_denies(self):
        """(j5) prbot's die() raises SystemExit; even SystemExit(0) from a broken prbot ends in a denial, not allow."""
        for code in (0, 2):
            with self.subTest(code=code):
                setup = f"module.load_prbot = lambda: (_ for _ in ()).throw(SystemExit({code}))\n"
                r, _ = self.run_with_stub(setup, repr({"tool_name": "Bash", "tool_input": {"command": "gh pr comment 1 -b x"}}))
                self.assertEqual(r.returncode, 0, r.stderr)
                reason = self.reason(json.loads(r.stdout))
                self.assertTrue(reason.startswith(f"prbot guard failed (SystemExit: {code})"), reason)

    def test_unloadable_prbot_allows_simple_unrelated_commands(self):
        """(j3) When prbot cannot load, simple commands that cannot start another program still run."""
        setup = "module.load_prbot = lambda: (_ for _ in ()).throw(ImportError('simulated'))\n"
        for command in ("ls", "ls -la", "cat README.md | head -5", "grep -n 'guard failed' README.md && wc -l README.md",
                        "ls 2>&1 | head", "pwd; echo done", ""):
            with self.subTest(command=command):
                r, _ = self.run_with_stub(setup, repr({"tool_name": "Bash", "tool_input": {"command": command}}))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "")

    def test_broken_guard_cannot_overwrite_hook(self):
        """(j6) While the guard is broken, redirections and output-file options that could overwrite the hook
        (or any file) are refused; descriptor copies and writes to /dev/null still pass."""
        setup = "module.load_prbot = lambda: (_ for _ in ()).throw(ImportError('simulated'))\n"
        hook = str(self.HOOK)
        for command in (f"echo > {hook}", f"echo >{hook}", f": > {hook}", f"cat /dev/null >> {hook}",
                        f"ls >| {hook}", f"ls &> {hook}", f"ls 2> {hook}", f"echo x 1>{hook}", f"ls >& {hook}",
                        f"cat <> {hook}", f"ls 2>&1 > {hook}", f"ls > /dev/null.py", f"ls 2>&1x",
                        f"uniq /dev/null {hook}", f"tree -o {hook}", f"echo hi | tee {hook}"):
            with self.subTest(command=command):
                r, _ = self.run_with_stub(setup, repr({"tool_name": "Bash", "tool_input": {"command": command}}))
                self.assertEqual(r.returncode, 0, r.stderr)
                reason = self.reason(json.loads(r.stdout))
                self.assertTrue(reason.startswith("prbot guard failed (ImportError: simulated)"), reason)
        for command in ("ls 2>&1 | head", "ls 2>/dev/null", "ls >&2", "ls > /dev/null 2>&1", "ls &>/dev/null",
                        "ls 2>&-"):
            with self.subTest(command=command):
                r, _ = self.run_with_stub(setup, repr({"tool_name": "Bash", "tool_input": {"command": command}}))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, "")

    def test_broken_config_refuses_overwriting_the_hook(self):
        """(j7) The same with a real broken config instead of a stub: `echo > hook` is denied."""
        self.break_config()
        reason = self.reason(self.run_hook(f"echo > {self.HOOK}"))
        self.assertIn("prbot guard failed (ConfigError", reason)

    def test_dangling_config_dir_fails_closed(self):
        """(j8) PRBOT_CONFIG_DIR (or a parent) is a symlink to a missing directory: the guard has no owners to
        trust, so it fails closed instead of reading the config as empty and allowing a personal push."""
        repo = self.make_repo("kennylhilljr")
        for depth in ("dir", "parent", "dotdot"):
            with self.subTest(depth):
                link = Path(tempfile.mkdtemp(dir=self.tmp.name)) / "prbot"
                link.symlink_to(Path(self.tmp.name, "gone", "prbot"))
                cfg_dir = {"dir": link, "parent": link / "nested", "dotdot": link / ".." / "cfg"}[depth]
                self.env["PRBOT_CONFIG_DIR"] = str(cfg_dir)
                for command in ("git push origin main", 'git commit -m "x"'):
                    reason = self.reason(self.run_hook_in(command, repo))
                    self.assertIn("prbot guard failed (ConfigError", reason)
                    self.assertIn("is a symlink to something that does not exist", reason)

    def test_load_config_oserror_with_unrouted_writes_runtimeerror_still_denies(self):
        """(k) When load_config and unrouted_writes both raise, the hook still denies and names only the first error."""
        setup = (
            "def load_config():\n"
            "    raise OSError('simulated')\n"
            "stub = types.SimpleNamespace(\n"
            "    AGENTS=('claude', 'codex'),\n"
            "    load_config=load_config,\n"
            "    unrouted_writes=lambda c, t=False: (_ for _ in ()).throw(RuntimeError('also bad')),\n"
            "    git_writes=lambda c: [],\n"
            "    repo_from_git=lambda p=None: None,\n"
            ")\n"
            "module.load_prbot = lambda: stub\n")
        r, _ = self.run_with_stub(setup, repr({"tool_name": "Bash", "tool_input": {"command": "gh pr comment 1 -b x"}}))
        self.assertEqual(r.returncode, 0, r.stderr)
        reason = self.reason(json.loads(r.stdout))
        self.assertTrue(reason.startswith("prbot guard failed (OSError: simulated)"), reason)
        self.assertNotIn("~/.config/prbot/config.json", reason)
        self.assertNotIn("RuntimeError", reason)

    def test_repo_from_git_oserror_variant(self):
        """(l) An OSError from repo_from_git during a git push check is denied and named in the reason."""
        setup = (
            "marker = os.path.join(tmp, \"repo_from_git.called\")\n"
            "def repo_from_git(cwd=None):\n"
            "    open(marker, \"w\").close()\n"
            "    raise OSError(\"boom\")\n"
            "stub = types.SimpleNamespace(\n"
            "    AGENTS=(\"claude\", \"codex\"),\n"
            "    load_config=lambda: {\"claude\": {\"owner\": \"kennylhilljr\"}},\n"
            "    git_writes=lambda c: [([\"push\", \"origin\", \"main\"], None)],\n"
            "    guard_bash=lambda c, cwd, owners, t=False: repo_from_git(cwd),\n"
            "    repo_from_git=repo_from_git,\n"
            "    unrouted_writes=lambda c, t=False: [],\n"
            ")\n"
            "module.load_prbot = lambda: stub\n")
        payload = "{'tool_name': 'Bash', 'tool_input': {'command': 'git push origin main'}, 'cwd': tmp}"
        r, tmp = self.run_with_stub(setup, payload)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(os.path.join(tmp, "repo_from_git.called")))
        reason = self.reason(json.loads(r.stdout))
        self.assertIn("prbot guard failed", reason)
        self.assertIn("OSError: boom", reason)
        self.assertNotIn("~/.config/prbot/config.json", reason)

    def test_fallback_regex_stays_fast_on_repeated_git_options(self):
        """(m) Repeated git options before a non-write do not make the fallback check backtrack for seconds."""
        setup = "module.load_prbot = lambda: (_ for _ in ()).throw(ImportError('simulated'))\n"
        command = "git " + "-c a.b=c " * 24 + "status; gh pr comment 1 -b x"
        d = tempfile.mkdtemp(dir=self.tmp.name)
        runner = Path(d, "runner.py")
        runner.write_text(
            "import importlib.util, io, json, sys\n"
            "from importlib.machinery import SourceFileLoader\n"
            f"loader = SourceFileLoader('guard', {str(self.HOOK)!r})\n"
            "module = importlib.util.module_from_spec(importlib.util.spec_from_loader('guard', loader))\n"
            "loader.exec_module(module)\n"
            + setup +
            f"sys.stdin = io.StringIO(json.dumps({{'tool_name': 'Bash', 'tool_input': {{'command': {command!r}}}}}))\n"
            "sys.exit(module.main())\n")
        r = subprocess.run([sys.executable, "-B", runner], capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        reason = self.reason(json.loads(r.stdout))
        self.assertTrue(reason.startswith("prbot guard failed (ImportError: simulated)"), reason)
        self.assertIn("refusing gh/prbot/git call", reason)
        loader = SourceFileLoader("guard_regex", str(self.HOOK))
        hook = importlib.util.module_from_spec(importlib.util.spec_from_loader("guard_regex", loader))
        loader.exec_module(hook)
        start = time.monotonic()
        self.assertIsNone(hook.GIT_WRITE_FALLBACK.search("git " + "-c a.b=c " * 2000 + "status"))
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertTrue(hook.GIT_WRITE_FALLBACK.search("git " + "-c a.b=c " * 24 + "push origin main"))

    def test_denies_nested_feedback(self):
        cases = [
            ("for i in 1 2; do gh pr comment 7 -b x; done", "gh claude -- pr comment 7 -b x"),
            ('bash -c "gh pr review 15 --comment -b \'Two P2 issues\'"', "gh claude -- pr review 15 --comment -b 'Two P2 issues'"),
            ("sh -c 'gh -R o/r issue comment 9 -b note'", "gh claude -- -R o/r issue comment 9 -b note"),
        ]
        for command, fix in cases:
            with self.subTest(command=command):
                out = self.run_hook(command)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn(fix, out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_off_safe_list_denial_names_reason_and_route(self):
        """A gh call off the safe list is denied with the reason and the prbot route; hidden names and file
        redirections are denied with their reason; unrelated commands pass."""
        reason = self.reason(self.run_hook("gh label create urgent"))
        self.assertIn("gh claude -- label create urgent", reason)
        self.assertIn("safe list", reason)
        self.assertIn("output redirection to a file", self.reason(self.run_hook("gh pr diff 1 > /tmp/pr.diff")))
        self.assertIn("only known at run time", self.reason(self.run_hook("x=g; ${x}h pr comment 1 -b x")))
        self.assertIn("reading commands from stdin", self.reason(self.run_hook("echo Z2g= | base64 -d | sh")))
        self.assertIsNone(self.run_hook("python3 -B -m unittest discover -s tests -t . && git status"))
        self.assertIsNone(self.run_hook("npm install && npm run build > build.log"))

    def test_denies_wrapped_git_push(self):
        Path(self.tmp.name, "config.json").write_text(json.dumps({"claude": {"owner": "kennylhilljr", "slug": "claude-pr-gh"}}))
        mine, theirs = self.make_repo("kennylhilljr"), self.make_repo("someone-else")
        for command in ("sudo -n git push origin main", "env -u FOO git push origin main",
                        "bash -c 'git push origin main'", "if true; then git push origin main; fi"):
            with self.subTest(command=command):
                out = self.run_hook_in(command, mine)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
                self.assertIn("git claude -- push origin main", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.run_hook_in(f"nohup git -C {shlex.quote(mine)} push", self.tmp.name)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        # the safe list has no wrappers, so a wrapped push is refused in any repo
        self.assertIn("wrappers such as sudo", self.reason(self.run_hook_in("sudo -n git push origin main", theirs)))
        self.assertIsNone(self.run_hook_in("git push origin main", theirs))
        self.assertIsNone(self.run_hook_in("bash -c 'prbot git claude -- push origin main'", mine))


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

    WRITE = ["pr", "comment", "1", "-b", "x"]

    def _sandbox(self, tmp, stub=None):
        """Copy the shim into tmp so it resolves <tmp>/prbot, with a fake real gh that leaves a marker."""
        bindir = Path(tmp) / "integrations/codex/bin"
        bindir.mkdir(parents=True)
        shim = bindir / "gh"
        shutil.copy2(ROOT / "integrations/codex/bin/gh", shim)
        os.chmod(shim, 0o755)
        fakebin = Path(tmp) / "fakebin"
        fakebin.mkdir()
        (fakebin / "gh").write_text(f'#!/bin/sh\ntouch "{tmp}/gh.called"\nexit 0\n')
        os.chmod(fakebin / "gh", 0o755)
        (Path(tmp) / "cfg").mkdir()
        if stub is not None:
            word, code = stub
            body = f'#!/bin/sh\ntouch "{tmp}/prbot.called"\n'
            if word:
                body += f"echo {word}\n"
            body += f"exit {code}\n"
            (Path(tmp) / "prbot").write_text(body)
            os.chmod(Path(tmp) / "prbot", 0o755)
        env = {k: v for k, v in os.environ.items() if k != "PRBOT_ACTIVE"}
        env["PRBOT_CONFIG_DIR"] = str(Path(tmp) / "cfg")
        env["PATH"] = f"{fakebin}:/usr/bin:/bin"
        return str(shim), env

    def test_classify_result_matrix(self):
        """(a) Only exit 0 with 'write' or exit 1 with 'read' is trusted; every other classify result refuses."""
        refused = [(("", 1), 1), (("", 2), 2), (("", 3), 3), (("", 127), 127), (("", 0), 1),
                   (("read", 2), 2), (("write", 1), 1), (("mischief", 0), 1)]
        for stub, expected in refused:
            with self.subTest(stub=stub), tempfile.TemporaryDirectory() as tmp:
                shim, env = self._sandbox(tmp, stub)
                r = subprocess.run([shim, *self.WRITE], capture_output=True, text=True, env=env, timeout=30)
                self.assertTrue(os.path.exists(f"{tmp}/prbot.called"))
                self.assertFalse(os.path.exists(f"{tmp}/gh.called"), "shim ran the real gh")
                self.assertEqual(r.returncode, expected, r.stderr)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("refusing to run real gh", r.stderr)
        with self.subTest(stub=("read", 1)), tempfile.TemporaryDirectory() as tmp:
            shim, env = self._sandbox(tmp, ("read", 1))
            r = subprocess.run([shim, *self.WRITE], capture_output=True, text=True, env=env, timeout=30)
            self.assertTrue(os.path.exists(f"{tmp}/prbot.called"))
            self.assertTrue(os.path.exists(f"{tmp}/gh.called"))
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_or_unexecutable_prbot_refuses(self):
        """(b) A prbot the shim cannot run is refused, never treated as a read."""
        for case in ("missing", "not executable"):
            with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                shim, env = self._sandbox(tmp)
                if case == "not executable":
                    (Path(tmp) / "prbot").write_text("#!/bin/sh\nexit 0\n")
                    os.chmod(Path(tmp) / "prbot", 0o644)
                r = subprocess.run([shim, *self.WRITE], capture_output=True, text=True, env=env, timeout=30)
                self.assertFalse(os.path.exists(f"{tmp}/gh.called"), "shim ran the real gh")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("refusing to run real gh", r.stderr)

    def test_prbot_active_does_not_bypass_classify(self):
        """(d) A non-empty PRBOT_ACTIVE never skips classify: a missing or failing prbot still refuses."""
        for stub in (None, ("", 4), ("", 2)):
            with self.subTest(stub=stub), tempfile.TemporaryDirectory() as tmp:
                shim, env = self._sandbox(tmp, stub)
                env["PRBOT_ACTIVE"] = "codex"
                r = subprocess.run([shim, *self.WRITE], capture_output=True, text=True, env=env, timeout=30)
                self.assertFalse(os.path.exists(f"{tmp}/gh.called"), "PRBOT_ACTIVE let the shim run the real gh")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("refusing to run real gh", r.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            shim, env = self._sandbox(tmp, ("write", 0))
            env["PRBOT_ACTIVE"] = "codex"
            subprocess.run([shim, *self.WRITE], capture_output=True, text=True, env=env, timeout=30)
            self.assertTrue(os.path.exists(f"{tmp}/prbot.called"), "PRBOT_ACTIVE skipped classify")
            self.assertFalse(os.path.exists(f"{tmp}/gh.called"))

    def test_real_gh_override_pointing_at_the_shim_is_refused(self):
        """(e) PRBOT_REAL_GH naming the shim, directly or through a symlink, stops prbot instead of looping."""
        shim = ROOT / "integrations/codex/bin/gh"
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "gh"
            link.symlink_to(shim)
            for target in (shim, link):
                with self.subTest(target=str(target)):
                    env = {k: v for k, v in os.environ.items() if k != "PRBOT_ACTIVE"}
                    env["PRBOT_CONFIG_DIR"] = tmp
                    env["PRBOT_REAL_GH"] = str(target)
                    env["PATH"] = f"{os.path.dirname(sys.executable)}:/usr/bin:/bin"
                    r = subprocess.run([str(shim), "pr", "comment", "1", "-R", "o/r", "-b", "x"],
                                       capture_output=True, text=True, env=env, timeout=30)
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertIn("PRBOT_REAL_GH points at the Codex gh shim", r.stderr)

    def test_real_gh_override_with_other_case_or_hardlink_is_refused(self):
        """(e2) The shim is recognised by file identity: a differently cased path (same file on a case-insensitive
        filesystem such as macOS) or a hard link to the shim is refused too."""
        shim = ROOT / "integrations/codex/bin/gh"
        targets = []
        swapped = Path(str(shim).swapcase())
        if swapped.exists() and os.path.samefile(swapped, shim):
            targets.append(swapped)
        with tempfile.TemporaryDirectory() as tmp:
            hard = Path(tmp) / "gh"
            try:
                os.link(shim, hard)
                targets.append(hard)
            except OSError:
                pass
            if not targets:
                self.skipTest("case-sensitive filesystem and no hard links")
            for target in targets:
                with self.subTest(target=str(target)):
                    env = {k: v for k, v in os.environ.items() if k != "PRBOT_ACTIVE"}
                    env["PRBOT_CONFIG_DIR"] = tmp
                    env["PRBOT_REAL_GH"] = str(target)
                    env["PATH"] = f"{os.path.dirname(sys.executable)}:/usr/bin:/bin"
                    r = subprocess.run([str(shim), "pr", "comment", "1", "-R", "o/r", "-b", "x"],
                                       capture_output=True, text=True, env=env, timeout=30)
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertIn("PRBOT_REAL_GH points at the Codex gh shim", r.stderr)

    def test_broken_config_refuses_and_shows_classify_message(self):
        """(c) A broken config.json stops the shim with exit 4 and passes classify's own message through."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg"
            cfg.mkdir()
            (cfg / "config.json").write_text("{bad")
            fakebin = Path(tmp) / "fakebin"
            fakebin.mkdir()
            (fakebin / "gh").write_text(f'#!/bin/sh\ntouch "{tmp}/gh.called"\nexit 0\n')
            os.chmod(fakebin / "gh", 0o755)
            env = {k: v for k, v in os.environ.items() if k != "PRBOT_ACTIVE"}
            env["PRBOT_CONFIG_DIR"] = str(cfg)
            env["PATH"] = f"{fakebin}:{os.path.dirname(sys.executable)}:/usr/bin:/bin"
            r = subprocess.run([str(ROOT / "integrations/codex/bin/gh"), "pr", "comment", "1", "-R", "o/r", "-b", "x"],
                               capture_output=True, text=True, env=env, timeout=30)
            self.assertFalse(os.path.exists(f"{tmp}/gh.called"), "shim ran the real gh")
            self.assertEqual(r.returncode, 4, r.stderr)
            self.assertIn(str(Path(cfg) / "config.json"), r.stderr)
            self.assertIn("fix or remove it", r.stderr)
            self.assertIn("refusing to run real gh", r.stderr)
            self.assertNotIn("{bad", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_codex_exemption_bypasses_never_reach_real_gh(self):
        """issue #19: an approval or non-trigger comment with an @codex body goes to prbot, never the real gh."""
        for argv in (["pr", "review", "1", "--approve", "-b", "@codex looks good"],
                     ["pr", "review", "1", "--request-changes", "-b", "@codex P1: broken"],
                     ["pr", "comment", "1", "-b", "@codexfoo hello"]):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as fake:
                calls = Path(fake, "calls.log")
                gh = Path(fake, "gh")  # stands in for the real gh, so nothing can post even if classification fails
                gh.write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(calls))}\nexit 0\n")
                gh.chmod(0o755)
                env = dict(os.environ, PRBOT_CONFIG_DIR=cfg, PRBOT_BOT="codex",
                           PATH=os.pathsep.join([str(ROOT / "integrations/codex/bin"), fake, os.environ["PATH"]]))
                env.pop("PRBOT_ACTIVE", None)
                r = subprocess.run(["gh", "-R", "o/r", *argv], capture_output=True, text=True, env=env, timeout=30)
                self.assertFalse(calls.exists(), "the shim passed a feedback write to gh")
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("bot 'codex' is not registered", r.stderr)

    def _fake_gh_env(self, cfg, fake, ghcfg):
        calls = Path(fake, "calls.log")
        gh = Path(fake, "gh")  # stands in for the real gh, so nothing can post even if classification fails
        gh.write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(calls))}\nexit 0\n")
        gh.chmod(0o755)
        env = dict(os.environ, PRBOT_CONFIG_DIR=cfg, PRBOT_BOT="codex", GH_CONFIG_DIR=ghcfg,
                   PATH=os.pathsep.join([str(ROOT / "integrations/codex/bin"), fake, os.path.dirname(sys.executable),
                                         "/usr/bin", "/bin"]))
        env.pop("PRBOT_ACTIVE", None)
        return calls, env

    def test_gh_aliases_and_extensions_never_reach_real_gh(self):
        """issue #20: the shim refuses every alias and extension bypass before the (fake) real gh runs, whatever
        gh's config.yml holds, and still passes the bots' reads through."""
        for name, args, config in ISSUE_20_BYPASSES:
            with self.subTest(name, args=args), tempfile.TemporaryDirectory() as cfg, \
                    tempfile.TemporaryDirectory() as fake, tempfile.TemporaryDirectory() as ghcfg:
                if config is not None:
                    Path(ghcfg, "config.yml").write_text(config)
                calls, env = self._fake_gh_env(cfg, fake, ghcfg)
                r = subprocess.run(["gh", *args], capture_output=True, text=True, env=env, timeout=30)
                self.assertFalse(calls.exists(), "the shim ran the real gh")
                self.assertEqual(r.returncode, 4, r.stderr)
                self.assertIn("refusing to run real gh", r.stderr)
        with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as fake, \
                tempfile.TemporaryDirectory() as ghcfg:
            calls, env = self._fake_gh_env(cfg, fake, ghcfg)
            for args in BOT_READS:
                with self.subTest(read=args):
                    r = subprocess.run(["gh", *args], capture_output=True, text=True, env=env, timeout=30)
                    self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(calls.read_text().splitlines(), [" ".join(a) for a in BOT_READS])

    def test_routes_global_repo_feedback(self):
        with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as fake:
            calls = Path(fake, "calls.log")
            gh = Path(fake, "gh")  # stands in for the real gh, so nothing can post even if classification fails
            gh.write_text(f"#!/bin/sh\necho \"$@\" >> {shlex.quote(str(calls))}\nexit 0\n")
            gh.chmod(0o755)
            env = dict(os.environ, PRBOT_CONFIG_DIR=cfg, PRBOT_BOT="codex",
                       PATH=os.pathsep.join([str(ROOT / "integrations/codex/bin"), fake, os.environ["PATH"]]))
            env.pop("PRBOT_ACTIVE", None)
            r = subprocess.run(["gh", "-R", "o/r", "pr", "comment", "1", "-b", "x"], capture_output=True, text=True, env=env)
            self.assertFalse(calls.exists(), "the shim passed a feedback write to gh")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("bot 'codex' is not registered", r.stderr)


class GuardDocs(unittest.TestCase):
    @staticmethod
    def norm(text):
        return " ".join(text.replace("`", "").split())

    def test_shim_safe_list_is_documented(self):
        """issue #20: the docs say the shim refuses aliases and extensions and that classify never reads config.yml."""
        for name in ("README.md", "DESIGN.md", "AGENTS.md"):
            text = self.norm((ROOT / name).read_text())
            with self.subTest(name):
                self.assertIn("alias", text)
                self.assertIn("extension", text)
        design = self.norm((ROOT / "DESIGN.md").read_text())
        for phrase in ("classify never reads it", "gh alias set human-review 'pr review'", "co is a config alias"):
            self.assertIn(phrase, design)
        self.assertIn("GH_SAFE_SUBCOMMANDS", self.norm((ROOT / "AGENTS.md").read_text()))

    def between(self, name, start, end):
        text = (ROOT / name).read_text()
        a = text.index(start)
        return self.norm(text[a:text.index(end, a + len(start))])

    def test_feedback_scope(self):
        readme = self.between("README.md", "The hook denies", "\n\n")
        enforcement = self.between("DESIGN.md", "## Enforcement", "\n## ")
        flow = self.between("DESIGN.md", "## How a post becomes a bot post", "\n## ")
        for name, text in (("README hook paragraph", readme), ("DESIGN Enforcement", enforcement)):
            with self.subTest(section=name):
                self.assertIn("feedback", text)
                self.assertIn("safe list", text)
                self.assertIn("@codex", text)
                self.assertIn("gh pr create, gh issue create, gh pr merge and gh pr edit", text)
                self.assertIn("sh -c", text)
                self.assertIn("output redirection to a file", text)
                self.assertIn("xargs -I", text)
                self.assertIn("not a sandbox", text)
        for phrase in ("gh pr close and gh issue close with -c/--comment", "query=@file", "--input file",
                       "as a write", "graphql", "#fragment", "shell_commands"):
            self.assertIn(phrase, enforcement)
        self.assertIn("PR or issue feedback?", flow)
        for name in ("README.md", "DESIGN.md"):
            text = self.norm((ROOT / name).read_text())
            with self.subTest(doc=name):
                self.assertNotIn("any gh command that writes to a PR or issue", text)
                self.assertNotIn("write to a PR?", text)
                self.assertNotIn("not a full shell parser", text)


STATE_ATTRS = ("CONFIG_DIR", "CONFIG", "WATCH_STATE", "QUEUE_DIR", "OPENCLAW_EVENTS")
STATE_KEYS = ("PRBOT_CONFIG_DIR", "PRBOT_QUEUE_DIR", "PRBOT_OPENCLAW_EVENTS")
# discovers the tests the way the test command does, then checks the prbot it loaded; self-contained on purpose
PROBE = """
import sys, unittest
from pathlib import Path
loader = unittest.TestLoader()
loader.discover("tests", top_level_dir=".")
assert not loader.errors, loader.errors
p = sys.modules["tests.test_prbot"].prbot
forbidden = [(Path.home() / r).resolve() for r in (".config", ".local/state", ".openclaw")]
bad = [n for n in ("CONFIG_DIR", "CONFIG", "WATCH_STATE", "QUEUE_DIR", "OPENCLAW_EVENTS")
       for r in forbidden if Path(getattr(p, n)).resolve().is_relative_to(r)]
assert not bad, bad
"""


class StatePaths(unittest.TestCase):
    """tests/__init__.py keeps prbot away from the real config, queue and OpenClaw files."""

    sandbox = None

    def tearDown(self):
        if self.sandbox:
            shutil.rmtree(self.sandbox, ignore_errors=True)

    def probe(self, env):
        subprocess.run([sys.executable, "-B", "-c", PROBE], cwd=str(ROOT), env=env,
                       check=True, capture_output=True, text=True)

    def test_in_process(self):
        forbidden = [(Path.home() / r).resolve() for r in (".config", ".local/state", ".openclaw")]
        for n in STATE_ATTRS:
            path = Path(getattr(prbot, n)).resolve()
            for root in forbidden:
                self.assertFalse(path.is_relative_to(root), f"prbot.{n} resolves to {path}, under {root}")

    def test_three_env_cases(self):
        h = Path.home()
        u = {"PRBOT_CONFIG_DIR": str(h / ".config/prbot"),
             "PRBOT_QUEUE_DIR": str(h / ".local/state/github-events/prbot"),
             "PRBOT_OPENCLAW_EVENTS": str(h / ".openclaw/events.jsonl")}
        base = {x: y for x, y in os.environ.items() if x not in STATE_KEYS}
        envs = (base, {**base, **u}, {**base, "PRBOT_QUEUE_DIR": u["PRBOT_QUEUE_DIR"]})
        for env in envs:
            with self.subTest(set_keys=sorted(k for k in STATE_KEYS if k in env)):
                try:
                    self.probe(env)
                except subprocess.CalledProcessError as e:
                    self.fail(e.stderr)

    def test_symlinked_tmpdir_fallback(self):
        # OpenClaw can put TMPDIR under ~/.openclaw, and that root may itself be a symlink
        self.sandbox = Path(tempfile.mkdtemp(dir="/tmp"))
        home, real = self.sandbox / "home", self.sandbox / "real"
        home.mkdir()
        real.mkdir()
        (home / ".openclaw").symlink_to(real)
        env = {x: y for x, y in os.environ.items() if x not in STATE_KEYS}
        env.update(TMPDIR=str(home / ".openclaw"), HOME=str(home))
        try:
            self.probe(env)
        except subprocess.CalledProcessError as e:
            self.fail(e.stderr)


REGISTER_RUNNER = '''import json, sys, importlib.util
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("prbot_mod", sys.argv[1])
prbot_mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("prbot_mod", loader))
loader.exec_module(prbot_mod)
calls = []
def rec(name):
    def f(*a, **kw):
        calls.append(name)
        raise AssertionError(name + "-called")
    return f
prbot_mod.http.server.HTTPServer = rec("HTTPServer")
prbot_mod.api = rec("api")
prbot_mod.store_key = rec("store_key")
assert prbot_mod.http.server.BaseHTTPRequestHandler.__name__ == "BaseHTTPRequestHandler"
try:
    rc = prbot_mod.main(["register", "claude"])
finally:
    print(json.dumps({"calls": calls}))
sys.exit(rc)
'''


class ClassifyCli(unittest.TestCase):
    """A broken or unreadable config makes classify exit 4 (never write or read) and other commands exit 2."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.config = self.dir / "config.json"

    def run_prbot(self, *args, env=None):
        env = dict(os.environ, PRBOT_CONFIG_DIR=str(self.dir), **(env or {}))
        return subprocess.run([sys.executable, "-B", str(ROOT / "prbot"), *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def assert_undecided(self, r, content):
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn(str(self.config), r.stderr)
        self.assertIn("fix or remove it", r.stderr)
        self.assertNotIn(content, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("write", r.stdout)
        self.assertNotIn("read", r.stdout)

    def test_invalid_json_is_undecidable(self):
        """(a) Config `{bad`: classify exits 4, names the config path, and never prints write or read."""
        self.config.write_text("{bad")
        self.assert_undecided(self.run_prbot("classify", "--", "pr", "comment", "1", "-b", "x"), "{bad")

    def test_non_object_is_undecidable(self):
        """(b) Config `[]`: classify exits 4, names the config path, and never prints write or read."""
        self.config.write_text("[]")
        self.assert_undecided(self.run_prbot("classify", "--", "pr", "comment", "1", "-b", "x"), "[]")

    def test_missing_config_still_classifies(self):
        """(c) No config file: a comment classifies as write (exit 0) and a view as read (exit 1)."""
        r = self.run_prbot("classify", "--", "pr", "comment", "1", "-b", "x")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "write"), r.stderr)
        r = self.run_prbot("classify", "--", "pr", "view", "1")
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "read"), r.stderr)

    def test_codex_review_is_a_write(self):
        """issue #19: classify, which the Codex shim trusts, calls an @codex approval a write."""
        r = self.run_prbot("classify", "--", "pr", "review", "1", "--approve", "-b", "@codex looks good")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "write"), r.stderr)
        r = self.run_prbot("classify", "--", "pr", "comment", "1", "-b", "@codex review")
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "read"), r.stderr)

    def test_gh_alias_and_extension_bypasses_exit_4(self):
        """issue #20: classify exits 4 for every alias and extension bypass and never prints read; the argv after
        `--` is kept whole, so `classify -- gh --version` is a command named gh, not gh --version."""
        ghcfg = self.dir / "gh"
        ghcfg.mkdir()
        for name, args, config in ISSUE_20_BYPASSES:
            with self.subTest(name, args=args):
                (ghcfg / "config.yml").write_text(config or "")
                r = self.run_prbot("classify", "--", *args, env={"GH_CONFIG_DIR": str(ghcfg)})
                self.assertEqual(r.returncode, 4, r.stderr)
                self.assertEqual(r.stdout, "")
                self.assertIn("prbot classify: ", r.stderr)
                self.assertNotIn("Traceback", r.stderr)
        r = self.run_prbot("classify", "--", "alias", "set", "human-review", "pr review")
        self.assertIn("gh alias set", r.stderr)
        r = self.run_prbot("classify", "--", "human-review", "19")
        self.assertIn("not a built-in gh command", r.stderr)
        # without `--` a leading gh is still dropped, as before
        r = self.run_prbot("classify", "gh", "pr", "view", "1")
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "read"), r.stderr)

    def test_config_is_a_directory(self):
        """(d) config.json is a directory: classify exits 4 and reports IsADirectoryError."""
        self.config.mkdir()
        r = self.run_prbot("classify", "--", "pr", "comment", "1", "-b", "x")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("cannot decide", r.stderr)
        self.assertIn("IsADirectoryError", r.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode 000 file")
    def test_config_unreadable(self):
        """(e) config.json has mode 000: classify exits 4 and reports PermissionError."""
        self.config.write_text("{}")
        self.config.chmod(0o000)
        self.addCleanup(self.config.chmod, 0o600)
        r = self.run_prbot("classify", "--", "pr", "comment", "1", "-b", "x")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn("cannot decide", r.stderr)
        self.assertIn("PermissionError", r.stderr)

    def test_whoami_broken_config_exits_2(self):
        """(f) Config `{bad`: whoami exits 2 with a clear message and no traceback."""
        self.config.write_text("{bad")
        r = self.run_prbot("whoami", "claude")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("fix or remove it", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_review_broken_config_exits_2_before_gh(self):
        """(g) Config `{bad`: review --route-only exits 2 before it runs gh."""
        self.config.write_text("{bad")
        fakebin = self.dir / "fakebin"
        fakebin.mkdir()
        gh = fakebin / "gh"
        gh.write_text(f"#!/bin/sh\ntouch '{fakebin / 'gh.called'}'\nexit 0\n")
        gh.chmod(0o755)
        r = self.run_prbot("review", "1", "-R", "o/r", "--route-only",
                           env={"PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("fix or remove it", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((fakebin / "gh.called").exists())

    def assert_review_unreadable(self, error_name, reason):
        fakebin = self.dir / "fakebin"
        fakebin.mkdir()
        gh = fakebin / "gh"
        gh.write_text(f"#!/bin/sh\ntouch '{fakebin / 'gh.called'}'\nexit 0\n")
        gh.chmod(0o755)
        r = self.run_prbot("review", "12", "-R", "o/r", "--route-only",
                           env={"PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(r.stderr.strip(),
                         f"prbot: {self.config}: cannot read ({error_name}: {reason}); fix or remove it")
        self.assertNotIn("Errno", r.stderr)
        self.assertFalse((fakebin / "gh.called").exists())

    def test_review_config_is_a_directory_exits_2(self):
        """(i) config.json is a directory: review --route-only exits 2 with one line, not an IsADirectoryError traceback."""
        self.config.mkdir()
        self.assert_review_unreadable("IsADirectoryError", "the path is a directory, not a file")

    @unittest.skipIf(os.geteuid() == 0, "root can read a mode 000 file")
    def test_review_config_unreadable_exits_2(self):
        """(j) config.json has mode 000: review --route-only exits 2 with one line, not a PermissionError traceback."""
        self.config.write_text("{}")
        self.config.chmod(0o000)
        self.addCleanup(self.config.chmod, 0o600)
        self.assert_review_unreadable("PermissionError", "permission denied; check the file's mode and owner")

    def test_invalid_utf8_config_exits_2_and_classify_exits_4(self):
        """(k) config.json is not UTF-8: whoami exits 2 with one line and classify exits 4, never a traceback."""
        self.config.write_bytes(b'{"claude": "\xff\xfe"}')
        r = self.run_prbot("whoami", "claude")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stderr.strip(), f"prbot: {self.config}: not valid UTF-8 text; fix or remove it")
        r = self.run_prbot("classify", "--", "pr", "comment", "1", "-b", "x")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn(r.stdout.strip(), ("write", "read"))

    def test_untrustworthy_configs_exit_2_with_one_line(self):
        """(m) Huge integers, deep nesting, wrong field types and dangling symlinks: whoami exits 2 with one line
        and classify exits 4, never a traceback."""
        cases = {
            "deep nesting": ("[" * 100000 + "]" * 100000, "not valid JSON"),
            "integer owner": (json.dumps({"claude": {"owner": 17}}), '"claude".owner is not a non-empty string'),
            "empty owner": (json.dumps({"codex": {"owner": ""}}), '"codex".owner is not a non-empty string'),
            "missing owner": (json.dumps({"claude": {"slug": "claude-pr-gh"}}), '"claude".owner is not a non-empty string'),
            "bot entry not an object": (json.dumps({"codex": []}), '"codex" is not a JSON object'),
            "string flag": (json.dumps({"codex_triggers_as_bot": "false"}), "codex_triggers_as_bot is not true or false"),
            "dangling symlink": (None, "a symlink to a file that does not exist"),
        }
        cases["huge integer"] = ('{"x": ' + "9" * 5000 + "}", "not valid JSON")  # refused on every Python
        cases["huge integer under the 3.11 limit"] = ('{"x": ' + "9" * 2000 + "}", "not valid JSON")
        for name, (text, why) in cases.items():
            with self.subTest(name):
                if text is None:
                    self.config.symlink_to(self.dir / "missing.json")
                else:
                    self.config.write_text(text)
                r = self.run_prbot("whoami", "claude")
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(r.stderr.strip(), f"prbot: {self.config}: {why}; fix or remove it")
                r = self.run_prbot("classify", "--", "pr", "comment", "1", "-b", "x")
                self.assertEqual(r.returncode, 4, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.config.unlink()

    def test_dangling_config_dir_exits_2(self):
        """(m2) The config directory is a symlink to a missing directory: whoami exits 2, classify exits 4."""
        link = self.dir / "linked"
        link.symlink_to(self.dir / "missing")
        env = {"PRBOT_CONFIG_DIR": str(link)}
        env_run = dict(os.environ, **env)
        r = subprocess.run([sys.executable, "-B", str(ROOT / "prbot"), "whoami", "claude"], env=env_run,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("is a symlink to something that does not exist", r.stderr)
        r = subprocess.run([sys.executable, "-B", str(ROOT / "prbot"), "classify", "--", "pr", "comment", "1"],
                           env=env_run, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_symlink_loop_config_exits_2(self):
        """(n) config.json is a symlink to itself: whoami exits 2 with the fixed read-failure line."""
        self.config.symlink_to(self.config)
        r = self.run_prbot("whoami", "claude")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(r.stderr.strip(),
                         f"prbot: {self.config}: cannot read (OSError: the file could not be read); fix or remove it")

    def test_valid_config_still_loads(self):
        """(o) The shape register writes, plus extra bots and fields, still loads."""
        cfg = {"claude": {"app_id": 1, "slug": "claude-pr-gh", "client_id": "Iv1", "owner": "kennylhilljr", "user_id": 2},
               "claude-dev": {"app_id": 3, "owner": "kennylhilljr"}, "codex_triggers_as_bot": True}
        self.config.write_text(json.dumps(cfg))
        r = self.run_prbot("classify", "--", "pr", "view", "1")
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "read"), r.stderr)

    def test_unrelated_oserror_is_not_reported_as_a_config_problem(self):
        """(l) An OSError about another file is not turned into the config message."""
        self.assertIsNone(prbot.config_problem(FileNotFoundError(2, "gone", "/elsewhere/file")))
        self.assertIsNone(prbot.config_problem(RuntimeError("x")))
        self.assertIn("cannot read (IsADirectoryError: the path is a directory, not a file)",
                      prbot.config_problem(IsADirectoryError(21, "Is a directory", str(prbot.CONFIG))))

    def test_register_broken_config_exits_2_before_server_or_api(self):
        """(h) Config `{bad`: register exits 2 before it starts a server or calls api or store_key."""
        self.config.write_text("{bad")
        helper_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, helper_dir, ignore_errors=True)
        helper = helper_dir / "register_runner.py"
        helper.write_text(REGISTER_RUNNER)
        r = subprocess.run([sys.executable, "-B", str(helper), str(ROOT / "prbot")],
                           env=dict(os.environ, PRBOT_CONFIG_DIR=str(self.dir)),
                           timeout=10, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("fix or remove it", r.stderr)
        self.assertIn(str(self.config), r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1]), {"calls": []})


if __name__ == "__main__":
    unittest.main()
