"""ConstraintFlow — prove the rules you wrote reached the model.

Everything else in this space guards the model's *output*. This proves the
model's *input*: that your declaration was in the payload on this request,
with a receipt that names its own observation boundary.

The five-minute path
--------------------
::

    import constraintflow as cf

    guard = cf.attach(client, host="openai-python",
                      library="lib.json", source="./skills")

    with guard.active("my-skill"):
        client.chat.completions.create(model=…, messages=[…], tools=[…])

    guard.last_receipt.ok          # was it actually in the payload?

`client` is your own OpenAI-shaped SDK client. The patch is on **that
instance** and `guard.uninstall()` reverses it. A ConstraintFlow failure never
raises into your call: it degrades to your original request plus a ledger row
naming the reason.

Why `host=` has to be named
---------------------------
A recorded profile supplies the role the capsule travels in and where it is
placed. Those are not defaults — they are values a run observed, and naming
the host is what selects them. A surface nobody has read is refused with the
reason rather than guessed at; see `cf.host_openai.REFUSED_SURFACES`.

This module vs `cf`
-------------------
`constraintflow` is the product surface. `cf` is the implementation and stays
importable and unchanged — nothing here hides it, and
`cf.container_api.__all__` remains the full container interface. What you get
here is the short path to the common case.

What a receipt does NOT say
---------------------------
That the model read the declaration, understood it, or obeyed it. **Delivery
is not compliance.** Nothing in this project has measured whether a model
follows a declaration, and the receipt says on its face what its observation
could not reach — for this host, the bytes on the wire.
"""

from __future__ import annotations

# -- the five-minute path ----------------------------------------------------
from cf.host_openai import (PROVIDER_PROFILES, REFUSED_SURFACES, ProviderGuard,
                            ProviderHostError, attach)

# -- the container interface, unchanged --------------------------------------
from cf.container_api import (AVAILABLE_COMPLETE, FAILED, UNDECLARED,
                              AdapterConfig, Assembly, ContainerError,
                              ContainerRequest, Receipt, ReceiptError,
                              SkillRequest, SkillVerdict, assemble,
                              build_library, check_manifests, load_library,
                              load_manifests, render, verify,
                              write_manifest_draft)
from cf.request_ledger import (ATTRIBUTION_UNVERIFIED, DELIVERED_COMPLETE,
                               DUE_BUT_MISSING, NO_DECLARATION_DUE, STATUSES,
                               RequestLedger)
from cf import __version__ as _cf_version

#: The product version, read from `cf` rather than restated here. A second
#: literal is a second thing to forget to bump — the defect class this project
#: calls "a document that retypes a computed figure".
#: `cf.container_api.VERSION` is the container interface's own and stays
#: separate on purpose.
__version__ = _cf_version

__all__ = [
    # the short path
    "attach", "ProviderGuard", "ProviderHostError",
    "PROVIDER_PROFILES", "REFUSED_SURFACES",
    # building and checking a library
    "build_library", "load_library", "load_manifests", "check_manifests",
    "write_manifest_draft", "assemble", "render", "verify",
    "ContainerRequest", "SkillRequest", "AdapterConfig",
    "Assembly", "Receipt", "SkillVerdict",
    "ContainerError", "ReceiptError",
    "AVAILABLE_COMPLETE", "FAILED", "UNDECLARED",
    # per-request accounting
    "RequestLedger", "STATUSES", "DELIVERED_COMPLETE", "DUE_BUT_MISSING",
    "NO_DECLARATION_DUE", "ATTRIBUTION_UNVERIFIED",
    "__version__",
]
