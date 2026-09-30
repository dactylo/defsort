from pathlib import Path

import pytest

from defsort._grammar import (
    _ELEMENT_RELATIVE_ROUTE,
    _ELEMENT_UNKNOWN_CWD,
    _HINT_ROUTE,
    CommitPlan,
    NoCommit,
    Rejection,
    classify_grammar,
)
from defsort._shellparse import parse_command
from tests._helpers import shell_path


def _grammar(text: str, cwd: Path | None) -> CommitPlan | NoCommit | Rejection:
    return classify_grammar(parse_command(text), None if cwd is None else str(cwd))


def _element(text: str, cwd: Path | None) -> str:
    result = _grammar(text, cwd)
    assert isinstance(result, Rejection), result
    return result.element


def _no_commit(text: str, cwd: Path | None) -> None:
    result = _grammar(text, cwd)
    assert isinstance(result, NoCommit), result


def _plan(text: str, cwd: Path | None) -> CommitPlan:
    result = _grammar(text, cwd)
    assert isinstance(result, CommitPlan), result
    return result


@pytest.fixture
def routes(tmp_path: Path) -> Path:
    (tmp_path / 'repo').mkdir()
    (tmp_path / ';').mkdir()
    return tmp_path


class TestEligibility:
    def test_argv_text_is_the_concatenation_of_literal_parts(self, routes: Path) -> None:
        plan = _plan('git add calc+fast.py "sp ace.py" \'#tag.py\' && git commit -m x', routes)
        assert plan.pre_commit_adds[0].tokens == (
            'git',
            'add',
            'calc+fast.py',
            'sp ace.py',
            '#tag.py',
        )

    def test_carriage_return_in_amend_date_is_rejected(self, routes: Path) -> None:
        assert _element('GIT_COMMITTER_DATE="x\ry" git commit --amend -m x', routes) == (
            'carriage return in \'GIT_COMMITTER_DATE="x\ry"\''
        )

    def test_carriage_return_in_route_is_rejected(self, routes: Path) -> None:
        assert _element('git -C "./re\rpo" commit -m x', routes) == (
            'carriage return in \'"./re\rpo"\''
        )

    def test_carriage_return_is_rejected(self, routes: Path) -> None:
        assert _element('git commit -m "x\ry"', routes) == 'carriage return in \'"x\ry"\''
        assert _element('git add "a\r.py" && git commit -m x', routes) == (
            'carriage return in \'"a\r.py"\''
        )

    @pytest.mark.parametrize(
        ('text', 'element'),
        [
            ('git commit -m x || true', "separator '||'"),
            ('git commit -m x | cat', "separator '|'"),
            ('git commit -m x &', "separator '&'"),
            ('git commit -m x > log', "operator '>'"),
            ('(git commit -m x)', "operator '('"),
            ('git commit -m x # done', 'comment'),
            ("sh <<'EOF'\ngit commit -m x\nEOF", "operator '<<'"),
            ('git commit -m "fix $BUG"', "expansion '$BUG'"),
            ('git add *.py && git commit -m x', "glob '*'"),
            ('git commit -m `date`', "substitution in '`date`'"),
            ('git add a.py ; ; git commit -m x', 'empty segment'),
        ],
    )
    def test_non_literal_elements_are_rejected(
        self, text: str, element: str, routes: Path
    ) -> None:
        assert _element(text, routes) == element

    def test_quoted_command_words_are_literal(self, routes: Path) -> None:
        _plan("'git' 'commit' -m x", routes)

    def test_quoted_separator_is_an_operand(self, routes: Path) -> None:
        assert _element("git add a.py ';' git commit -m x", routes) == "option '-m' on git add"

    def test_recovered_tree_is_rejected_with_its_first_reason(self, routes: Path) -> None:
        assert _element("git commit -m 'x", routes) == 'unbalanced quotes'

    @pytest.mark.parametrize('platform', ['win32', 'linux'])
    @pytest.mark.parametrize(
        'case',
        [
            ('/c', True),
            ('/Z/repo', True),
            ('/home/repo', False),
            ('/1', False),
            ('/c-file', False),
            ('C:/repo', False),
        ],
    )
    @pytest.mark.parametrize(
        'template',
        ['git commit -m "{}"', 'git -C "{}" commit -m x', 'git add "{}" && git commit -m x'],
    )
    def test_windows_msys_word_boundary_and_diagnostic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        routes: Path,
        case: tuple[str, bool],
        *,
        platform: str,
        template: str,
    ) -> None:
        word, msys_path = case
        monkeypatch.setattr('sys.platform', platform)
        result = _grammar(template.format(word), routes)
        if platform == 'win32' and msys_path:
            assert isinstance(result, Rejection)
            assert 'MSYS-style word' in result.element
            assert word in result.element
            assert 'Windows' in result.element
            assert 'C:/...' in result.hint
        else:
            # Acceptance of the grammar does not establish that a route exists.
            assert isinstance(result, (CommitPlan, NoCommit))


