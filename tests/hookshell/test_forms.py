import pytest

from defsort._vendor.hookshell._detector import (
    find_commit,
)
from defsort._vendor.hookshell._forms import (
    analyze_data_writes,
)
from defsort._vendor.hookshell._shellparse import (
    Heredoc,
    parse_command,
)


@pytest.mark.parametrize(
    ('accepted', 'excluded'),
    [
        ('mkdir -p -m 755 folder', 'mkdir -m u+x folder'),
        ('cd -P folder', 'cd old new'),
        ('pwd -L', 'pwd extra'),
        ('true', 'true extra'),
        ('echo -n done', 'echo -e done'),
        (r"printf '%s\n%%' done", "printf -v name '%s' done"),
        ('ls -aAdFhlnprRSt1 notes', 'ls --color notes'),
        ('wc -clmw notes', 'wc --files0-from=notes'),
        ('head -n 2 notes', 'head -n -2 notes'),
        ('tail -c 2 notes', 'tail -f notes'),
        ('cat -benstuv notes', 'cat --show-all notes'),
        ('tee -ai output', 'tee --output-error=exit output'),
        ('touch -acm one two', 'touch -r notes output'),
        ('cp -fip notes copy', 'cp -fl notes shortcut'),
        ('mv -fi notes moved', 'mv --exchange notes moved'),
        ('rm -firR unused', 'rm --preserve-root unused'),
        ('chmod 0755 one two', 'chmod -R 755 one'),
        ('grep -EFivnqlc -e pattern notes', 'grep -f patterns notes'),
        ('diff -u notes copy', 'diff -l notes copy'),
        ('sort -nru notes', 'sort --compress-program=notes'),
        ('basename notes .md', 'basename -a notes'),
        ('dirname notes', 'dirname notes other'),
        ('realpath notes', 'realpath -e notes'),
    ],
)
def test_selected_forms_and_nearest_exclusions(accepted: str, excluded: str) -> None:
    assert not _detected_chain(accepted)
    assert _detected_chain(excluded)


@pytest.mark.parametrize(
    ('accepted', 'excluded'),
    [
        ('wc -l "$TMPDIR/notes"', 'wc -l $TMPDIR/notes'),
        ('wc -l "${TMPDIR}/notes"', 'wc -l "${TMPDIR:-/tmp}/notes"'),
        ('wc -l "$TMPDIR"/notes', 'wc -l "$TMPDIR"notes'),
        ('cd "$HOME"', 'cd "$tmp"'),
        ('cd ~', 'cd ~root'),
        ('ls ~/notes', 'ls ~+/notes'),
        ('ls ~/"$DIR/notes"', 'ls ~"/notes"'),
        ('wc -l "$PWD/notes"', 'wc -l "$_"'),
        ('wc -l "$OLDPWD/notes"', 'wc -l "$LINENO/notes"'),
        ('echo "Saved to $TMPDIR/notes"', 'echo "Saved to $TMPDIR."'),
        ('echo ~', 'wc -l "$TMPDIR[1]/notes"'),
        ('wc -l -- "$TMPDIR/notes"', 'wc -l "$TMPDIR:h/notes"'),
        ('wc -l "$TMPDIR/notes"', 'wc "$TMPDIR/notes" -l'),
        ('grep pattern "$TMPDIR/notes"', 'wc -l "$(pwd)/notes"'),
        ('chmod +x "$TMPDIR/notes"', 'wc -l *.md'),
        ('printf \'%s\\n\' "$TMPDIR"', 'wc -l "$A$B/notes"'),
        ('basename "$TMPDIR/notes" .md', 'wc -l "$x/notes"'),
        ('grep -e "=ls" notes', 'grep -e =ls notes'),
    ],
)
def test_home_and_variable_paths(accepted: str, excluded: str) -> None:
    assert not _detected_chain(accepted)
    assert _detected_chain(excluded)


@pytest.mark.parametrize(
    'name',
    [
        'ARGC',
        'BASHPID',
        'COLUMNS',
        'COMPREPLY',
        'COPROC',
        'DIRSTACK',
        'EPOCHREALTIME',
        'EPOCHSECONDS',
        'ERRNO',
        'FUNCNAME',
        'HISTCMD',
        'LINENO',
        'LINES',
        'MAPFILE',
        'MATCH',
        'MBEGIN',
        'MEND',
        'OPTARG',
        'OPTIND',
        'PIPESTATUS',
        'RANDOM',
        'REPLY',
        'SECONDS',
        'SRANDOM',
        'TTYIDLE',
        'BASH_COMMAND',
        'ZSH_ARGZERO',
        'COMP_LINE',
        'READLINE_LINE',
        'TRY_BLOCK_ERROR',
    ],
)
def test_shell_updated_names_are_excluded(name: str) -> None:
    assert _detected_chain(f'wc -l "${name}/notes"')


