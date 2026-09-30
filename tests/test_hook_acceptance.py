import json
import os
import subprocess
from pathlib import Path

import pytest

from defsort import resolve_config, sort
from tests._helpers import _HOSTS, decode_text, process_transcript, run_cli, run_setup, shell_path

CORPUS = Path(__file__).parent / 'data' / 'commit_classification.json'
SRC = Path(__file__).resolve().parents[1] / 'src'
NEEDS_SORT = (
    'def bravo_{name}():\n    return alpha_{name}()\n\n\ndef alpha_{name}():\n    return 1\n'
)
SORTED = 'def alpha_{name}():\n    return 1\n\n\ndef bravo_{name}():\n    return alpha_{name}()\n'
CONFORMING_BASELINE = 'def alpha():\n    return 1\n\n\ndef bravo():\n    return alpha()\n'
ADDED_ON_TOP = 'def charlie():\n    return 2\n\n\n' + CONFORMING_BASELINE
EXPECTED_INCREMENTAL = CONFORMING_BASELINE + '\n\ndef charlie():\n    return 2\n'
INCIDENT_FILES = (
    'src/sequential_hooks/_adapters/_common.py',
    'src/sequential_hooks/_containment/__init__.py',
    'src/sequential_hooks/_containment/_posix.py',
    'src/sequential_hooks/_containment/_uncontained.py',
    'src/sequential_hooks/_containment/_windows.py',
    'src/sequential_hooks/_containment/_windows_api.py',
    'src/sequential_hooks/_doctor/__init__.py',
    'src/sequential_hooks/_doctor/_types.py',
    'src/sequential_hooks/_runner.py',
)
INCIDENT_COMMAND = (
    'git add ' + ' '.join(INCIDENT_FILES) + '\n'
    ' git diff --cached --check\n'
    " git commit -m 'docs: correct reviewed docstring contracts' -m 'Apply the 15 accepted"
    ' docstring proposals for failure conditions,\n'
    'completion sentinels, process lifecycle, version fallback, and timeout\n'
    'semantics. Executable code is unchanged.\n'
    '\n'
    'Validation: exact proposal matching, non-docstring AST comparison, and\n'
    "make check passed. Native Windows tests remain platform-skipped.'"
)
HEREDOC_COMMAND = (
    "git add sample.py && git commit -m \"$(cat <<'EOF'\n"
    'feat: sample\n'
    '\n'
    'Body mentioning git commit forms.\n'
    'EOF\n'
    ')"'
)
AMEND_COMMAND = (
    'GIT_COMMITTER_DATE="$(git log -1 --format=%aI)" git commit --amend'
    " -m 'docs: amended subject' -m 'Amended body.'"
)
DIRECT_COMMAND = "git commit -m 'probe: direct commit'"