class TestHeredocMessage:
    IDIOM = 'git commit -m "$(cat <<\'EOF\'\nfeat: x\n\nbody\nEOF\n)"'

    def test_carriage_return_in_opaque_body_is_accepted(self, routes: Path) -> None:
        _plan(self.IDIOM.replace('feat: x', 'feat: x\ry'), routes)

    def test_double_quoted_idiom_is_accepted_once(self, routes: Path) -> None:
        _plan(self.IDIOM, routes)
        _plan(self.IDIOM.replace('-m', '--message'), routes)

    @pytest.mark.parametrize(
        'text',
        [
            'git commit -m "$(cat <<\'EOT\'\nx\nEOT\n)"',
            'git commit -m "$(cat <<EOF\nx\nEOF\n)"',
            'git commit -m "$(cat <<-\'EOF\'\nx\nEOF\n)"',
            'git commit -m "$(cat <<\'EOF\'\nx\nEOF\n)"suffix',
            'git commit --message="$(cat <<\'EOF\'\nx\nEOF\n)"',
            'git commit -m "$(tac <<\'EOF\'\nx\nEOF\n)"',
            'git commit --author "$(cat <<\'EOF\'\nx\nEOF\n)" -m x',
            'git add "$(cat <<\'EOF\'\na.py\nEOF\n)" && git commit -m x',
            'git commit -m x -- "$(cat <<\'EOF\'\na.py\nEOF\n)"',
        ],
    )
    def test_other_substitutions_are_rejected(self, text: str, routes: Path) -> None:
        assert _element(text, routes).startswith('substitution in ')

    def test_second_idiom_is_rejected(self, routes: Path) -> None:
        text = self.IDIOM + ' -m "$(cat <<\'EOF\'\ny\nEOF\n)"'
        assert _element(text, routes) == 'second heredoc message'

    def test_unquoted_idiom_is_rejected(self, routes: Path) -> None:
        text = "git commit -m $(cat <<'EOF'\nx --only b.py\nEOF\n)"
        assert _element(text, routes) == 'unquoted heredoc message'


