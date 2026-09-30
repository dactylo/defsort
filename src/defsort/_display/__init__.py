"""Terminal output formatting for defsort."""

from defsort._display._constraints import (
    format_banner_constraint_details as format_banner_constraint_details,
)
from defsort._display._constraints import (
    format_banner_constraint_summary as format_banner_constraint_summary,
)
from defsort._display._detect import print_detect as print_detect
from defsort._display._diff import print_diff as print_diff
from defsort._display._json import build_json_record as build_json_record
from defsort._display._json import format_json as format_json
from defsort._display._movement import print_preview as print_preview
from defsort._display._skipped import print_skipped as print_skipped
from defsort._display._summary import build_summary_row as build_summary_row
from defsort._display._summary import format_summary_rows as format_summary_rows