@pytest.mark.parametrize(
    'neighbor',
    [
        'chmod "$MODE" notes',
        'chmod $MODE notes',
        'printf "$FORMAT" x',
        'grep "$PATTERN" notes',
        'grep $PATTERN notes',
        'grep -- "$PATTERN" notes',
        'grep -e "$PATTERN" notes',
        'grep -e =ls notes',
        'grep =ls notes',
        'head -n "$N" notes',
        'head -n $N notes',
        'head -n "$n" notes',
        'head -n "${N:-5}" notes',
        'mkdir -m "$MODE" folder',
        'sort "--compress-program=$PWD/notes" notes',
        'sort "--compress-program=$PWD"/notes notes',
        'wc "-$FLAGS" notes',
        '"$TOOL" notes',
        '~/bin/tool notes',
        '=ls notes',
        'head < $TMPDIR/notes -n $N',
        'head > $TMPDIR/out -n $N notes',
        'head > "$OUT" -n $N',
        'head > "$TMPDIR/ls" -n $N; ls',
    ],
)
def test_literal_only_positions_reject_variables(neighbor: str) -> None:
    command = parse_command(f"cat > notes <<'EOF'\ngit commit -m x\nEOF\n{neighbor}")
    blockers = analyze_data_writes(command).blockers
    assert [blocker.kind for blocker in blockers] == ['literal-required']


@pytest.mark.parametrize(
    ('neighbor', 'kind'),
    [
        ('head -n $N notes 2>&1', 'structure'),
        ('$TOOL -n $N', 'literal-required'),
        ('python $X/s.py > "$OUT"', 'execution-consumer'),
        ('head < $TMPDIR/notes -n $N', 'literal-required'),
        ('head > "$OUT" -n $N', 'literal-required'),
        ('head > "$TMPDIR/ls" -n $N; ls', 'literal-required'),
        ('wc > "$OUT" $TMPDIR/notes', 'unquoted-variable'),
        ('wc -z notes > ls; ls', 'arguments'),
    ],
)
def test_each_command_reports_only_its_highest_ranked_defect(neighbor: str, kind: str) -> None:
    command = parse_command(f"cat > notes <<'EOF'\ngit commit -m x\nEOF\n{neighbor}")
    blockers = analyze_data_writes(command).blockers
    assert [blocker.kind for blocker in blockers if blocker.body is None] == [kind]


@pytest.mark.parametrize(
    ('accepted', 'excluded'),
    [
        ("grep '' notes", "grep '' ''"),
        ("grep -e '' notes", "grep -e '' ''"),
        ("basename notes ''", "basename '' .md"),
    ],
)
def test_empty_non_path_operands_preserve_nonempty_paths(accepted: str, excluded: str) -> None:
    assert not _detected_chain(accepted)
    assert _detected_chain(excluded)


@pytest.mark.parametrize('target', ["'=ls'", '"=ls"', r'\=ls', './=ls', '"="ls'])
@pytest.mark.parametrize('position', ['output', 'input', 'cp', 'mv', 'pattern'])
def test_equals_expansion_is_excluded_but_literal_spellings_qualify(
    target: str, position: str
) -> None:
    def command(word: str) -> str:
        sink = f'cat > {word}' if position == 'output' else 'cat > notes'
        if position == 'input':
            sink += f' < {word}'
        neighbor = (
            f'{position} notes {word}; ls'
            if position in {'cp', 'mv'}
            else f'grep -e {word} notes; ls'
            if position == 'pattern'
            else 'ls'
        )
        return f"{sink} <<'EOF'\ngit commit -m x\nEOF\n{neighbor}"

    assert find_commit(parse_command(command(target))) is None
    assert find_commit(parse_command(command('=ls'))) is not None


def test_deep_grouping_uses_no_python_recursion() -> None:
    text = '(' * 1500 + "cat > notes <<'EOF'\ngit commit -m x\nEOF\n" + ')' * 1500
    assert find_commit(parse_command(text)) is None


