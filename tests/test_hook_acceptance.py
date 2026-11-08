import json
import os
import subprocess
from pathlib import Path

import pytest

from defsort import resolve_config, sort
from tests._helpers import HOSTS, decode_text, process_transcript, run_cli, run_setup, shell_path

_AMEND_COMMAND = (
    'GIT_COMMITTER_DATE="$(git log -1 --format=%aI)" git commit --amend'
    " -m 'docs: amended subject' -m 'Amended body.'"
)
_CONFORMING_BASELINE = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
_ADDED_ON_TOP = 'def charlie():\n    return 2\n\n\n' + _CONFORMING_BASELINE
_CORPUS = Path(__file__).parent / 'hookshell' / 'data' / 'commit_classification.json'
_DIRECT_COMMAND = "git commit -m 'probe: direct commit'"
_EVIDENCE = 'def bravo():\n    return 1\n\n\nRESULT = bravo()\n\n\ndef alpha():\n    return 2\n'
_EXPECTED_INCREMENTAL = _CONFORMING_BASELINE + '\n\ndef charlie():\n    return 2\n'
_HEREDOC_COMMAND = (
    "git add sample.py && git commit -m \"$(cat <<'EOF'\n"
    'feat: sample\n'
    '\n'
    'Body mentioning git commit forms.\n'
    'EOF\n'
    ')"'
)
_INCIDENT_FILES = (
    'src/example/_adapters/_common.py',
    'src/example/_containment/__init__.py',
    'src/example/_containment/_posix.py',
    'src/example/_containment/_uncontained.py',
    'src/example/_containment/_windows.py',
    'src/example/_containment/_windows_api.py',
    'src/example/_doctor/__init__.py',
    'src/example/_doctor/_types.py',
    'src/example/_runner.py',
)
_INCIDENT_COMMAND = (
    'git add ' + ' '.join(_INCIDENT_FILES) + '\n'
    ' git diff --cached --check\n'
    " git commit -m 'docs: correct reviewed docstring contracts' -m 'Apply the 15 accepted"
    ' docstring proposals for failure conditions,\n'
    'completion sentinels, process lifecycle, version fallback, and timeout\n'
    'semantics. Executable code is unchanged.\n'
    '\n'
    'Validation: exact proposal matching, non-docstring AST comparison, and\n'
    "make check passed. Native Windows tests remain platform-skipped.'"
)
_NEEDS_SORT = (
    'def bravo_{name}():\n    return alpha_{name}()\n\n\ndef alpha_{name}():\n    return 1\n'
)
_SELECTION_CASES = (
    ('auto-stage-short', 'auto'),
    ('auto-stage-long', 'auto'),
    ('auto-stage-cluster', 'auto'),
    ('only-short', 'only'),
    ('only-long', 'only'),
    ('only-dashdash', 'only'),
    ('include-long', 'include'),
    ('pathspec-file-separate', 'only'),
    ('pathspec-file-joined', 'only'),
    ('pathspec-file-nul-separate', 'only'),
    ('pathspec-file-nul-joined', 'only'),
    ('include-pathspec-file', 'include'),
    ('only-pathspec-file', 'only'),
    ('auto-stage-reset', 'normal'),
    ('only-reset', 'normal'),
    ('include-reset', 'normal'),
)
_SORTED = 'def alpha_{name}():\n    return 1\n\n\ndef bravo_{name}():\n    return alpha_{name}()\n'
_SRC = Path(__file__).resolve().parents[1] / 'src'


