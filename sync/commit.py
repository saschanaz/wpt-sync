import os
import re

import git
from mozautomation import commitparser

import log
from env import Environment
from pipeline import AbortError


env = Environment()
logger = log.get_logger(__name__)

METADATA_RE = re.compile("([^\:]*): (.*)")


class ShowError(Exception):
    pass


class ApplyError(Exception):
    pass


def get_metadata(text):
    data = {}
    for line in reversed(text.splitlines()):
        if line:
            m = METADATA_RE.match(line)
            if m:
                key, value = m.groups()
                data[key] = value
    return data


class GitNotes(object):
    def __init__(self, commit):
        self.commit = commit
        self._data = self._read()

    def _read(self):
        try:
            text = self.commit.repo.git.notes("show", self.commit.sha1)
        except git.GitCommandError:
            return {}
        data = get_metadata(text)

        return data

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def __setitem__(self, key, value):
        self._data[key] = value
        if key in self._data:
            data = "\n".join("%s: %s" % item for item in self._data.iteritems())
            self.commit.repo.git.notes("add", "-f", "-m", data, self.commit.sha1)
        else:
            self.commit.repo.git.notes("append", "-m", data, self.commit.sha1)


class Commit(object):
    def __init__(self, repo, commit):
        if isinstance(commit, Commit):
            _commit = commit.commit
        elif isinstance(commit, git.Commit):
            _commit = commit
        else:
            _commit = repo.commit(commit)
        self.repo = repo
        self.commit = _commit
        self.sha1 = self.commit.hexsha
        self._notes = None

    def __eq__(self, other):
        if hasattr(other, "sha1"):
            return self.sha1 == other.sha1
        elif hasattr(other, "hexsha"):
            return self.sha1 == other.hexsha
        else:
            return self.sha1 == other

    def __ne__(self, other):
        return not self == other

    @property
    def notes(self):
        if self._notes is None:
            self._notes = GitNotes(self)
        return self._notes

    @property
    def canonical_rev(self):
        if hasattr(self.repo, "cinnabar"):
            return self.repo.cinnabar.git2hg(self.sha1)
        return self.sha1

    @property
    def msg(self):
        return self.commit.message

    @property
    def author(self):
        return self.commit.author

    @property
    def metadata(self):
        return get_metadata(self.msg)

    @classmethod
    def create(cls, repo, msg, metadata, author=None, amend=False):
        msg = Commit.make_commit_msg(msg, metadata)
        commit_kwargs = {}
        if amend:
            commit_kwargs["amend"] = True
            commit_kwargs["no_edit"] = True
        else:
            if author is not None:
                commit_kwargs["author"] = author
        repo.git.commit(message=msg, **commit_kwargs)
        return cls(repo, repo.head.commit.hexsha)

    @staticmethod
    def make_commit_msg(msg, metadata):
        if metadata:
            metadata_str = "\n".join("%s: %s" % item for item in sorted(metadata.items()))
            msg = "%s\n%s" % (msg, metadata_str)
        return msg

    def is_empty(self, prefix=None):
        return self.repo.git.show(self.sha1, format="", patch=True).strip() == ""

    def tags(self):
        return [item for item in self.repo.git.tag(points_at=self.sha1).split("\n")
                if item.strip()]

    def move(self, dest_repo, skip_empty=True, msg_filter=None, metadata=None, src_prefix=None,
             dest_prefix=None, amend=False, three_way=True):
        if metadata is None:
            metadata = {}

        if msg_filter:
            msg, metadata_extra = msg_filter(self.msg)
        else:
            msg, metadata_extra = self.msg, None

        if metadata_extra:
            metadata.update(metadata_extra)

        msg = Commit.make_commit_msg(msg, metadata)

        with Store(dest_repo, self.canonical_rev + ".message", msg) as message_path:
            show_args = ()
            if src_prefix:
                show_args = ("--", src_prefix)
            try:
                patch = self.repo.git.show(self.sha1, binary=True, pretty="email",
                                           *show_args) + "\n"
            except git.GitCommandError as e:
                raise AbortError(e.message)

            if skip_empty and patch.endswith("\n\n\n"):
                return None

            strip_dirs = len(src_prefix.split("/")) + 1 if src_prefix else 1

            with Store(dest_repo, self.canonical_rev + ".diff", patch) as patch_path:

                # Without this tests were failing with "Index does not match"
                dest_repo.git.update_index(refresh=True)
                apply_kwargs = {}
                if dest_prefix:
                    apply_kwargs["directory"] = dest_prefix
                if three_way:
                    apply_kwargs["3way"] = True
                else:
                    apply_kwargs["reject"] = True
                try:
                    dest_repo.git.apply(patch_path, index=True, binary=True,
                                        p=strip_dirs, **apply_kwargs)
                except git.GitCommandError as e:
                    err_msg = """git apply failed
        %s returned status %s
        Patch saved as :%s
        Commit message saved as: %s
         %s""" % (e.command, e.status, patch_path, message_path, e.stderr)
                    raise AbortError(err_msg)

                return Commit.create(dest_repo, msg, None, amend=amend)


class GeckoCommit(Commit):
    @property
    def bug(self):
        bugs = commitparser.parse_bugs(self.commit.message.split("\n")[0])
        if len(bugs) > 1:
            logger.warning("Got multiple bugs for commit %s: %s" %
                           (self.canonical_rev, ", ".join(str(item) for item in bugs)))
        if not bugs:
            return None
        return str(bugs[0])

    def has_wpt_changes(self):
        prefix = env.config["gecko"]["path"]["wpt"]
        return not self.is_empty(prefix)

    @property
    def is_backout(self):
        return commitparser.is_backout(self.commit.message)

    def wpt_commits_backed_out(self):
        commits = []
        bugs = None
        if self.is_backout:
            nodes_bugs = commitparser.parse_backouts(self.commit.message)
            if nodes_bugs is None:
                # We think this a backout, but have no idea what it backs out
                # it's not clear how to handle that case so for now we pretend it isn't
                # a backout
                return commits, bugs

            nodes, bugs = nodes_bugs
            # Assuming that all commits are listed.

            # Add all backouts that affect wpt commits to the list
            for node in nodes:
                git_sha = self.repo.cinnabar.hg2git(node)
                commit = GeckoCommit(self.repo, git_sha)
                if commit.has_wpt_changes():
                    commits.append(git_sha)
        return commits, set(bugs)


class WptCommit(Commit):
    def pr(self):
        if "wpt_pr" not in self.notes:
            tags = [item.rsplit("_", 1)[1] for item in self.tags()
                    if item.startswith("merge_pr_")]
            if tags and len(tags) == 1:
                logger.info("Using tagged PR for commit %s" % self.sha1)
                pr = tags[0]
            else:
                pr = env.gh_wpt.pr_for_commit(self.sha1)
            if not pr:
                pr == ""
            logger.info("Setting PR to %s" % pr)
            self.notes["wpt_pr"] = pr
        pr = self.notes["wpt_pr"]
        try:
            int(pr)
            return pr
        except (TypeError, ValueError):
            return None


class Store(object):
    """Create a named file that is deleted if no exception is raised"""

    def __init__(self, repo, name, data):
        self.path = os.path.join(repo.working_dir, name)
        self.data = data

    def __enter__(self):
        with open(self.path, "w") as f:
            f.write(self.data.encode("utf8"))
        self.data = None
        return self.path

    def __exit__(self, type, value, traceback):
        if not type:
            os.unlink(self.path)