@pytest.mark.parametrize(
    'neighbor',
    [
        'cp -P notes out',
        'cp -R notes out',
        'cp -a notes out',
        'cp -d notes out',
        'cp --preserve=links notes out',
        'cp -fps notes out',
        'cp --link notes out',
        'cp --symbolic-link notes out',
        'ln notes out',
        'printf %n x',
        "printf '%b' x",
        r"printf '\t' x",
        'test -v x',
        'cd -',
        'cd +1',
        'cd -2',
        'echo -- done',
        'grep -e one -e two notes',
        'head -n2 notes',
        'mkdir -m755 folder',
        'wc notes -l',
        'touch -- -',
        'cat > .',
        'cat > /',
        'cat > ..',
        'cat > ""',
        'cat 2>&1',
        'cat &> out',
        'cat >| out',
        'cat <<< text',
        'cat > >(true)',
        'echo $x',
        'echo ${x@P}',
        'echo ${(e)x}',
        'echo a{b,c}',
        'echo *',
        'echo $((1))',
        'echo $(true)',
        'cat <(true)',
        'A=x true',
        '/bin/true',
        '"true"',
        'for x in a; do true; done',
    ],
)
def test_unlisted_forms_never_exempt(neighbor: str) -> None:
    assert _detected_chain(neighbor)


def test_earlier_command_and_sink_names_are_guarded() -> None:
    assert (
        find_commit(parse_command("wc notes; cat > DIR/WC <<'EOF'\ngit commit -m x\nEOF\n"))
        is not None
    )
    assert _detected_chain('true', target='DIR/cat')


def test_eligible_neighbors_still_scan_their_own_words() -> None:
    assert _detected_chain('echo git commit')


@pytest.mark.parametrize(
    'neighbor',
    [
        'wc -- -notes',
        'cat < notes',
        'cat 2> errors',
        'mkdir -- -dir',
        'cp -- notes copy/',
        'mv -- one two destination/',
        'grep -e -pattern notes',
        'grep pattern notes',
        'chmod +x output',
        'chmod u+x output',
        'chmod a+x output',
        'chmod 644 output',
        'echo "${x}"',
        'echo done',
    ],
)
def test_literal_options_and_operands(neighbor: str) -> None:
    assert _detected_chain(neighbor) is ('${x}' in neighbor)


@pytest.mark.parametrize(
    'neighbor',
    [
        'echo done > DIR/wc; wc -l notes',
        'echo done 2> DIR/wc; wc -l notes',
        'touch other DIR/wc; wc -l notes',
        'chmod 644 other DIR/wc; wc -l notes',
        'cp notes DIR/wc; wc -l notes',
        'cp DIR/wc destination/; wc -l notes',
        'mv DIR/wc destination/; wc -l notes',
        'mv stage bin; ls',
        'rm stage/ls; ls',
        'chmod +x stage/ls; ls',
        'chmod 0644 stage/ls; ls',
        'mv stage bin; echo done > cp',
        'echo done > rm; mv stage bin',
        'echo done > cat',
        'echo done > tee; tee out',
        "echo done > 'DIR\\WC'; wc -l notes",
    ],
)
def test_all_writes_compare_with_all_command_names(neighbor: str) -> None:
    assert _detected_chain(neighbor, target='stage/ls')


def test_exemptions_retain_distinct_original_body_identities() -> None:
    write = "cat > notes <<'EOF'\ngit commit -m x\nEOF\n"
    command = parse_command(write + write)
    bodies = [
        item
        for segment in command.segments
        for item in segment.elements
        if isinstance(item, Heredoc)
    ]
    analysis = analyze_data_writes(command)
    assert not analysis.blockers
    assert bodies[0] == bodies[1]
    assert analysis.exempt_bodies[0] is bodies[0]
    assert analysis.exempt_bodies[1] is bodies[1]


@pytest.mark.parametrize(
    'neighbor',
    [
        'cp notes "$TMPDIR/"',
        'mv notes "$TMPDIR/"',
        'cp notes ./',
        'mv notes ../',
        'cp notes "$TMPDIR/copy"',
        'tee "$TMPDIR/out" < notes',
        # A directory destination adds only source names, even when its own name is a command.
        'cp notes "$TMPDIR/ls/"; ls',
        'mv notes "$TMPDIR"/ls/; ls',
        'cp notes ls/; ls',
    ],
)
def test_variable_and_directory_destinations_qualify(neighbor: str) -> None:
    assert not _detected_chain(neighbor)