class _Harness:
    def __init__(self, repo: Path, distributions: Path) -> None:
        self.repo = repo
        self.env = dict(os.environ)
        self.env['PYTHONPATH'] = os.pathsep.join([str(_SRC.parent), str(_SRC), str(distributions)])
        self.env['GIT_AUTHOR_NAME'] = 'Test'
        self.env['GIT_AUTHOR_EMAIL'] = 'test@test.com'
        self.env['GIT_COMMITTER_NAME'] = 'Test'
        self.env['GIT_COMMITTER_EMAIL'] = 'test@test.com'

    def git(self, *args: str) -> str:
        return decode_text(run_setup(['git', *args], cwd=self.repo, env=self.env).stdout)

    def hook(
        self,
        command: str,
        *extra: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command_cwd = self.repo if cwd is None else cwd
        payload = {
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': command},
            'cwd': str(command_cwd),
        }
        merged = dict(self.env)
        merged.update(env or {})
        return run_cli(
            ['hook', '--agent', 'claude', *extra],
            stdin=json.dumps(payload),
            cwd=command_cwd,
            env=merged,
        )

    def assert_fixed_point(self, path: str, ref: str = 'HEAD') -> str:
        blob = self.git('show', f'{ref}:{path}')
        config = resolve_config(self.repo / path)
        assert sort(blob, config=config) == blob, path
        return blob

    def run_agent_command(self, command: str) -> None:
        run_setup(['sh', '-c', command], cwd=self.repo, env=self.env)

    def stage(self, path: str, source: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding='utf-8')
        self.git('add', path)


@pytest.mark.parametrize(
    ('case', 'command', 'reason'),
    [
        ('long-write', None, None),
        (
            'tmpdir-write',
            (
                'cat > "$TMPDIR/notes.md" <<\'EOF\'\nThen run git commit when ready.\nEOF\n'
                'wc -l "$TMPDIR/notes.md"'
            ),
            None,
        ),
        (
            'unquoted-variable',
            (
                "cat > notes.md <<'EOF'\nThen run git commit when ready.\nEOF\n"
                'wc -l $TMPDIR/notes.md'
            ),
            (
                "heredoc body line 1 'Then run git commit when ready.'; scanned because "
                "'$TMPDIR/notes.md' has an unquoted variable; quote it, as in "
                '"$TMPDIR/notes.md", if the body is data'
            ),
        ),
        (
            'unknown-neighbor',
            (
                "cat > notes/review.md <<'EOF'\n"
                'See `docs/operations.md` (commit `e2e6dc8`).\n'
                'EOF\nnpx prettier --write notes/review.md'
            ),
            (
                "heredoc body line 1 'See `docs/operations.md` (commit `e2e6dc8`).'; "
                "scanned because 'npx prettier --write notes/review.md' is not a recognized "
                'command; a quoted heredoc body skips scanning only when every command in the '
                'chain is a documented form'
            ),
        ),
        (
            'execution-consumer',
            "cat > s.sh <<'EOF' && sh s.sh\n# shell script\ngit commit -m x\nEOF",
            (
                "heredoc body line 2 'git commit -m x'; scanned because 'sh s.sh' can run "
                'the written file; to commit, use the supported form: '
                '[cd DIR &&] [git add ...;] git commit ...'
            ),
        ),
        (
            'unquoted-delimiter',
            'cat > notes <<EOF\ngit\ncontext\nfixed in commit e2e6dc8\nEOF',
            (
                "heredoc body line 3 (git on line 1) 'fixed in commit e2e6dc8'; "
                "scanned because the heredoc delimiter is unquoted; quote it (<<'EOF') "
                'if the body is data'
            ),
        ),
        (
            'makefile-consumer',
            "cat > Makefile <<'EOF'\nall:\n\tgit commit -m x\nEOF\nmake",
            (
                "heredoc body line 2 '\\x09git commit -m x'; scanned because 'make' can run "
                'the written file; to commit, use the supported form: '
                '[cd DIR &&] [git add ...;] git commit ...'
            ),
        ),
        ('outer-commit', 'git commit -m x && git push', None),
    ],
)
def test_chained_heredoc_hook_decision(
    harness: _Harness, case: str, command: str | None, reason: str | None
) -> None:
    if case == 'long-write':
        entries = json.loads(_CORPUS.read_text(encoding='utf-8'))['entries']
        command = next(
            entry['command'] for entry in entries if entry['id'] == 'data-sink-long-body'
        )
        assert command is not None
        assert command.count('ordinary prose') == 299
    assert command is not None
    result = harness.hook(command)
    assert result.returncode == 0, process_transcript(result)
    if case in {'long-write', 'tmpdir-write'}:
        assert (result.stdout, result.stderr) == ('', '')
        return
    decision = json.loads(result.stdout)['hookSpecificOutput']
    assert decision['permissionDecision'] == 'deny'
    rendered = decision['permissionDecisionReason']
    if case == 'outer-commit':
        assert rendered.startswith('defsort: unsupported commit command: ')
        assert 'git push' in rendered
    else:
        assert rendered.startswith('defsort: possible commit text: ')
        assert reason is not None
        assert rendered == 'defsort: possible commit text: ' + reason


class TestIncremental:
    def _enable_incremental(self, harness: _Harness) -> None:
        (harness.repo / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\n', encoding='utf-8'
        )
        harness.git('add', 'pyproject.toml')
        harness.git('commit', '-qm', 'config')

    def test_added_definition_moves_into_place(self, harness: _Harness) -> None:
        self._enable_incremental(harness)
        harness.stage('sample.py', _CONFORMING_BASELINE)
        harness.git('commit', '-qm', 'baseline')
        harness.stage('sample.py', _ADDED_ON_TOP)
        _assert_allow(harness.hook(_DIRECT_COMMAND), 1)
        harness.run_agent_command(_DIRECT_COMMAND)
        assert harness.git('show', 'HEAD:sample.py') == _EXPECTED_INCREMENTAL
        harness.assert_fixed_point('sample.py')

    def test_no_eligible_change_leaves_the_blob_byte_identical(self, harness: _Harness) -> None:
        self._enable_incremental(harness)
        harness.stage('sample.py', _CONFORMING_BASELINE)
        harness.git('commit', '-qm', 'baseline')
        edited = _CONFORMING_BASELINE.replace('return 1', 'return 11')
        harness.stage('sample.py', edited)
        _assert_allow(harness.hook(_DIRECT_COMMAND), None)
        assert harness.git('show', ':sample.py') == edited
        harness.run_agent_command(_DIRECT_COMMAND)
        assert harness.git('show', 'HEAD:sample.py') == edited


class TestMandatedForms:
    def test_absolute_route_touches_only_the_selected_repository(
        self, harness: _Harness, tmp_path: Path
    ) -> None:
        other_root = tmp_path / 'other'
        other_root.mkdir()
        other = _Harness(other_root, HOSTS)
        other.git('init', '-q')
        other.git('config', 'defsort.hook', 'true')
        other.stage('base.py', 'x = 1\n')
        other.git('commit', '-qm', 'init')
        other.stage('target.py', _NEEDS_SORT.format(name='target'))
        harness.stage('sample.py', _NEEDS_SORT.format(name='sample'))
        command = f"git -C {shell_path(other_root)} commit -m 'probe: cross repository'"
        _assert_allow(harness.hook(command), 1)
        assert harness.git('show', ':sample.py') == _NEEDS_SORT.format(name='sample')
        assert other.git('show', ':target.py') == _SORTED.format(name='target')
        harness.run_agent_command(command)
        assert other.assert_fixed_point('target.py') == _SORTED.format(name='target')
        assert harness.git('rev-list', '--count', 'HEAD') == '1\n'

    def test_direct_commit(self, harness: _Harness) -> None:
        harness.stage('sample.py', _NEEDS_SORT.format(name='sample'))
        _assert_allow(harness.hook(_DIRECT_COMMAND), 1)
        harness.run_agent_command(_DIRECT_COMMAND)
        assert harness.assert_fixed_point('sample.py') == _SORTED.format(name='sample')

    def test_heredoc_commit_with_add(self, harness: _Harness) -> None:
        (harness.repo / 'sample.py').write_text(
            _NEEDS_SORT.format(name='sample'), encoding='utf-8'
        )
        _assert_allow(harness.hook(_HEREDOC_COMMAND), 1)
        harness.run_agent_command(_HEREDOC_COMMAND)
        harness.assert_fixed_point('sample.py')
        assert harness.git('log', '-1', '--format=%B').startswith(
            'feat: sample\n\nBody mentioning'
        )

    def test_incident_command(self, harness: _Harness) -> None:
        for index, path in enumerate(_INCIDENT_FILES):
            (harness.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (harness.repo / path).write_text(
                _NEEDS_SORT.format(name=f'f{index}'), encoding='utf-8'
            )
        index_before = (harness.repo / '.git' / 'index').read_bytes()
        worktree_before = {path: (harness.repo / path).read_bytes() for path in _INCIDENT_FILES}
        result = harness.hook(_INCIDENT_COMMAND)
        assert result.returncode == 0, process_transcript(result)
        assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert (harness.repo / '.git' / 'index').read_bytes() == index_before
        assert {
            path: (harness.repo / path).read_bytes() for path in _INCIDENT_FILES
        } == worktree_before

        # The check completes in an earlier call, before the supported add/commit call.
        _assert_allow(harness.hook('git diff --check'), None)
        harness.run_agent_command('git diff --check')
        replacement = _INCIDENT_COMMAND.replace(' git diff --cached --check\n', '')
        _assert_allow(harness.hook(replacement), len(_INCIDENT_FILES))
        harness.run_agent_command(replacement)
        for path in _INCIDENT_FILES:
            harness.assert_fixed_point(path)
        assert (
            harness.git('log', '-1', '--format=%s')
            == 'docs: correct reviewed docstring contracts\n'
        )

    def test_mandated_amend(self, harness: _Harness) -> None:
        harness.stage('first.py', 'y = 2\n')
        harness.git('commit', '-qm', 'first')
        author_date = harness.git('log', '-1', '--format=%aI')
        harness.stage('sample.py', _NEEDS_SORT.format(name='sample'))
        _assert_allow(harness.hook(_AMEND_COMMAND), 1)
        harness.run_agent_command(_AMEND_COMMAND)
        harness.assert_fixed_point('sample.py')
        assert harness.git('log', '-1', '--format=%cI') == author_date
        assert harness.git('log', '-1', '--format=%s') == 'docs: amended subject\n'

    def test_partial_staging_keeps_the_unstaged_edit_out_of_the_commit(
        self, harness: _Harness
    ) -> None:
        harness.stage('sample.py', _NEEDS_SORT.format(name='sample'))
        (harness.repo / 'sample.py').write_text(
            _NEEDS_SORT.format(name='sample') + '\n# unstaged note\n', encoding='utf-8'
        )
        result = harness.hook(_DIRECT_COMMAND)
        note = (
            'defsort: did not update sample.py in the working tree: it has unstaged changes; '
            'the commit has the sorted version'
        )
        assert (result.returncode, result.stderr) == (0, note + '\n'), process_transcript(result)
        assert json.loads(result.stdout) == {
            'systemMessage': 'defsort: sorted 1 staged Python file for this commit\n' + note
        }
        assert harness.git('show', ':sample.py') == _SORTED.format(name='sample')
        harness.run_agent_command(_DIRECT_COMMAND)
        assert harness.assert_fixed_point('sample.py') == _SORTED.format(name='sample')
        assert '# unstaged note' not in harness.git('show', 'HEAD:sample.py')
        assert '# unstaged note' in (harness.repo / 'sample.py').read_text(encoding='utf-8')


def _assert_allow(result: subprocess.CompletedProcess[str], sorted_files: int | None) -> None:
    assert (result.returncode, result.stderr) == (0, ''), process_transcript(result)
    if sorted_files is None:
        assert result.stdout == ''
        return
    noun = 'file' if sorted_files == 1 else 'files'
    assert json.loads(result.stdout) == {
        'systemMessage': f'defsort: sorted {sorted_files} staged Python {noun} for this commit'
    }


class TestDenials:
    def test_infrastructure_failure_denies_under_fail_closed(self, harness: _Harness) -> None:
        harness.stage('sample.py', _NEEDS_SORT.format(name='sample'))
        result = harness.hook(_DIRECT_COMMAND, env={'GIT_OBJECT_DIRECTORY': '/nonexistent'})
        assert result.returncode == 0, process_transcript(result)
        decision = json.loads(result.stdout)['hookSpecificOutput']
        assert decision['permissionDecision'] == 'deny'
        assert decision['permissionDecisionReason'].startswith('defsort: failed to')

    def test_infrastructure_failure_warns_under_allow_on_failure(self, harness: _Harness) -> None:
        harness.stage('sample.py', _NEEDS_SORT.format(name='sample'))
        result = harness.hook(
            _DIRECT_COMMAND, '--allow-on-failure', env={'GIT_OBJECT_DIRECTORY': '/nonexistent'}
        )
        assert result.returncode == 0, process_transcript(result)
        assert result.stderr.startswith('WARNING: defsort: failed to')
        assert json.loads(result.stdout) == {'systemMessage': result.stderr.rstrip('\n')}

    @pytest.mark.parametrize('allow_on_failure', [False, True])
    def test_nonregular_pathspec_denial_preserves_index_and_worktree(
        self, harness: _Harness, *, allow_on_failure: bool
    ) -> None:
        harness.stage('sample.py', 'baseline = 1\n')
        harness.git('commit', '-qm', 'baseline')
        source = _NEEDS_SORT.format(name='sample').encode()
        (harness.repo / 'sample.py').write_bytes(source)
        index = (harness.repo / '.git' / 'index').read_bytes()
        result = harness.hook(
            'git add sample.py && git commit --pathspec-from-file=. -m x',
            *(['--allow-on-failure'] if allow_on_failure else []),
        )
        assert result.returncode == 0, process_transcript(result)
        assert result.stderr == 'defsort: pathspec resource must be a regular file\n'
        decision = json.loads(result.stdout)['hookSpecificOutput']
        assert decision['permissionDecision'] == 'deny'
        assert 'regular file' in decision['permissionDecisionReason']
        assert (harness.repo / '.git' / 'index').read_bytes() == index
        assert (harness.repo / 'sample.py').read_bytes() == source

    @pytest.mark.parametrize('enabled', [False, True])
    @pytest.mark.parametrize('allow_on_failure', [False, True])
    def test_precheck_denial_preserves_index_and_worktree(
        self, harness: _Harness, *, enabled: bool, allow_on_failure: bool
    ) -> None:
        harness.stage('sample.py', 'baseline = 1\n')
        harness.stage('separate.txt', 'baseline\n')
        harness.git('commit', '-qm', 'baseline')
        (harness.repo / 'sample.py').write_text(_NEEDS_SORT.format(name='sample'))
        harness.git('config', 'defsort.hook', str(enabled).lower())
        index = (harness.repo / '.git' / 'index').read_bytes()
        worktree = {
            path: (harness.repo / path).read_bytes() for path in ('sample.py', 'separate.txt')
        }
        result = harness.hook(
            'git diff --check && git add sample.py && git commit -m x',
            *(['--allow-on-failure'] if allow_on_failure else []),
        )
        assert (harness.repo / '.git' / 'index').read_bytes() == index
        assert {path: (harness.repo / path).read_bytes() for path in worktree} == worktree
        assert result.returncode == 0, process_transcript(result)
        decision = json.loads(result.stdout)['hookSpecificOutput']
        assert decision['permissionDecision'] == 'deny'
        assert 'unsupported commit command' in decision['permissionDecisionReason']

    @pytest.mark.parametrize(
        'command',
        [
            'git status',
            'git status -l',
            'git diff --check',
            'git log --oneline',
            'git diff --check && git add sample.py && git commit --dry-run',
        ],
    )
    def test_read_only_commands_pass_through_without_mutation(
        self, harness: _Harness, command: str
    ) -> None:
        harness.stage('sample.py', _NEEDS_SORT.format(name='sample'))
        index = (harness.repo / '.git' / 'index').read_bytes()
        source = (harness.repo / 'sample.py').read_bytes()
        result = harness.hook(command)
        assert (result.returncode, result.stdout, result.stderr) == (0, '', ''), (
            process_transcript(result)
        )
        assert (harness.repo / '.git' / 'index').read_bytes() == index
        assert (harness.repo / 'sample.py').read_bytes() == source

    def test_unsupported_commit_denies_and_changes_nothing(self, harness: _Harness) -> None:
        harness.stage('sample.py', _NEEDS_SORT.format(name='sample'))
        head = harness.git('rev-parse', 'HEAD')
        index = harness.git('diff', '--cached')
        result = harness.hook("git commit -m 'x' && git push")
        assert result.returncode == 0, process_transcript(result)
        decision = json.loads(result.stdout)['hookSpecificOutput']
        assert decision['permissionDecision'] == 'deny'
        assert 'unsupported commit command' in decision['permissionDecisionReason']
        assert result.stderr.startswith('defsort: unsupported commit command')
        assert harness.git('rev-parse', 'HEAD') == head
        assert harness.git('diff', '--cached') == index


def _prepare_selection(harness: _Harness, mode: str) -> dict[str, str]:
    baseline = {
        path: _SORTED.format(name=path[0]).replace('return 1', 'return 0')
        for path in ('a.py', 'b.py')
    }
    for path, source in baseline.items():
        harness.stage(path, source)
    harness.git('commit', '-qm', 'selection baseline')
    for path in baseline:
        (harness.repo / path).write_text(_NEEDS_SORT.format(name=path[0]), encoding='utf-8')
    harness.git('add', 'b.py')
    if mode == 'normal':
        harness.git('add', 'a.py')
    (harness.repo / 'paths.txt').write_text('a.py\n', encoding='utf-8')
    (harness.repo / 'paths.nul').write_bytes(b'a.py\0')
    return baseline


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    repo = tmp_path / 'repo'
    repo.mkdir()
    harness = _Harness(repo, HOSTS)
    harness.git('init', '-q')
    harness.git('config', 'defsort.hook', 'true')
    (repo / 'base.py').write_text('x = 1\n', encoding='utf-8')
    harness.git('add', 'base.py')
    harness.git('commit', '-qm', 'init')
    return harness


@pytest.fixture
def labelled(harness: _Harness, tmp_path: Path) -> _Harness:
    harness.env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM='1',
        GIT_ATTR_NOSYSTEM='1',
        XDG_CONFIG_HOME=str(tmp_path / 'xdg'),
    )
    for name in ('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'):
        harness.env.pop(name, None)
    harness.stage('drafts/.gitattributes', '* private\n')
    harness.stage('drafts/p.py', _SORTED.format(name='p'))
    harness.stage('src/pub.py', _SORTED.format(name='pub'))
    harness.git('commit', '-qm', 'labelled baseline')
    for path, name in (('drafts/p.py', 'p'), ('src/pub.py', 'pub')):
        (harness.repo / path).write_text(_NEEDS_SORT.format(name=name), encoding='utf-8')
    return harness


class TestPrivateLabels:
    @pytest.mark.parametrize(
        ('cwd_name', 'command', 'staged', 'private_staged'),
        [
            ('.', 'git commit -m labels', True, True),
            ('.', 'git commit -a -m labels', False, False),
            ('.', 'git commit -m labels -- src/pub.py drafts/p.py', False, False),
            ('.', 'git commit -i -m labels -- src/pub.py drafts/p.py', False, False),
            ('.', 'git commit -o -m labels -- src/pub.py drafts/p.py', False, False),
            ('.', 'git add src/pub.py drafts/p.py && git commit -m labels', False, True),
            ('src', 'git commit -m labels', True, True),
            ('src', 'git commit -a -m labels', False, False),
            ('src', 'git commit -m labels -- pub.py ../drafts/p.py', False, False),
            ('src', 'git commit -i -m labels -- pub.py ../drafts/p.py', False, False),
            ('src', 'git commit -o -m labels -- pub.py ../drafts/p.py', False, False),
            ('src', 'git add pub.py ../drafts/p.py && git commit -m labels', False, True),
        ],
    )
    def test_every_commit_mode_leaves_private_files_untouched(
        self,
        labelled: _Harness,
        cwd_name: str,
        command: str,
        *,
        staged: bool,
        private_staged: bool,
    ) -> None:
        if staged:
            labelled.git('add', 'src/pub.py', 'drafts/p.py')
        cwd = labelled.repo / cwd_name
        result = labelled.hook(command, cwd=cwd)
        assert result.returncode == 0, process_transcript(result)
        assert result.stderr == ''
        assert (labelled.repo / 'drafts/p.py').read_text(encoding='utf-8') == _NEEDS_SORT.format(
            name='p'
        )
        assert labelled.git('show', ':drafts/p.py') == (
            _NEEDS_SORT if private_staged else _SORTED
        ).format(name='p')
        if staged or private_staged:
            assert labelled.git('show', ':src/pub.py') == _SORTED.format(name='pub')
        else:
            assert (labelled.repo / 'src/pub.py').read_text(encoding='utf-8') == _SORTED.format(
                name='pub'
            )

    def test_invalid_name_warns_and_uses_private(self, labelled: _Harness) -> None:
        labelled.git('config', 'defsort.privateAttribute', 'a b')
        labelled.git('add', 'src/pub.py', 'drafts/p.py')
        result = labelled.hook('git commit -m labels')
        assert result.returncode == 0, process_transcript(result)
        assert result.stderr == (
            "warning: invalid git config defsort.privateAttribute 'a b': "
            "use letters, digits, '.', '_', or '-', not starting with '-' or 'builtin_'; "
            "using 'private'\n"
        )
        assert (
            result.stderr.rstrip('\n') in json.loads(result.stdout)['systemMessage'].splitlines()
        )
        assert labelled.git('show', ':drafts/p.py') == _NEEDS_SORT.format(name='p')
        assert (labelled.repo / 'drafts/p.py').read_text(encoding='utf-8') == _NEEDS_SORT.format(
            name='p'
        )
        assert labelled.git('show', ':src/pub.py') == _SORTED.format(name='pub')

    @pytest.mark.parametrize(
        ('command', 'staged'),
        [
            ('git commit -m labels', True),
            ('git commit -a -m labels', False),
        ],
    )
    def test_literal_pathspec_setting_does_not_change_selection(
        self, labelled: _Harness, command: str, *, staged: bool
    ) -> None:
        if staged:
            labelled.git('add', 'src/pub.py', 'drafts/p.py')
        result = labelled.hook(command, env={'GIT_LITERAL_PATHSPECS': '1'})
        assert result.returncode == 0, process_transcript(result)
        assert result.stderr == ''
        assert (labelled.repo / 'drafts/p.py').read_text(encoding='utf-8') == _NEEDS_SORT.format(
            name='p'
        )
        assert labelled.git('show', ':drafts/p.py') == (_NEEDS_SORT if staged else _SORTED).format(
            name='p'
        )
        if staged:
            assert labelled.git('show', ':src/pub.py') == _SORTED.format(name='pub')
        else:
            assert (labelled.repo / 'src/pub.py').read_text(encoding='utf-8') == _SORTED.format(
                name='pub'
            )

    def test_only_private_files_staged_is_allowed_silently(self, labelled: _Harness) -> None:
        labelled.git('add', 'drafts/p.py')
        _assert_allow(labelled.hook('git commit -m labels'), None)
        assert labelled.git('show', ':drafts/p.py') == _NEEDS_SORT.format(name='p')

    def test_preserved_script_in_a_labelled_folder_commits_unchanged(
        self, labelled: _Harness, tmp_path: Path
    ) -> None:
        attributes = tmp_path / 'xdg' / 'git' / 'attributes'
        attributes.parent.mkdir(parents=True)
        attributes.write_text('notes/** private\n', encoding='utf-8')
        labelled.stage('notes/reviews/evidence.py', _EVIDENCE)
        _assert_allow(labelled.hook('git commit -m evidence'), None)
        assert labelled.git('show', ':notes/reviews/evidence.py') == _EVIDENCE
        assert (labelled.repo / 'notes/reviews/evidence.py').read_text(
            encoding='utf-8'
        ) == _EVIDENCE


class TestSelectionForms:
    def test_add_update_pathspec_preserves_unstaged_worktree_outside_pathspec(
        self, harness: _Harness
    ) -> None:
        sub = harness.repo / 'sub'
        sub.mkdir()
        harness.stage('sub/in.py', _SORTED.format(name='in'))
        harness.stage('top.py', _SORTED.format(name='top'))
        harness.git('commit', '-qm', 'baseline')

        staged_top = _NEEDS_SORT.format(name='top')
        harness.stage('top.py', staged_top)
        worktree_top = (staged_top + '\n# unstaged edit\n').encode()
        (harness.repo / 'top.py').write_bytes(worktree_top)
        (sub / 'in.py').write_text(_NEEDS_SORT.format(name='in'), encoding='utf-8')

        result = harness.hook("git add -u sub/ && git commit -m 'pathspec'")
        note = (
            'defsort: did not update top.py in the working tree: it has unstaged changes; '
            'the commit has the sorted version'
        )
        assert (result.returncode, result.stderr) == (0, note + '\n'), process_transcript(result)
        assert json.loads(result.stdout) == {
            'systemMessage': 'defsort: sorted 2 staged Python files for this commit\n' + note
        }

        assert harness.git('show', ':top.py') == _SORTED.format(name='top')
        assert harness.git('show', ':sub/in.py') == _SORTED.format(name='in')
        assert (harness.repo / 'top.py').read_bytes() == worktree_top

    @pytest.mark.parametrize(('case_id', 'mode'), _SELECTION_CASES)
    def test_committed_content_matches_selection(
        self, harness: _Harness, case_id: str, mode: str
    ) -> None:
        entries = json.loads(_CORPUS.read_text(encoding='utf-8'))['entries']
        entry = next(entry for entry in entries if entry['id'] == case_id)
        assert entry['outcome'] == 'supported'
        command = entry['command']
        baseline = _prepare_selection(harness, mode)
        index_before = harness.git('diff', '--cached')
        staged_b_before = harness.git('show', ':b.py')

        _assert_allow(harness.hook(command), {'normal': 2, 'include': 1}.get(mode))
        if mode in {'only', 'auto'}:
            assert harness.git('diff', '--cached') == index_before
        if mode == 'normal':
            assert harness.git('show', ':a.py') == _SORTED.format(name='a')
        if mode in {'normal', 'include'}:
            assert harness.git('show', ':b.py') == _SORTED.format(name='b')
        if mode in {'only', 'include', 'auto'}:
            assert (harness.repo / 'a.py').read_text(encoding='utf-8') == _SORTED.format(name='a')

        harness.run_agent_command(command)
        selected = ('a.py',) if mode == 'only' else ('a.py', 'b.py')
        for path in selected:
            committed = harness.assert_fixed_point(path)
            assert committed == _SORTED.format(name=path[0])
            assert committed != baseline[path]
        if mode == 'only':
            assert harness.git('show', 'HEAD:b.py') == baseline['b.py']
            assert harness.git('show', ':b.py') == staged_b_before
