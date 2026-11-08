import json
import shlex
from pathlib import Path

import pytest

from defsort._vendor.hookshell._classify import (
    _deny_reason,
    classify_command,
)
from defsort._vendor.hookshell._detector import (
    find_commit,
)
from defsort._vendor.hookshell._grammar import (
    CommitPlan,
    NoCommit,
    Rejection,
    classify_grammar,
)
from defsort._vendor.hookshell._shellparse import (
    parse_command,
)

_CORPUS = Path(__file__).parent / 'data' / 'commit_classification.json'
_POSSIBLE_PREFIX = 'defsort: possible commit text'
_PREFIX = 'tool: unsupported commit command'


class TestPossibleReason:
    def test_earlier_reparsed_body_match_precedes_later_token_match(self) -> None:
        text = 'cat <<\'EOF\'\n$GIT com""mit -m earlier\ngit commit -m later\nEOF\n'
        reason = classify_command(text, None, reason_prefix=_PREFIX).reason
        assert reason is not None
        assert 'heredoc body line 1' in reason
        assert 'heredoc body line 2' not in reason

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            (
                'cat > ls <<EOF\ngit commit -m x\nEOF\nls',
                (
                    "heredoc body line 1 'git commit -m x'; scanned because the heredoc "
                    "delimiter is unquoted; quote it (<<'EOF') if the body is data"
                ),
            ),
            (
                "cat > ls 3<<'EOF'\ngit commit -m x\nEOF\nls",
                (
                    "heredoc body line 1 'git commit -m x'; scanned because the heredoc has no "
                    'eligible file target; write the body to a file with a documented cat or '
                    'tee form if it is data'
                ),
            ),
            (
                'cat --bad > notes <<EOF\ngit commit -m x\nEOF\nwc notes',
                (
                    "heredoc body line 1 'git commit -m x'; scanned because the heredoc "
                    "delimiter is unquoted; quote it (<<'EOF') if the body is data"
                ),
            ),
            (
                (
                    "cat > notes/review.md <<'EOF' && npx prettier --write notes/review.md\n"
                    'See `docs/operations.md` (commit `e2e6dc8`).\nEOF\n'
                ),
                (
                    "heredoc body line 1 'See `docs/operations.md` (commit `e2e6dc8`).'; "
                    "scanned because 'npx prettier --write notes/review.md' is not a recognized "
                    'command; a quoted heredoc body skips scanning only when every command '
                    'in the chain is a documented form'
                ),
            ),
            (
                "cat > s.sh <<'EOF' && sh s.sh\n# script\ngit commit -m x\nEOF\n",
                (
                    "heredoc body line 2 'git commit -m x'; scanned because 'sh s.sh' can run "
                    'the written file; to commit, use the supported form: '
                    '[cd DIR &&] [git add ...;] git commit ...'
                ),
            ),
            (
                'cat > notes <<EOF\ngit\ncontext\nfixed in commit e2e6dc8\nEOF\n',
                (
                    "heredoc body line 3 (git on line 1) 'fixed in commit e2e6dc8'; "
                    'scanned because the heredoc delimiter is unquoted; '
                    "quote it (<<'EOF') if the body is data"
                ),
            ),
            (
                "cat > n.md <<'EOF'\ngit commit -m x\nEOF\nwc -l $TMPDIR/n.md",
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    "'$TMPDIR/n.md' has an unquoted variable; quote it, as in "
                    '"$TMPDIR/n.md", if the body is data'
                ),
            ),
            (
                "cat > n.md <<'EOF'\ngit commit -m x\nEOF\nwc -l ~/$DIR/n.md",
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    "'~/$DIR/n.md' has an unquoted variable; quote it, as in "
                    '~/"$DIR/n.md", if the body is data'
                ),
            ),
            (
                "cat > n.md <<'EOF'\ngit commit -m x\nEOF\nwc -l $TMPDIR/'n.md'",
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    "'$TMPDIR/'n.md'' has an unquoted variable; quote it, as in "
                    '"$TMPDIR/notes.md", if the body is data'
                ),
            ),
            (
                (
                    "cat > n.md <<'EOF'\ngit commit -m x\nEOF\n"
                    'wc -l $TMPDIR/abcdefghijklmnopqrstuvwxyz/n.md'
                ),
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    "'$TMPDIR/abcdefghijklmnopqrstuvwxyz/n.md' has an unquoted variable; "
                    'quote it, as in "$TMPDIR/notes.md", if the body is data'
                ),
            ),
            (
                'cat > n.md <<\'EOF\'\ngit commit -m x\nEOF\nwc -l "$tmp/n.md"',
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    '\'"$tmp/n.md"\' has unsupported arguments or expansions; '
                    'use literal text, ~/, or a quoted variable such as '
                    '"$TMPDIR/notes.md" if the body is data'
                ),
            ),
            (
                'cat > n.md <<\'EOF\'\ngit commit -m x\nEOF\nhead -n "$N" n.md',
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    '\'"$N"\' needs literal text in this position; '
                    'use literal text there if the body is data'
                ),
            ),
            (
                "cat > n.md <<'EOF'\ngit commit -m x\nEOF\nhead -n $N n.md",
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    "'$N' needs literal text in this position; "
                    'use literal text there if the body is data'
                ),
            ),
            (
                "cat > n.md <<'EOF'\ngit commit -m x\nEOF\nhead < $TMPDIR/n.md -n $N",
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    "'$N' needs literal text in this position; "
                    'use literal text there if the body is data'
                ),
            ),
            (
                "tee $TMPDIR/n.md <<'EOF'\ngit commit -m x\nEOF\nwc -l n.md",
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    "'$TMPDIR/n.md' has an unquoted variable; quote it, as in "
                    '"$TMPDIR/n.md", if the body is data'
                ),
            ),
            (
                (
                    "cat > n.md <<'EOF'\ngit commit -m x\nEOF\n"
                    'sort "--compress-program=$PWD/p" n.md'
                ),
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    '\'"--compress-program=$PWD/p"\' needs literal text in this position; '
                    'use literal text there if the body is data'
                ),
            ),
            (
                "cat > n.md <<'EOF'\ngit commit -m x\nEOF\nsort -k1 n.md",
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    "'sort -k1 n.md' has unsupported arguments or expansions; "
                    'use literal arguments in a documented file-writing form if the body is data'
                ),
            ),
            (
                'cat > "$OUT" <<\'EOF\'\ngit commit -m x\nEOF\nwc -l n.md',
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    'write target \'"$OUT"\' does not name a file; '
                    'name the file literally, as in "$TMPDIR/notes.md", if the body is data'
                ),
            ),
            (
                'cat > n.md <<\'EOF\'\ngit commit -m x\nEOF\ncp n.md "$DEST"',
                (
                    "heredoc body line 1 'git commit -m x'; scanned because "
                    'write target \'"$DEST"\' does not name a file; '
                    'add a trailing / if it is a directory, or name the file literally, '
                    'if the body is data'
                ),
            ),
        ],
    )
    def test_exact_contract(self, text: str, expected: str) -> None:
        result = classify_command(
            text, None, reason_prefix=_PREFIX, possible_reason_prefix=_POSSIBLE_PREFIX
        )
        assert result.reason == f'{_POSSIBLE_PREFIX}: {expected}'

    @pytest.mark.parametrize(
        ('text', 'cause', 'remedy'),
        [
            (
                "cat <<'EOF'\ngit commit\nEOF\n",
                'no eligible file target',
                'write the body to a file',
            ),
            (
                "cat > ls <<'EOF' && ls\ngit commit\nEOF\n",
                'can replace a command',
                'embedded commit text remains scanned',
            ),
            (
                'cat > x <<EOF && npx x\ngit commit\nEOF\n',
                'delimiter is unquoted',
                "quote it (<<'EOF')",
            ),
            (
                "cat > a <<A\ndata\nA\ncat > b <<'B'\ngit commit\nB\n",
                'another heredoc delimiter',
                'quote that delimiter',
            ),
            (
                "cat <<'A'\ndata\nA\ncat > b <<'B'\ngit commit\nB\n",
                'another heredoc write',
                'write that body to a file',
            ),
        ],
    )
    def test_cause_selects_the_remedy(self, text: str, cause: str, remedy: str) -> None:
        reason = classify_command(text, None, reason_prefix=_PREFIX).reason
        assert reason is not None
        assert reason.startswith('possible commit text: ')
        assert cause in reason
        assert remedy in reason
        assert 'separate' not in reason
        assert 'split' not in reason

    @pytest.mark.parametrize('text', ['echo git commit', 'git log commit'])
    def test_argument_words_are_possible_text(self, text: str) -> None:
        reason = classify_command(text, None, reason_prefix=_PREFIX).reason
        assert reason is not None
        assert reason.startswith('possible commit text: ')

    def test_excerpt_is_centered_clipped_then_escaped(self) -> None:
        line = 'before ' * 20 + 'fixed in commit e2e6dc8' + '\x00\x1f\x7f\x80\x9f' + ' after' * 20
        text = f'cat > notes <<EOF\ngit\ncontext\n{line}\nEOF\n'
        center = line.index('commit') + len('commit') // 2
        excerpt = line[center - 40 : center + 40]
        for control in ('\x00', '\x1f', '\x7f', '\x80', '\x9f'):
            excerpt = excerpt.replace(control, f'\\x{ord(control):02x}')
        reason = classify_command(
            text, None, reason_prefix=_PREFIX, possible_reason_prefix=_POSSIBLE_PREFIX
        ).reason
        assert reason == (
            f"{_POSSIBLE_PREFIX}: heredoc body line 3 (git on line 1) '...{excerpt}...'; "
            'scanned because the heredoc delimiter is unquoted; '
            "quote it (<<'EOF') if the body is data"
        )

    def test_neighbor_fragment_is_bounded_before_escaping(self) -> None:
        neighbor = 'custom \x1b\x80' + 'x' * 80
        text = f"cat > notes <<'EOF' && {neighbor}\ngit commit\nEOF\n"
        reason = classify_command(text, None, reason_prefix=_PREFIX).reason
        expected = neighbor[:40].replace('\x1b', '\\x1b').replace('\x80', '\\x80')
        assert reason is not None
        assert f"'{expected}' is not a recognized command" in reason
        assert '\x1b' not in reason
        assert '\x80' not in reason

    @pytest.mark.parametrize('first', [True, False])
    def test_outer_reason_wins_in_either_order(self, *, first: bool) -> None:
        body = "cat <<'EOF'\ngit commit\nEOF\n"
        text = 'git commit --interactive; ' + body if first else body + 'git commit --interactive'
        rejection = classify_grammar(parse_command(text), None)
        assert isinstance(rejection, Rejection)
        result = classify_command(
            text, None, reason_prefix=_PREFIX, possible_reason_prefix=_POSSIBLE_PREFIX
        )
        assert result.reason == _deny_reason(rejection, _PREFIX)

    def test_reparsed_cross_line_pair_names_the_earlier_git_line(self) -> None:
        text = 'cat <<\'EOF\'\ng""it \\\ncom""mit\nEOF\n'
        reason = classify_command(text, None, reason_prefix=_PREFIX).reason
        assert reason is not None
        assert 'heredoc body line 2 (git on line 1) \'com""mit\'' in reason

    def test_unknown_substitution_does_not_invent_a_token_or_line(self) -> None:
        text = "cat <<'EOF'\n" + 'echo ' + '$(echo ' * 9 + 'deep' + ')' * 9 + '\nEOF\n'
        reason = classify_command(text, None, reason_prefix=_PREFIX).reason
        assert reason is not None
        assert 'unknown content' in reason
        assert 'line ' not in reason
        assert 'depth limit' in reason