@pytest.mark.parametrize(
    ('target', 'neighbor', 'kind'),
    [
        ('"$HOME/bin/ls"', 'ls', 'write-target-collision'),
        ('"$TMPDIR/stage/ls"', 'mv "$TMPDIR/stage" "$BIN/"; ls', 'write-target-collision'),
        ('notes', 'cp "$TMPDIR/ls" "$BIN/"; ls', 'write-target-collision'),
        ('"$OUT"', 'wc notes', 'unknown-target'),
        ('~', 'wc notes', 'unknown-target'),
        ('notes', 'touch "$TMPDIR"', 'unknown-target'),
        ('notes', 'cp notes "$DEST"', 'unknown-destination'),
        ('notes', 'cp notes .', 'unknown-destination'),
        ('.', 'wc notes', 'unknown-target'),
        # The first failing target in source order wins: the source, before the destination.
        ('notes', 'cp ls "$DEST"; ls', 'write-target-collision'),
    ],
)
def test_write_targets_need_literal_names(target: str, neighbor: str, kind: str) -> None:
    command = parse_command(f"cat > {target} <<'EOF'\ngit commit -m x\nEOF\n{neighbor}")
    analysis = analyze_data_writes(command)
    assert not analysis.exempt_bodies
    assert [blocker.kind for blocker in analysis.blockers if blocker.body is None] == [kind]


@pytest.mark.parametrize('connector', ['&&', '||', ';', '\n'])
def test_connectors_and_comments(connector: str) -> None:
    text = (
        f"mkdir -p notes {connector} cat > notes/a <<'EOF'\n"
        'git commit -m x\nEOF\n# comment\nwc -l notes/a'
    )
    assert find_commit(parse_command(text)) is None


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ('(', ')'),
        ('{', '; }'),
        ('( { (', '); } )'),
        ('if true; then', '; fi'),
        ('if true; then true; elif true; then', '; else true; fi'),
        ('while true; do', '; done'),
        ('until true; do', '; done'),
    ],
)
def test_balanced_compounds(before: str, after: str) -> None:
    text = f"{before} cat > notes <<'EOF'\ngit commit -m x\nEOF\nwc -l notes {after}"
    assert find_commit(parse_command(text)) is None


def test_local_defect_also_explains_why_other_bodies_scan() -> None:
    command = parse_command(
        "cat > one <<ONE\ndata\nONE\ncat > two <<'TWO'\ngit commit -m x\nTWO\n"
    )
    blockers = analyze_data_writes(command).blockers
    local = next(item for item in blockers if item.kind == 'quoting' and item.body is not None)
    shared = next(item for item in blockers if item.kind == 'quoting' and item.body is None)
    assert local.span is shared.span


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ('(', '; }'),
        ('{', ')'),
        ('{', '}'),
        ('if true; "then"', '; fi'),
        ('if true; then', '; done'),
        ('while true; do', '; fi'),
        ('f() {', '; }'),
        ('function f {', '; }'),
        ('(', ') > out'),
        ('if true; then', '; fi > out'),
        ('true |', ''),
        ('true &', ''),
        ('true && ;', ''),
        ('if; then', '; fi'),
        ('while; do', '; done'),
    ],
)
def test_malformed_or_unrecognized_structure_is_scanned(before: str, after: str) -> None:
    text = f"{before} cat > notes <<'EOF'\ngit commit -m x\nEOF\nwc -l notes {after}"
    assert find_commit(parse_command(text)) is not None


def test_multiple_bodies_require_every_body_quoted() -> None:
    text = "cat > notes <<'ONE' <<'TWO'\ngit commit -m x\nONE\ngit commit -m x\nTWO\nwc notes"
    assert find_commit(parse_command(text)) is None
    assert find_commit(parse_command(text.replace("<<'TWO'", '<<TWO'))) is not None


@pytest.mark.parametrize(
    ('redirection', 'detected'),
    [
        ('0<<', False),
        ('3<<', True),
        ('<<', False),
        ('0 <<', False),
        ('2 > err <<', False),
        ('2> err <<', False),
    ],
)
def test_heredoc_descriptor_adjacency(redirection: str, *, detected: bool) -> None:
    text = f"true; cat {redirection}'EOF' > notes\ngit commit -m x\nEOF\nwc -l notes"
    assert (find_commit(parse_command(text)) is not None) is detected


def test_quoted_reserved_word_is_an_argument_inside_valid_structure() -> None:
    text = "if true; then echo 'then'; cat > notes <<'EOF'\ngit commit -m x\nEOF\nfi"
    assert find_commit(parse_command(text)) is None


@pytest.mark.parametrize(
    ('output', 'detected'),
    [
        ('2> errors', True),
        ('3>> errors', True),
        ('> notes 2> errors', False),
        ('1> notes 2> errors', False),
        ('01>> notes 2> errors', False),
        ('2 > notes', False),
        ('> notes 2> wc', True),
    ],
)
def test_cat_sink_requires_stdout_but_guards_all_output_files(
    output: str, *, detected: bool
) -> None:
    text = f"true; cat {output} <<'EOF'\ngit commit -m x\nEOF\nwc notes"
    assert (find_commit(parse_command(text)) is not None) is detected


