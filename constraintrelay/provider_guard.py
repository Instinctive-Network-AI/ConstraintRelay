"""Mandatory endpoint and credential guard for anything that calls a provider.

In the package rather than in a field-test script, because a guard that each
runner re-implements is a guard that the next runner forgets. Every real-session
runner must call `require()` before it constructs a provider client, and
`tests/test_release_readiness.py` fails if one does not.

Why this exists
---------------
On 2026-10-03, wiring ConstraintFlow's first real-session runner sent a DeepSeek
API key to `api.openai.com`. The mechanism is in the harness, not here:
Vibe-Trading's `_sync_provider_env` copies a provider-specific key into
`OPENAI_API_KEY` but sets `OPENAI_BASE_URL` only if a base URL is already
configured — its own docstring says *"All base URLs must be set explicitly in
.env — no hardcoded defaults."* So with `LANGCHAIN_PROVIDER=deepseek` and no
`DEEPSEEK_BASE_URL`, the OpenAI SDK fell back to its own endpoint and the key
went to the wrong vendor, which rejected it with a 401 quoting a masked prefix.

The shape of that bug is general and worth naming: **in a configuration chain
where the credential has a fallback but the endpoint does not, the default
behaviour is to send the credential somewhere it does not belong.** A missing
endpoint must be an error, never a default. That is what `require()` enforces.

Two independent checks
----------------------
1. **Endpoint.** The resolved host of `base_url` must be on the allow-list for
   the named provider. No defaults, no inference, no "probably fine".
2. **Credential revocation.** A key whose fingerprint is in
   `REVOKED_FINGERPRINTS` is refused outright. This is how "do not make any more
   calls with the compromised credential" becomes a property of the code rather
   than a promise someone has to remember.

Nothing here logs, prints or returns key material. `fingerprint()` is a
truncated SHA-256, and it is compared, never displayed alongside anything that
would identify the key beyond what has already been disclosed.
"""

from __future__ import annotations

import hashlib
import os
from urllib.parse import urlparse

#: Hosts each provider's credential may be sent to. Adding a provider is a
#: deliberate edit; an unlisted provider is refused rather than guessed at.
ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "deepseek": frozenset({"api.deepseek.com"}),
    "openai": frozenset({"api.openai.com"}),
    "anthropic": frozenset({"api.anthropic.com"}),
    "moonshot": frozenset({"api.moonshot.cn"}),
    "dashscope": frozenset({"dashscope.aliyuncs.com"}),
}

#: Which environment variable holds each provider's credential.
KEY_ENV: dict[str, str] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
}

#: `fingerprint(key) -> why it must not be used`.
#:
#: Credentials known to have been exposed. A key listed here is refused even
#: when the endpoint is correct, because the problem is the key, not the call.
#: Remove an entry only after the credential has actually been rotated — the
#: entry costs nothing once the old key no longer exists.
REVOKED_FINGERPRINTS: dict[str, str] = {
    "8d573fa5": (
        "this DEEPSEEK_API_KEY was transmitted to api.openai.com on 2026-10-03 "
        "by the harness's provider env-sync (no base URL configured) and was "
        "rejected with a 401 quoting a masked prefix. Treated as a security "
        "incident; the owner is rotating it. Set the replacement key and this "
        "guard will pass."),
}

#: Truncation length for `fingerprint`. Long enough that two live keys will not
#: collide, short enough to be quotable in a report without being a credential.
FINGERPRINT_CHARS = 8


class ProviderGuardError(RuntimeError):
    """A provider call that must not be made. Always fatal; never caught to
    retry, because every reason this raises is a reason not to send the
    request."""


def fingerprint(key: str) -> str:
    """A short, non-reversible identifier for a credential."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def check(provider: str, base_url: str, *, key: str | None = None
          ) -> str | None:
    """`None` when the call is safe, else a sentence saying why it is not.

    *key* defaults to the environment variable for *provider*. Pass it
    explicitly only in tests.
    """
    provider = (provider or "").strip().lower()
    allowed = ALLOWED_HOSTS.get(provider)
    if allowed is None:
        return (f"provider {provider!r} is not in ALLOWED_HOSTS. Add it "
                f"deliberately rather than letting a credential go to an "
                f"unlisted host.")

    host = (urlparse(base_url or "").hostname or "").lower()
    if not host:
        return (f"--base-url {base_url!r} resolves to no host. A missing "
                f"endpoint must be an error, never a default: that is exactly "
                f"how a provider key reaches the wrong vendor.")
    if host not in allowed:
        return (f"provider {provider!r} credentials may only be sent to "
                f"{', '.join(sorted(allowed))} — not {host!r}. Refusing: a "
                f"provider key sent to another vendor's endpoint is a "
                f"credential disclosure.")

    env_name = KEY_ENV.get(provider, "")
    material = key if key is not None else os.environ.get(env_name, "")
    if not (material or "").strip():
        return f"{env_name or 'the provider credential'} is not set"

    fp = fingerprint(material.strip())
    if fp in REVOKED_FINGERPRINTS:
        return (f"the configured {env_name} is a revoked credential "
                f"(fingerprint {fp}): {REVOKED_FINGERPRINTS[fp]}")
    return None


def require(provider: str, base_url: str, *, key: str | None = None) -> str:
    """Raise `ProviderGuardError` unless the call is safe. Returns the host.

    Call this immediately before constructing a provider client, and let it
    raise — there is no safe way to continue past any of its findings.
    """
    why = check(provider, base_url, key=key)
    if why:
        raise ProviderGuardError(why)
    return (urlparse(base_url).hostname or "").lower()


def configure_env(provider: str, base_url: str, model: str = "") -> None:
    """Set the harness's provider variables *explicitly*, after `require`.

    The harness resolves its endpoint through a fallback chain. Rather than
    trusting that chain to land on the right host, every variable it consults is
    written here, so there is no path by which a default can take over.

    Call `require()` first; this function does not re-check.
    """
    os.environ["LANGCHAIN_PROVIDER"] = provider
    if model:
        os.environ["LANGCHAIN_MODEL_NAME"] = model
    for name in (f"{provider.upper()}_BASE_URL", "OPENAI_BASE_URL",
                 "OPENAI_API_BASE"):
        os.environ[name] = base_url