class TestAmendPrefix:
    @pytest.mark.parametrize(
        'text',
        [
            'GIT_COMMITTER_DATE="$(git log -1 --format=%aI)" git commit --amend --no-edit',
            'GIT_COMMITTER_DATE=$(git log -1 --format=%aI) git commit --amend -m x',
            "GIT_COMMITTER_DATE='2026-06-09T07:58:28-07:00' git commit --amend -m x",
            'GIT_COMMITTER_DATE="2026-09-07T00:00:00Z" git commit -m x',
            'GIT_COMMITTER_DATE=2026-06-09 git commit --amend -m x',
        ],
    )
    def test_accepted_prefixes(self, text: str, routes: Path) -> None:
        _plan(text, routes)

    @pytest.mark.parametrize(
        ('text', 'element'),
        [
            ('GIT_AUTHOR_DATE=x git commit -m x', "assignment 'GIT_AUTHOR_DATE=x'"),
            (
                'GIT_COMMITTER_DATE="$(date)" git commit -m x',
                'assignment \'GIT_COMMITTER_DATE="$(date)"\'',
            ),
            (
                'GIT_COMMITTER_DATE="$(git log -1 --format=%aI)"x git commit -m x',
                "assignment 'GIT_COMMITTER_DATE=\"$(git log -1 --forma'",
            ),
            (
                '"GIT_COMMITTER_DATE=2026-09-07T00:00:00Z" git commit -m x',
                "segment 'GIT_COMMITTER_DATE=2026-09-07T00:00:00Z'",
            ),
            (
                'GIT_COMMITTER_DATE=x git add a.py && git commit -m x',
                "segment 'GIT_COMMITTER_DATE=x git add a.py'",
            ),
            (
                'GIT_COMMITTER_DATE=x git status && git commit -m x',
                "segment 'GIT_COMMITTER_DATE=x git status'",
            ),
            (
                'git commit -m x; GIT_COMMITTER_DATE=x git status',
                "assignment 'GIT_COMMITTER_DATE=x'",
            ),
        ],
    )
    def test_rejected_prefixes(self, text: str, element: str, routes: Path) -> None:
        assert _element(text, routes) == element


class TestSequence:
    def test_cd_route_resolves_and_replays_in_the_route(self, routes: Path) -> None:
        plan = _plan('cd ./repo && git add a.py && git commit -m x', routes)
        assert plan.cwd == str(routes / 'repo')
        assert plan.pre_commit_adds[0].exec_cwd == str(routes / 'repo')
        assert plan.pre_commit_adds[0].git_cwd == str(routes / 'repo')

    def test_dash_c_route_replays_in_the_payload_directory(self, routes: Path) -> None:
        plan = _plan('git -C ./repo add a.py && git -C ./repo commit -m x', routes)
        assert plan.cwd == str((routes / 'repo').resolve())
        assert plan.pre_commit_adds[0].tokens == ('git', '-C', './repo', 'add', 'a.py')
        assert plan.pre_commit_adds[0].exec_cwd == str(routes)

    def test_missing_cd_target_is_a_proven_no_commit(self, routes: Path) -> None:
        _no_commit('cd ./missing && git commit -m x', routes)

    @pytest.mark.parametrize(
        'text',
        [
            'git add a.py; git commit -m x',
            'git add a.py\ngit commit -m x',
            'git add a.py &&\ngit commit -m x',
            'git add a.py\n\n\ngit commit -m x',
            '\ngit commit -m x\n',
        ],
    )
    def test_separators(self, text: str, routes: Path) -> None:
        _plan(text, routes)

    def test_quoted_separator_route_is_a_directory_name(self, routes: Path) -> None:
        plan = _plan("git -C ';' commit -m x", routes)
        assert plan.cwd == str((routes / ';').resolve())

    @pytest.mark.parametrize(
        ('text', 'element'),
        [
            ('cd ./repo; git commit -m x', "separator ';' after cd"),
            ('cd ./repo\ngit commit -m x', 'line feed after cd'),
            ('cd ./repo', "separator 'end' after cd"),
            ('cd repo && git commit -m x', "cd target 'repo'"),
            ('cd ./repo ./other && git commit -m x', "segment 'cd ./repo ./other'"),
            ('cd ./repo && git -C ./repo commit -m x', 'cd combined with -C'),
            ('git -C ./repo add a.py && git commit -m x', '-C route differs between segments'),
            ('git add a.py; git add b.py; git commit -m x', "segment 'git add b.py'"),
            ('git commit -m x && git push', "segment 'git push'"),
            ('git commit -m x && git status', "final segment 'git status'"),
            ('git commit -m x; git commit -m y', "segment 'git commit -m x'"),
            ('make check && git commit -m x', "segment 'make check'"),
            ('git init && git add -A && git commit -m x', "segment 'git init'"),
            ('git --no-pager commit -m x', "segment 'git --no-pager commit -m x'"),
            ('git.exe commit -m x', "executable 'git.exe'"),
            ('env FOO=1 git commit -m x', "segment 'env FOO=1 git commit -m x'"),
            ('git -C commit -m x', "segment 'git -C commit -m x'"),
            ('git status', "final segment 'git status'"),
            ('', 'empty command'),
        ],
    )
    def test_shape_rejections(self, text: str, element: str, routes: Path) -> None:
        assert _element(text, routes) == element

    @pytest.mark.parametrize(
        ('text', 'element'),
        [
            ('/usr/bin/git commit -m x', "executable '/usr/bin/git'"),
            ('./git commit -m x', "executable './git'"),
            ('Git.Exe commit -m x', "executable 'Git.Exe'"),
            (
                '"C:/Program Files/Git/cmd/git.exe" commit -m x',
                "executable 'C:/Program Files/Git/cmd/git.exe'",
            ),
            ("'.\\git.exe' commit -m x", "executable '.\\git.exe'"),
        ],
    )
    def test_path_qualified_git_names_the_executable(
        self, text: str, element: str, tmp_path: Path
    ) -> None:
        result = classify_grammar(parse_command(text), str(tmp_path))
        assert isinstance(result, Rejection)
        assert result.element == element
        assert result.hint == 'use the bare executable name git; PATH resolves the installed git'