@pytest.mark.parametrize(
    'body',
    [
        'See `docs/operations.md` (commit `e2e6dc8`).',
        'The $X value can change before commit.',
        'See the git history. Then commit nothing.',
    ],
)
@pytest.mark.parametrize('sink', ['cat > notes', 'cat >> notes', 'tee notes', 'tee -a notes'])
def test_quoted_prose_bodies_do_not_trigger_detection(body: str, sink: str) -> None:
    command = parse_command(f"mkdir -p dir && {sink} <<'EOF'\n{body}\nEOF\nwc -l notes")
    assert find_commit(command) is None


def test_sink_argument_defect_has_local_and_shared_evidence() -> None:
    command = parse_command("true; cat --unknown > notes <<'EOF'\ngit commit -m x\nEOF\n")
    blockers = analyze_data_writes(command).blockers
    local = next(item for item in blockers if item.kind == 'arguments' and item.body is not None)
    shared = next(item for item in blockers if item.kind == 'arguments' and item.body is None)
    assert local.span is shared.span


def test_tee_body_reports_its_own_word_defect() -> None:
    command = parse_command("tee $TMPDIR/notes <<'EOF'\ngit commit -m x\nEOF\nwc notes")
    blockers = analyze_data_writes(command).blockers
    assert [blocker.kind for blocker in blockers if blocker.body is not None] == [
        'unquoted-variable'
    ]


def test_tee_operands_after_double_dash_still_sink() -> None:
    assert (
        find_commit(parse_command("tee -- -notes <<'EOF'\ngit commit -m x\nEOF\nwc -- -notes"))
        is None
    )


@pytest.mark.parametrize(
    ('sink', 'neighbor', 'kind'),
    [
        ('cat', 'wc notes', 'sink'),
        ('cat > notes', 'echo $x', 'expansion'),
        ('cat > notes', 'wc -l $TMPDIR/notes', 'unquoted-variable'),
        ('cat > notes', 'head -n "$N" notes', 'literal-required'),
        ('cat > notes', 'sort -k1 notes', 'arguments'),
        ('cat > notes', 'python $X/s.py', 'execution-consumer'),
        ('cat > notes', 'npx $X', 'unknown-form'),
        ('cat > notes', 'cat < $TMPDIR/notes', 'unquoted-variable'),
        ('cat > notes', 'sh notes', 'execution-consumer'),
        ('cat > notes', 'npx prettier notes', 'unknown-form'),
        ('cat > DIR/wc', 'wc notes', 'write-target-collision'),
        ('cat > notes', '(true', 'structure'),
    ],
)
def test_blockers_identify_the_scanning_cause(sink: str, neighbor: str, kind: str) -> None:
    analysis = analyze_data_writes(
        parse_command(f"{sink} <<'EOF'\ngit commit -m x\nEOF\n{neighbor}")
    )
    assert not analysis.exempt_bodies
    assert kind in {blocker.kind for blocker in analysis.blockers}
    assert all(blocker.span is not None for blocker in analysis.blockers)


def test_unquoted_body_blocker_has_local_identity() -> None:
    command = parse_command('cat > notes <<EOF\ngit commit -m x\nEOF\nwc notes')
    body = next(item for item in command.segments[0].elements if isinstance(item, Heredoc))
    blocker = next(
        item for item in analyze_data_writes(command).blockers if item.kind == 'quoting'
    )
    assert blocker.body is body
    assert blocker.span is body.span


def test_unquoted_variable_blocker_marks_offending_word() -> None:
    analysis = analyze_data_writes(
        parse_command("cat > notes <<'EOF'\ngit commit -m x\nEOF\nwc -l $TMPDIR/notes")
    )
    blocker = next(item for item in analysis.blockers if item.kind == 'unquoted-variable')
    assert blocker.span is not None
    assert blocker.span.source.text[blocker.span.start : blocker.span.end] == '$TMPDIR/notes'


@pytest.mark.parametrize(
    'script',
    [
        'eval "cat > notes <<\'EOF\'\ngit commit -m x\nEOF\nwc notes"',
        'sh -c "cat > notes <<\'EOF\'\ngit commit -m x\nEOF\nwc notes"',
        "echo $(cat > notes <<'EOF'\ngit commit -m x\nEOF\nwc notes\n)",
    ],
)
def test_chained_data_exemptions_exclude_nested_scripts(script: str) -> None:
    assert find_commit(parse_command(script)) is not None


def _detected_chain(neighbor: str, *, target: str = 'notes') -> bool:
    return (
        find_commit(parse_command(f"cat > {target} <<'EOF'\ngit commit -m x\nEOF\n{neighbor}"))
        is not None
    )