SELECTION_CASES = (
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


class Harness:
    def __init__(self, repo: Path, distributions: Path) -> None:
        self.repo = repo
        self.env = dict(os.environ)
        self.env['PYTHONPATH'] = os.pathsep.join([str(SRC.parent), str(SRC), str(distributions)])
        self.env['GIT_AUTHOR_NAME'] = 'Test'
        self.env['GIT_AUTHOR_EMAIL'] = 'test@test.com'
        self.env['GIT_COMMITTER_NAME'] = 'Test'
        self.env['GIT_COMMITTER_EMAIL'] = 'test@test.com'

    def git(self, *args: str) -> str:
        return decode_text(run_setup(['git', *args], cwd=self.repo, env=self.env).stdout)

    def hook(
        self, command: str, *extra: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        payload = {
            'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash',
            'tool_input': {'command': command},
            'cwd': str(self.repo),
        }
        merged = dict(self.env)
        merged.update(env or {})
        return run_cli(
            ['hook', '--agent', 'claude', *extra],
            stdin=json.dumps(payload),
            cwd=self.repo,
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


class TestDenials:
    def test_infrastructure_failure_denies_under_fail_closed(self, harness: Harness) -> None:
        harness.stage('sample.py', NEEDS_SORT.format(name='sample'))
        result = harness.hook(DIRECT_COMMAND, env={'GIT_OBJECT_DIRECTORY': '/nonexistent'})
        assert result.returncode == 0, process_transcript(result)
        decision = json.loads(result.stdout)['hookSpecificOutput']
        assert decision['permissionDecision'] == 'deny'
        assert decision['permissionDecisionReason'].startswith('defsort: failed to')

    def test_infrastructure_failure_warns_under_allow_on_failure(self, harness: Harness) -> None:
        harness.stage('sample.py', NEEDS_SORT.format(name='sample'))
        result = harness.hook(
            DIRECT_COMMAND, '--allow-on-failure', env={'GIT_OBJECT_DIRECTORY': '/nonexistent'}
        )
        assert (result.returncode, result.stdout) == (0, ''), process_transcript(result)
        assert result.stderr.startswith('WARNING: defsort: failed to')

    @pytest.mark.parametrize('allow_on_failure', [False, True])
    def test_nonregular_pathspec_denial_preserves_index_and_worktree(
        self, harness: Harness, *, allow_on_failure: bool
    ) -> None:
        harness.stage('sample.py', 'baseline = 1\n')
        harness.git('commit', '-qm', 'baseline')
        source = NEEDS_SORT.format(name='sample').encode()
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

    @pytest.mark.parametrize('whitespace_path', ['sample.py', 'separate.txt'])
    @pytest.mark.parametrize('enabled', [False, True])
    @pytest.mark.parametrize('allow_on_failure', [False, True])
    def test_precheck_denial_preserves_index_and_worktree(
        self, harness: Harness, whitespace_path: str, *, enabled: bool, allow_on_failure: bool
    ) -> None:
        harness.stage('sample.py', 'baseline = 1\n')
        harness.stage('separate.txt', 'baseline\n')
        harness.git('commit', '-qm', 'baseline')
        (harness.repo / 'sample.py').write_text(NEEDS_SORT.format(name='sample'))
        with (harness.repo / whitespace_path).open('a') as stream:
            stream.write('# trailing whitespace  \n')
        harness.git('config', 'defsort.hook', str(enabled).lower())
        index = (harness.repo / '.git' / 'index').read_bytes()
        worktree = {
            path: (harness.repo / path).read_bytes() for path in ('sample.py', 'separate.txt')
        }
        check = run_setup(['git', 'diff', '--check'], cwd=harness.repo, check=False)
        assert check.returncode != 0, process_transcript(check)
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
        after = run_setup(['git', 'diff', '--check'], cwd=harness.repo, check=False)
        assert (after.returncode, after.stdout) == (check.returncode, check.stdout), (
            process_transcript(after)
        )

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
        self, harness: Harness, command: str
    ) -> None:
        harness.stage('sample.py', NEEDS_SORT.format(name='sample'))
        index = (harness.repo / '.git' / 'index').read_bytes()
        source = (harness.repo / 'sample.py').read_bytes()
        result = harness.hook(command)
        assert (result.returncode, result.stdout, result.stderr) == (0, '', ''), (
            process_transcript(result)
        )
        assert (harness.repo / '.git' / 'index').read_bytes() == index
        assert (harness.repo / 'sample.py').read_bytes() == source

    def test_unsupported_commit_denies_and_changes_nothing(self, harness: Harness) -> None:
        harness.stage('sample.py', NEEDS_SORT.format(name='sample'))
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


class TestIncremental:
    def _enable_incremental(self, harness: Harness) -> None:
        (harness.repo / 'pyproject.toml').write_text(
            '[tool.defsort]\nincremental = true\n', encoding='utf-8'
        )
        harness.git('add', 'pyproject.toml')
        harness.git('commit', '-qm', 'config')

    def test_added_definition_moves_into_place(self, harness: Harness) -> None:
        self._enable_incremental(harness)
        harness.stage('sample.py', CONFORMING_BASELINE)
        harness.git('commit', '-qm', 'baseline')
        harness.stage('sample.py', ADDED_ON_TOP)
        _assert_allow(harness.hook(DIRECT_COMMAND), 1)
        harness.run_agent_command(DIRECT_COMMAND)
        assert harness.git('show', 'HEAD:sample.py') == EXPECTED_INCREMENTAL
        harness.assert_fixed_point('sample.py')

    def test_no_eligible_change_leaves_the_blob_byte_identical(self, harness: Harness) -> None:
        self._enable_incremental(harness)
        harness.stage('sample.py', CONFORMING_BASELINE)
        harness.git('commit', '-qm', 'baseline')
        edited = CONFORMING_BASELINE.replace('return 1', 'return 11')
        harness.stage('sample.py', edited)
        _assert_allow(harness.hook(DIRECT_COMMAND), None)
        assert harness.git('show', ':sample.py') == edited
        harness.run_agent_command(DIRECT_COMMAND)
        assert harness.git('show', 'HEAD:sample.py') == edited


class TestMandatedForms:
    def test_absolute_route_touches_only_the_selected_repository(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        other_root = tmp_path / 'other'
        other_root.mkdir()
        other = Harness(other_root, _HOSTS)
        other.git('init', '-q')
        other.git('config', 'defsort.hook', 'true')
        other.stage('base.py', 'x = 1\n')
        other.git('commit', '-qm', 'init')
        other.stage('target.py', NEEDS_SORT.format(name='target'))
        harness.stage('sample.py', NEEDS_SORT.format(name='sample'))
        command = f"git -C {shell_path(other_root)} commit -m 'probe: cross repository'"
        _assert_allow(harness.hook(command), 1)
        assert harness.git('show', ':sample.py') == NEEDS_SORT.format(name='sample')
        assert other.git('show', ':target.py') == SORTED.format(name='target')
        harness.run_agent_command(command)
        assert other.assert_fixed_point('target.py') == SORTED.format(name='target')
        assert harness.git('rev-list', '--count', 'HEAD') == '1\n'

    def test_direct_commit(self, harness: Harness) -> None:
        harness.stage('sample.py', NEEDS_SORT.format(name='sample'))
        _assert_allow(harness.hook(DIRECT_COMMAND), 1)
        harness.run_agent_command(DIRECT_COMMAND)
        assert harness.assert_fixed_point('sample.py') == SORTED.format(name='sample')

    def test_heredoc_commit_with_add(self, harness: Harness) -> None:
        (harness.repo / 'sample.py').write_text(NEEDS_SORT.format(name='sample'), encoding='utf-8')
        _assert_allow(harness.hook(HEREDOC_COMMAND), 1)
        harness.run_agent_command(HEREDOC_COMMAND)
        harness.assert_fixed_point('sample.py')
        assert harness.git('log', '-1', '--format=%B').startswith(
            'feat: sample\n\nBody mentioning'
        )

    def test_incident_command(self, harness: Harness) -> None:
        for index, path in enumerate(INCIDENT_FILES):
            (harness.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (harness.repo / path).write_text(NEEDS_SORT.format(name=f'f{index}'), encoding='utf-8')
        index_before = (harness.repo / '.git' / 'index').read_bytes()
        worktree_before = {path: (harness.repo / path).read_bytes() for path in INCIDENT_FILES}
        result = harness.hook(INCIDENT_COMMAND)
        assert result.returncode == 0, process_transcript(result)
        assert json.loads(result.stdout)['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert (harness.repo / '.git' / 'index').read_bytes() == index_before
        assert {
            path: (harness.repo / path).read_bytes() for path in INCIDENT_FILES
        } == worktree_before

        # The check completes in an earlier call, before the supported add/commit call.
        _assert_allow(harness.hook('git diff --check'), None)
        harness.run_agent_command('git diff --check')
        replacement = INCIDENT_COMMAND.replace(' git diff --cached --check\n', '')
        _assert_allow(harness.hook(replacement), len(INCIDENT_FILES))
        harness.run_agent_command(replacement)
        for path in INCIDENT_FILES:
            harness.assert_fixed_point(path)
        assert (
            harness.git('log', '-1', '--format=%s')
            == 'docs: correct reviewed docstring contracts\n'
        )

    def test_mandated_amend(self, harness: Harness) -> None:
        harness.stage('first.py', 'y = 2\n')
        harness.git('commit', '-qm', 'first')
        author_date = harness.git('log', '-1', '--format=%aI')
        harness.stage('sample.py', NEEDS_SORT.format(name='sample'))
        _assert_allow(harness.hook(AMEND_COMMAND), 1)
        harness.run_agent_command(AMEND_COMMAND)
        harness.assert_fixed_point('sample.py')
        assert harness.git('log', '-1', '--format=%cI') == author_date
        assert harness.git('log', '-1', '--format=%s') == 'docs: amended subject\n'

    def test_partial_staging_keeps_the_unstaged_edit_out_of_the_commit(
        self, harness: Harness
    ) -> None:
        harness.stage('sample.py', NEEDS_SORT.format(name='sample'))
        (harness.repo / 'sample.py').write_text(
            NEEDS_SORT.format(name='sample') + '\n# unstaged note\n', encoding='utf-8'
        )
        _assert_allow(harness.hook(DIRECT_COMMAND), 1)
        assert harness.git('show', ':sample.py') == SORTED.format(name='sample')
        harness.run_agent_command(DIRECT_COMMAND)
        assert harness.assert_fixed_point('sample.py') == SORTED.format(name='sample')
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


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    repo = tmp_path / 'repo'
    repo.mkdir()
    harness = Harness(repo, _HOSTS)
    harness.git('init', '-q')
    harness.git('config', 'defsort.hook', 'true')
    (repo / 'base.py').write_text('x = 1\n', encoding='utf-8')
    harness.git('add', 'base.py')
    harness.git('commit', '-qm', 'init')
    return harness


class TestSelectionForms:
    @pytest.mark.parametrize(('case_id', 'mode'), SELECTION_CASES)
    def test_committed_content_matches_selection(
        self, harness: Harness, case_id: str, mode: str
    ) -> None:
        entries = json.loads(CORPUS.read_text(encoding='utf-8'))['entries']
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
            assert harness.git('show', ':a.py') == SORTED.format(name='a')
        if mode in {'normal', 'include'}:
            assert harness.git('show', ':b.py') == SORTED.format(name='b')
        if mode in {'only', 'include', 'auto'}:
            assert (harness.repo / 'a.py').read_text(encoding='utf-8') == SORTED.format(name='a')

        harness.run_agent_command(command)
        selected = ('a.py',) if mode == 'only' else ('a.py', 'b.py')
        for path in selected:
            committed = harness.assert_fixed_point(path)
            assert committed == SORTED.format(name=path[0])
            assert committed != baseline[path]
        if mode == 'only':
            assert harness.git('show', 'HEAD:b.py') == baseline['b.py']
            assert harness.git('show', ':b.py') == staged_b_before


def _prepare_selection(harness: Harness, mode: str) -> dict[str, str]:
    baseline = {
        path: SORTED.format(name=path[0]).replace('return 1', 'return 0')
        for path in ('a.py', 'b.py')
    }
    for path, source in baseline.items():
        harness.stage(path, source)
    harness.git('commit', '-qm', 'selection baseline')
    for path in baseline:
        (harness.repo / path).write_text(NEEDS_SORT.format(name=path[0]), encoding='utf-8')
    harness.git('add', 'b.py')
    if mode == 'normal':
        harness.git('add', 'a.py')
    (harness.repo / 'paths.txt').write_text('a.py\n', encoding='utf-8')
    (harness.repo / 'paths.nul').write_bytes(b'a.py\0')
    return baseline