class TestReadOnly:
    @pytest.mark.parametrize(
        'segment',
        [
            'git status',
            'git status --short --branch --porcelain --porcelain=v1 --untracked-files=all',
            'git diff --cached --check --stat --name-only --name-status',
            'git log --oneline --stat --format=%H -3 -n 2',
            "git log -1 --format='%h %s'",
        ],
    )
    def test_allowed_options(self, segment: str, routes: Path) -> None:
        _no_commit(f'{segment} && git commit --dry-run -m x', routes)

    @pytest.mark.parametrize('check', ['status', 'diff --check', 'log --oneline'])
    @pytest.mark.parametrize('separator', [' && ', '; ', '\n'])
    @pytest.mark.parametrize('add_position', ['none', 'before', 'after'])
    @pytest.mark.parametrize('route', ['bare', 'cd', 'dash_c'])
    def test_committing_prechecks_are_rejected(
        self, check: str, separator: str, add_position: str, route: str, routes: Path
    ) -> None:
        git = 'git -C ./repo' if route == 'dash_c' else 'git'
        segments = [f'{git} {check}']
        if add_position != 'none':
            segments.insert(0 if add_position == 'before' else 1, f'{git} add a.py')
        segments.append(f'{git} commit -m x')
        command = separator.join(segments)
        if route == 'cd':
            command = f'cd ./repo && {command}'
        assert _element(command, routes) == f"segment '{git} {check}'"

    def test_precheck_text_in_a_message_is_not_a_segment(self, routes: Path) -> None:
        _plan("git commit -m 'git diff --check; git status && git log --oneline'", routes)

    @pytest.mark.parametrize(
        ('segment', 'element'),
        [
            ('git diff --no-index --output=g.py a b', "option '--no-index' on git diff"),
            ('git diff --output=g.py', "option '--output=g.py' on git diff"),
            ('git diff main', "argument 'main' on git diff"),
            ('git diff --cached --name-only --', "argument '--' on git diff"),
            ('git log --format %H', "option '--format' on git log"),
            ('git log -n x', "option '-n' on git log"),
            ('git status -s', "option '-s' on git status"),
            ('git status -l', "option '-l' on git status"),
            ('git status a.py', "argument 'a.py' on git status"),
            ('git -C ./other status', '-C route differs between segments'),
        ],
    )
    def test_rejected_options(self, segment: str, element: str, routes: Path) -> None:
        assert _element(f'{segment} && git commit --dry-run -m x', routes) == element


