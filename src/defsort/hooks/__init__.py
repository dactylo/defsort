"""Public host adapter contract for `defsort hook`.

A host adapter translates one agent host's hook protocol to defsort's neutral
hook input and translates defsort's neutral result back to the host's response.
Adapters are discovered through the `defsort.hook_adapters` entry-point group;
each entry point names a class whose instances satisfy `HookAdapter`. The
contract types are structural `Protocol` classes, and the concrete `HostInput`
and `HostResponse` records are optional conveniences for adapter
implementations.

Pre-tool input carries two directories. `cwd` is event context consumed only by
post-tool handling. `command_cwd` is the directory the command executes in, or
`None` when the host contract does not establish it; the core then denies a
committing command unless it names an absolute route.

defsort selects the adapter named by `defsort hook --agent NAME`, refuses an
adapter whose `api_version` differs from `API_VERSION`, and never infers the
host from the payload. Classification, Git operations, and the allow or deny
decision never depend on the adapter.
"""

from defsort._vendor.hookfront._contract import (
    API_VERSION as API_VERSION,
)
from defsort._vendor.hookfront._contract import (
    HookAdapter as HookAdapter,
)
from defsort._vendor.hookfront._contract import (
    HookDecisionName as HookDecisionName,
)
from defsort._vendor.hookfront._contract import (
    HookEvent as HookEvent,
)
from defsort._vendor.hookfront._contract import (
    HookInput as HookInput,
)
from defsort._vendor.hookfront._contract import (
    HookResponse as HookResponse,
)
from defsort._vendor.hookfront._contract import (
    HookResult as HookResult,
)
from defsort._vendor.hookfront._contract import (
    HostInput as HostInput,
)
from defsort._vendor.hookfront._contract import (
    HostResponse as HostResponse,
)
from defsort._vendor.hookfront._contract import (
    absolute_directory as absolute_directory,
)
from defsort._vendor.hookfront._contract import (
    notes_text as notes_text,
)
from defsort._vendor.hookfront._contract import (
    object_field as object_field,
)
from defsort._vendor.hookfront._contract import (
    payload_cwd as payload_cwd,
)
