"""Per-site knowledge for authenticated sessions: where to check a session, which
cookies carry the login, and how the site signals "logged out" or a captcha.

Verticals register the profiles for the sites they log into (e.g. the jobs vertical
registers LinkedIn), so new sites never require core changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..core.urls import domain_matches, normalize_domain


@dataclass(frozen=True)
class DomainProfile:
    domain: str
    #: Page that only renders for logged-in users (used by health checks).
    check_url: str | None = None
    #: Default page for interactive login.
    login_url: str | None = None
    #: Cookies that must exist (and not be expired) for the session to be valid.
    auth_cookies: tuple[str, ...] = ()
    #: Regexes matched against the final URL path when redirected to a login page.
    login_path_patterns: tuple[str, ...] = ()
    #: Regexes matched against the HTML that mean "you are not logged in".
    logged_out_markers: tuple[str, ...] = ()
    #: Extra captcha / bot-wall markers specific to this site.
    captcha_markers: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def compiled_login_paths(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.I) for p in self.login_path_patterns]

    def compiled_logged_out(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.I) for p in self.logged_out_markers]

    def compiled_captcha(self) -> list[re.Pattern[str]]:
        return [re.compile(p, re.I) for p in self.captcha_markers]


GENERIC_PROFILE = DomainProfile(domain="")

_PROFILES: dict[str, DomainProfile] = {}


def register_profile(profile: DomainProfile) -> None:
    _PROFILES[normalize_domain(profile.domain)] = profile


def profile_for(host_or_domain: str) -> DomainProfile:
    """Most specific registered profile for a host, or a generic one."""
    host = normalize_domain(host_or_domain)
    best: DomainProfile | None = None
    for domain, profile in _PROFILES.items():
        if domain_matches(host, domain) and (best is None or len(domain) > len(best.domain)):
            best = profile
    if best is not None:
        return best
    return DomainProfile(domain=host)


def registered_profiles() -> list[DomainProfile]:
    return list(_PROFILES.values())