class TestGitAdd:
    @pytest.mark.parametrize(
        'segment',
        [
            'git add a.py',
            'git add -- -a.py',
            'git add -A',
            'git add --update -- a.py',
            'git add -u',
        ],
    )
    def test_accepted_forms(self, segment: str, routes: Path) -> None:
        assert _plan(f'{segment} && git commit -m x', routes).pre_commit_adds

    @pytest.mark.parametrize(
        ('segment', 'element'),
        [
            ('git add', 'git add without pathspec'),
            ('git add --', 'git add without pathspec'),
            ('git add -p a.py', "option '-p' on git add"),
            ('git add -A -p', "option '-p' on git add"),
            ('git add --dry-run a.py', "option '--dry-run' on git add"),
        ],
    )
    def test_rejected_forms(self, segment: str, element: str, routes: Path) -> None:
        assert _element(f'{segment} && git commit -m x', routes) == element


class TestCommitOptions:
    @pytest.mark.parametrize(
        'text',
        [
            'git commit -m x -m y -F f --file g --author a --date d -c HEAD -C HEAD',
            'git commit --reedit-message HEAD --reuse-message HEAD --fixup HEAD --squash HEAD',
            'git commit --trailer k=v --cleanup strip -t tpl --template tpl --unified 3',
            'git commit --inter-hunk-context 2 -m x --fixup=amend:HEAD',
            'git commit -m -x --author=-A',
            'git commit --amend -e --edit --no-edit --reset-author -s --signoff --no-signoff -n',
            'git commit --no-verify --verify --no-post-rewrite -q --quiet --no-quiet -v --verbose',
            'git commit --no-verbose --allow-empty --no-allow-empty --allow-empty-message',
            'git commit --no-allow-empty-message --status --no-status --branch --no-branch',
            'git commit --ahead-behind --no-ahead-behind -S -SKEY --gpg-sign --gpg-sign=KEY',
            'git commit --no-gpg-sign -u -uall --untracked-files --untracked-files=all',
            'git commit --no-untracked-files -m x',
            'git commit -sqm x',
            'git commit -p --no-patch -m x',
            'git commit --interactive --no-interactive -m x',
            'git commit -a -m x',
            'git commit --all --no-all -m x a.py',
            'git commit -i a.py -m x',
            'git commit -o a.py -m x',
            'git commit -m x -- a.py b.py',
            'git commit --pathspec-from-file list --pathspec-file-nul -m x',
            'git commit --pathspec-from-file=list -m x',
            'git commit -am x',
        ],
    )
    def test_accepted_options(self, text: str, routes: Path) -> None:
        _plan(text, routes)

    def test_mode_records_selection_and_pathspecs(self, routes: Path) -> None:
        mode = _plan('git commit -i -m x -- a.py b.py', routes).mode
        assert mode.include
        assert not mode.auto_stage
        assert mode.pathspecs == ('a.py', 'b.py')
        mode = _plan('git commit --pathspec-from-file list --pathspec-file-nul -m x', routes).mode
        assert mode.pathspec_from_file == 'list'
        assert mode.pathspec_file_nul

    @pytest.mark.parametrize(
        ('text', 'element'),
        [
            ('git commit --mess x', 'option --mess'),
            ('git commit --no-dry-run -m x', 'option --no-dry-run'),
            ('git commit --dry-run --no-dry -m x', 'option --no-dry'),
            ('git commit --short --no-short -m x', 'option --no-short'),
            ('git commit --porcelain --no-porcelain -m x', 'option --no-porcelain'),
            ('git commit --long --no-long -m x', 'option --no-long'),
            ('git commit --null --no-null -m x', 'option --no-null'),
            ('git commit --no-amend -m x', 'option --no-amend'),
            ('git commit --amend=x -m x', 'option --amend=x'),
            ('git commit -x', 'option -x'),
            ('git commit -p -m x', 'option -p'),
            ('git commit --patch -m x', 'option --patch'),
            ('git commit --interactive', 'option --interactive'),
            ('git commit -h', 'option -h'),
            ('git commit --help', 'option --help'),
            ('git commit -m', 'missing value for -m'),
            ('git commit --message', 'missing value for --message'),
            ('git commit --fixup=reword:HEAD -m x', 'option --fixup=reword:'),
            ('git commit --fixup reword:HEAD', 'option --fixup=reword:'),
            ('git commit --pathspec-from-file=- -m x', 'option --pathspec-from-file=-'),
            ('git commit --pathspec-from-file - -m x', 'option --pathspec-from-file=-'),
            (
                'git commit --pathspec-file-nul -m x',
                '--pathspec-file-nul without --pathspec-from-file',
            ),
            ('git commit -a -m x a.py', 'pathspec with --all'),
            ('git commit -a --pathspec-from-file=f -m x', 'pathspec with --all'),
            ('git commit --pathspec-from-file=f -m x a.py', '--pathspec-from-file with pathspecs'),
            ('git commit -i -o a.py -m x', '--include with --only'),
            ('git commit -i -m x', '--include without pathspec'),
            ('git commit -o -m x', '--only without pathspec'),
        ],
    )
    def test_rejected_options(self, text: str, element: str, routes: Path) -> None:
        assert _element(text, routes) == element

    @pytest.mark.parametrize(
        'text',
        [
            'git commit --dry-run',
            'git commit --short -m x',
            'git commit -z',
            'git commit --porcelain',
            'git commit --long',
        ],
    )
    def test_status_only_is_accepted_without_a_commit(self, text: str, routes: Path) -> None:
        _no_commit(text, routes)