class TestReason:
    def test_all_control_ranges_are_escaped_in_the_reason(self) -> None:
        rejection = Rejection('a\x00\x1f\x7f\x80\x9fb', 'hint\x9f')
        assert _deny_reason(rejection, _PREFIX) == (
            f'{_PREFIX}: a\\x00\\x1f\\x7f\\x80\\x9fb; hint\\x9f'
        )

    def test_fragments_are_bounded_and_sanitized(self, corpus_root: Path) -> None:
        long_segment = 'make ' + 'x' * 80 + '\x1b[31m'
        classification = classify_command(
            f'{long_segment} && git commit -m x', str(corpus_root), reason_prefix=_PREFIX
        )
        assert classification.reason is not None
        element = classification.reason.removeprefix(f'{_PREFIX}: ').split(';')[0]
        assert element == f"segment '{long_segment[:40]}'"
        assert '\x1b' not in classification.reason
        assert _deny_reason(Rejection('x\x07', 'hint'), _PREFIX) == f'{_PREFIX}: x\\x07; hint'

    def test_precheck_reason_names_the_command_and_separate_call_remedy(
        self, corpus_root: Path
    ) -> None:
        classification = classify_command(
            'git diff --check && git add a.py && git commit -m x',
            str(corpus_root),
            reason_prefix=_PREFIX,
        )
        assert classification.outcome == 'unsupported'
        assert classification.reason == (
            f'{_PREFIX}: read-only git diff before git commit; '
            'run the check or observation in a separate tool call, then run the add/commit '
            'command only after success; for a check after staging, run add plus check '
            'without a real commit, then submit the commit separately'
        )

    def test_reason_names_the_element_and_the_accepted_form(self, corpus_root: Path) -> None:
        classification = classify_command(
            'git commit -m x && git push', str(corpus_root), reason_prefix=_PREFIX
        )
        assert classification.outcome == 'unsupported'
        assert classification.reason == (
            f"{_PREFIX}: segment 'git push'; supported form: "
            '[cd DIR &&] [git add ...;] git commit ...'
        )

    def test_reason_uses_the_caller_prefix(self, corpus_root: Path) -> None:
        classification = classify_command(
            'git commit -m x && git push',
            str(corpus_root),
            reason_prefix='acme: unsupported commit command',
        )
        assert classification.reason is not None
        assert classification.reason.startswith(
            "acme: unsupported commit command: segment 'git push'; "
        )

    def test_source_heredoc_with_many_shift_operators_is_unrelated(self) -> None:
        source = 'std::cout << "hello";\n' * 400
        result = classify_command(
            f"cat > t.cpp <<'EOF'\n{source}EOF\n", None, reason_prefix=_PREFIX
        )
        assert result.outcome == 'unrelated'
        assert result.reason is None

    def test_unknown_directory_reason_is_one_line_with_a_placeholder_route(self) -> None:
        classification = classify_command('git commit -m x', None, reason_prefix=_PREFIX)
        assert classification.outcome == 'unsupported'
        assert classification.reason == (
            f'{_PREFIX}: unknown working directory; supported form: '
            'git -C /absolute/repo commit ... or cd /absolute/repo && git commit ...'
        )

    def test_unrelated_and_supported_carry_no_reason(self, corpus_root: Path) -> None:
        assert classify_command('ls', str(corpus_root), reason_prefix=_PREFIX).reason is None
        assert (
            classify_command('git commit -m x', str(corpus_root), reason_prefix=_PREFIX).reason
            is None
        )


