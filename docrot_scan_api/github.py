"""Public GitHub repository URL validation.

Strictly accepts HTTPS URLs of the form:
    https://github.com/<owner>/<repo>[/...github UI ref path...]
and derives (owner, repo, ref).

Rejected: non-https schemes, non-GitHub hosts, credentials in URL,
missing owner/repo, owner/repo names violating GitHub naming rules,
`.git` suffix kept only when it is exactly the repo segment.
"""

import re
from urllib.parse import urlsplit, unquote

_GITHUB_HOSTS = {"github.com", "www.github.com"}

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,38}[A-Za-z0-9])?$")
# GitHub repo names additionally allow dots anywhere except start/end pattern
# above; keep conservative. Reserved names that are not repos:
_RESERVED_REPO_NAMES = {"new", "settings", "organizations", "marketplace",
                        "explore", "topics", "trending", "collections",
                        "events", "sponsors", "features", "enterprise",
                        "team", "pricing", "security", "login", "join",
                        "about", "contact", "customer-stories", "readme"}

# ref that appears in a github.com UI URL path:
#   /tree/<ref>  or  /blob/<ref>  exactly; deeper paths (a file inside the
#   repo) are not a repository reference and are rejected.
_UI_REF_RE = re.compile(r"^/(?:tree|blob)/([^/]+)$")

MAX_REF_LEN = 200
_REF_SAFE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._/\-+]*$"
)


class RepositoryValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_repository_url(value, github_host="github.com"):
    """Validate and normalize a user-supplied repository reference.

    Returns dict: {url, owner, repo, ref}
      url   -- canonical https://<host>/<owner>/<repo>
      ref   -- None or ref extracted from a /tree/ or /blob/ path
    Raises RepositoryValidationError with a machine-readable code.
    """
    if not isinstance(value, str) or not value.strip():
        raise RepositoryValidationError("invalid_repository",
                                        "repository must be a non-empty string")
    raw = value.strip()

    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise RepositoryValidationError("invalid_repository",
                                        f"unparseable URL: {exc}") from exc

    if parts.scheme != "https":
        raise RepositoryValidationError(
            "unsupported_repository_scheme",
            "repository must use https:// scheme")
    host = (parts.hostname or "").lower()
    if host not in _GITHUB_HOSTS and host != github_host.lower():
        raise RepositoryValidationError(
            "unsupported_repository_host",
            "only https://github.com repositories are supported")
    if parts.username or parts.password or "@" in (parts.netloc or ""):
        raise RepositoryValidationError(
            "invalid_repository",
            "credentials in repository URL are not allowed")
    if parts.query or parts.fragment:
        raise RepositoryValidationError(
            "invalid_repository",
            "query strings and fragments are not allowed in repository URL")

    path = unquote(parts.path or "")
    segments = [s for s in path.split("/") if s != ""]

    if len(segments) < 2:
        raise RepositoryValidationError(
            "invalid_repository",
            "repository URL must be https://github.com/<owner>/<repo>")

    owner, repo = segments[0], segments[1]
    extra = segments[2:]

    if not _NAME_RE.match(owner) or owner.startswith(".") or owner.endswith("."):
        raise RepositoryValidationError("invalid_repository",
                                        f"invalid GitHub owner: {owner!r}")
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not repo or not _NAME_RE.match(repo) or repo.startswith(".") or repo.endswith("."):
        raise RepositoryValidationError("invalid_repository",
                                        f"invalid GitHub repo: {repo!r}")
    if repo.lower() in _RESERVED_REPO_NAMES:
        raise RepositoryValidationError("invalid_repository",
                                        f"{repo!r} is not a repository name")

    ref = None
    if extra:
        m = _UI_REF_RE.match("/" + "/".join(extra))
        if not m:
            raise RepositoryValidationError(
                "invalid_repository",
                "only a /tree/<ref> or /blob/<ref> suffix is allowed; file "
                "paths inside the repository are not accepted — pass a plain "
                "https://github.com/<owner>/<repo> URL and use ref")
        ref = m.group(1)
        validate_ref(ref)

    canon = f"https://{github_host}/{owner}/{repo}"
    return {"url": canon, "owner": owner, "repo": repo, "ref": ref}


def validate_ref(ref):
    """Validate a ref that will be embedded into a codeload archive URL."""
    if not isinstance(ref, str) or not ref or len(ref) > MAX_REF_LEN:
        raise RepositoryValidationError(
            "invalid_ref",
            f"ref must be a non-empty string of at most {MAX_REF_LEN} chars")
    if not _REF_SAFE_RE.match(ref):
        raise RepositoryValidationError(
            "invalid_ref",
            "ref contains characters outside the allowed set "
            "[A-Za-z0-9._/-+], must not start with a separator")
    lowered = ref.lower()
    if lowered.startswith("refs/") or lowered in ("head", "heads", "tags"):
        raise RepositoryValidationError("invalid_ref",
                                        f"reserved ref form: {ref!r}")
    if ref.startswith(".") or ref.endswith("/") or "//" in ref or ".." in ref:
        raise RepositoryValidationError("invalid_ref",
                                        f"unsafe ref path shape: {ref!r}")
    if "%" in ref or "\\" in ref:
        raise RepositoryValidationError("invalid_ref",
                                        f"unsafe ref encoding: {ref!r}")
    return ref
