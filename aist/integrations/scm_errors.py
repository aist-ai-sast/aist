from __future__ import annotations

# 401/403 from an SCM file fetch mean the organization's integration credential is
# expired, revoked, or lacks access to the repository.
SCM_AUTH_FAILURE_STATUSES = frozenset({401, 403})


class ScmFetchError(Exception):

    """
    The SCM answered a single-file fetch with a failure status other than 404.

    Carries only the upstream status code: the message never includes the request URL
    (it holds the internal SCM host, the ref, and for some providers query parameters)
    nor any credential, so it is safe to log.
    """

    def __init__(self, status_code: int):
        super().__init__(f"SCM responded with HTTP {status_code}")
        self.status_code = status_code

    @property
    def is_auth_failure(self) -> bool:
        return self.status_code in SCM_AUTH_FAILURE_STATUSES