class TestTotality:
    @pytest.mark.parametrize(
        'template', ['git -C {route} commit -m x', 'cd {route} && git commit -m x']
    )
    @pytest.mark.parametrize('component', ['\0', 'a' * 256], ids=['invalid', 'overlong'])
    def test_unusable_absolute_route_never_produces_a_commit_plan(
        self, template: str, component: str, tmp_path: Path
    ) -> None:
        route = shlex.quote(tmp_path.as_posix() + '/' + component)
        result = classify_command(template.format(route=route), None, reason_prefix=_PREFIX)
        # Windows can report an overlong route as missing; that cannot create a commit.
        if isinstance(result.plan, NoCommit):
            assert result.outcome == 'supported'
            assert result.reason is None
            return
        assert result.outcome == 'unsupported'
        assert result.plan is None
        assert result.reason is not None
        assert 'git -C /absolute/repo' in result.reason
        assert 'cd /absolute/repo' in result.reason

    @pytest.mark.parametrize(
        'template', ['git -C {route} commit -m x', 'cd {route} && git commit -m x']
    )
    def test_route_lookup_error_is_unsupported(
        self, template: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail_stat(_path: Path) -> None:
            raise PermissionError('route lookup refused')

        route = shlex.quote(tmp_path.as_posix())
        monkeypatch.setattr(Path, 'stat', fail_stat)
        result = classify_command(template.format(route=route), None, reason_prefix=_PREFIX)
        assert result.outcome == 'unsupported'
        assert result.plan is None
        assert result.reason is not None
        assert 'unresolvable directory route' in result.reason

    @pytest.mark.parametrize(
        ('suffix', 'outcome'),
        [('echo x', 'unrelated'), ('true; git commit -m x', 'unsupported')],
    )
    def test_deep_eval_chain_returns_a_classification(self, suffix: str, outcome: str) -> None:
        result = classify_command('eval ' * 180 + suffix, None, reason_prefix=_PREFIX)
        assert result.outcome == outcome
        assert result.plan is None


def _entries() -> list[dict[str, object]]:
    data = json.loads(_CORPUS.read_text(encoding='utf-8'))
    return data['entries']


def _ids() -> list[str]:
    return [str(entry['id']) for entry in _entries()]


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    (tmp_path / 'repo').mkdir()
    (tmp_path / ';').mkdir()
    return tmp_path


class TestCorpus:
    @pytest.mark.parametrize('entry', _entries(), ids=_ids())
    def test_expected_outcome_and_detection(
        self, entry: dict[str, object], corpus_root: Path
    ) -> None:
        command = str(entry['command'])
        cwd = _cwd(entry, corpus_root)
        classification = classify_command(command, cwd, reason_prefix=_PREFIX)
        assert classification.outcome == entry['outcome']
        assert (find_commit(parse_command(command)) is not None) is entry['detected']
        if entry['outcome'] == 'supported':
            if entry.get('commits', True):
                assert isinstance(classification.plan, CommitPlan)
                assert entry['detected'] is True
            else:
                assert isinstance(classification.plan, NoCommit)
        else:
            assert classification.plan is None
        if 'element' in entry:
            result = classify_grammar(parse_command(command), cwd)
            assert isinstance(result, Rejection)
            assert result.element == entry['element']

    def test_ids_are_unique(self) -> None:
        ids = _ids()
        assert len(ids) == len(set(ids))

    def test_supported_inventory_covers_selection_modes(self, corpus_root: Path) -> None:
        covered: set[str] = set()
        for entry in _entries():
            if entry['outcome'] != 'supported':
                continue
            plan = classify_grammar(parse_command(str(entry['command'])), _cwd(entry, corpus_root))
            assert not isinstance(plan, Rejection)
            if isinstance(plan, NoCommit):
                continue
            for mode in (
                'auto_stage',
                'include',
                'only',
                'pathspec_from_file',
                'pathspec_file_nul',
            ):
                if getattr(plan.mode, mode):
                    covered.add(mode)
        assert covered == {
            'auto_stage',
            'include',
            'only',
            'pathspec_from_file',
            'pathspec_file_nul',
        }


def _cwd(entry: dict[str, object], corpus_root: Path) -> str | None:
    if 'command_cwd' in entry and entry['command_cwd'] is None:
        return None
    return str(corpus_root)


def test_commit_hint_not_called_for_other_hints() -> None:
    calls: list[None] = []

    def hint() -> str:
        calls.append(None)
        return 'unused'

    # A route rejection carries its own hint, not the commit-options hint.
    classify_command('/usr/bin/git commit -m x', '/repo', reason_prefix=_PREFIX, commit_hint=hint)
    assert calls == []


def test_commit_hint_replaces_commit_options_hint() -> None:
    result = classify_command(
        'git commit --interactive', '/repo', reason_prefix=_PREFIX, commit_hint=lambda: 'see X'
    )
    assert result.reason == f'{_PREFIX}: option --interactive; see X'


def test_commit_hint_text_is_escaped() -> None:
    result = classify_command(
        'git commit --interactive', '/repo', reason_prefix=_PREFIX, commit_hint=lambda: 'a\x07b'
    )
    assert result.reason is not None
    assert result.reason.endswith('; a\\x07b')


def test_default_commit_hint_names_no_path() -> None:
    result = classify_command('git commit --interactive', '/repo', reason_prefix=_PREFIX)
    assert result.reason == (
        f'{_PREFIX}: option --interactive; '
        'supported commit options are listed in the hook documentation'
    )


def test_raising_commit_hint_uses_default() -> None:
    def broken() -> str:
        raise RuntimeError('boom')

    result = classify_command(
        'git commit --interactive', '/repo', reason_prefix=_PREFIX, commit_hint=broken
    )
    assert result.reason is not None
    assert result.reason.endswith(
        '; supported commit options are listed in the hook documentation'
    )