class TestUnknownDirectory:
    def test_absolute_cd_route_resolves_without_a_base(self, routes: Path) -> None:
        target = routes / 'repo'
        plan = _plan(f'cd {shell_path(target)} && git add a.py && git commit -m x', None)
        assert plan.cwd == str(target)
        assert plan.pre_commit_adds[0].exec_cwd == str(target)
        assert plan.pre_commit_adds[0].git_cwd == str(target)

    def test_absolute_dash_c_route_replays_in_the_route(self, routes: Path) -> None:
        target = routes / 'repo'
        plan = _plan(
            f'git -C {shell_path(target)} add a.py && git -C {shell_path(target)} commit -m x',
            None,
        )
        assert plan.cwd == str(target.resolve())
        assert plan.pre_commit_adds[0].tokens == ('git', '-C', target.as_posix(), 'add', 'a.py')
        assert plan.pre_commit_adds[0].exec_cwd == str(target.resolve())

    def test_absolute_dash_c_with_known_directory_replays_there(self, routes: Path) -> None:
        target = routes / 'repo'
        plan = _plan(
            f'git -C {shell_path(target)} add a.py && git -C {shell_path(target)} commit -m x',
            routes,
        )
        assert plan.pre_commit_adds[0].exec_cwd == str(routes)

    def test_bare_commit_is_rejected_with_the_route_hint(self) -> None:
        result = _grammar('git commit -m x', None)
        assert result == Rejection(_ELEMENT_UNKNOWN_CWD, _HINT_ROUTE)

    def test_missing_absolute_target_is_a_no_commit(self, routes: Path) -> None:
        _no_commit(f'cd {shell_path(routes / "missing")} && git commit -m x', None)
        _no_commit(f'git -C {shell_path(routes / "missing")} commit -m x', None)

    @pytest.mark.parametrize(
        'text',
        [
            'cd ./repo && git commit -m x',
            'git -C ./repo commit -m x',
            'git -C ../repo add a.py && git -C ../repo commit -m x',
            'cd ./missing && git commit -m x',
        ],
    )
    def test_relative_route_is_rejected(self, text: str) -> None:
        assert _element(text, None) == _ELEMENT_RELATIVE_ROUTE

    def test_status_only_is_a_no_commit_before_directory_reasoning(self) -> None:
        _no_commit('git commit --dry-run', None)
        _no_commit('cd ./repo && git commit --dry-run', None)
