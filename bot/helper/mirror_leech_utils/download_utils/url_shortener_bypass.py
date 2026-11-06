import base64
import codecs
import json
from re import search
from urllib.parse import urlparse

from curl_cffi import requests as cffi_requests

from ...ext_utils.exceptions import DirectDownloadLinkException

_OUO_DOMAINS = ("ouo.io", "ouo.press")
_GREENMOTORS_DOMAINS = ("greenmotors.club", "greenmotors.cc", "greenmountmotors.com")

_CSRF_PATTERN = (
    r'<input[^>]*\bname="_token"[^>]*\bvalue="([^"]*)"'
    r'|<input[^>]*\bvalue="([^"]*)"[^>]*\bname="_token"'
)


def is_url_shortener(domain):
    return (
        any(d in domain for d in _OUO_DOMAINS + _GREENMOTORS_DOMAINS)
        if domain
        else False
    )


def bypass_shortener(link):
    domain = urlparse(link).hostname or ""
    if any(d in domain for d in _OUO_DOMAINS):
        return _ouo(link)
    elif any(d in domain for d in _GREENMOTORS_DOMAINS):
        return _greenmotors(link)
    raise DirectDownloadLinkException(f"ERROR: No bypasser for {domain}")


def _extract_csrf(html):
    m = search(_CSRF_PATTERN, html)
    return next((g for g in m.groups() if g), "") if m else ""


def _ouo(link):
    """Resolve ouo.io / ouo.press shortlinks.

    Three-step CSRF dance: GET landing → POST /go/<id> → POST /xreallcygo/<id>.
    Cloudflare fronts ouo.io and fingerprints both TLS ClientHello and HTTP/2
    SETTINGS, so curl_cffi's chrome impersonation is required — stdlib
    requests/httpx gets 403.
    """
    normalized = link.replace("ouo.press", "ouo.io")
    parsed = urlparse(normalized)
    short_id = parsed.path.rsplit("/", 1)[-1]
    if not short_id:
        raise DirectDownloadLinkException("ERROR: ouo: empty id segment")

    base = f"{parsed.scheme}://{parsed.netloc}"
    go_url = f"{base}/go/{short_id}"
    final_url = f"{base}/xreallcygo/{short_id}"

    try:
        with cffi_requests.Session(impersonate="chrome136", timeout=30) as s:
            r1 = s.get(normalized, allow_redirects=True)
            if r1.status_code == 403:
                raise DirectDownloadLinkException(
                    "ERROR: ouo.io blocked the request (403)"
                )
            tok1 = _extract_csrf(r1.text)
            if not tok1:
                raise DirectDownloadLinkException(
                    f"ERROR: ouo: _token not found on initial page (status={r1.status_code})"
                )

            r2 = s.post(
                go_url,
                data={"_token": tok1, "x-token": "", "v-token": "vm"},
                headers={"Origin": "https://ouo.io", "Referer": normalized},
                allow_redirects=False,
            )
            if r2.status_code == 403:
                raise DirectDownloadLinkException(
                    "ERROR: ouo.io blocked the request (403)"
                )
            if r2.status_code != 200:
                raise DirectDownloadLinkException(
                    f"ERROR: ouo: /go/ status {r2.status_code}"
                )
            tok2 = _extract_csrf(r2.text)
            if not tok2:
                raise DirectDownloadLinkException(
                    "ERROR: ouo: _token not found on /go/ page"
                )

            r3 = s.post(
                final_url,
                data={"_token": tok2, "x-token": ""},
                headers={"Origin": "https://ouo.io", "Referer": go_url},
                allow_redirects=False,
            )
            location = r3.headers.get("Location", "")
            if r3.status_code != 302 or not location:
                raise DirectDownloadLinkException(
                    f"ERROR: ouo: /xreallcygo/ status {r3.status_code} (location={location!r})"
                )
            return location
    except DirectDownloadLinkException:
        raise
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: ouo bypass failed: {e}") from e


def _greenmotors(link):
    """Resolve greenmotors redirector links (greenmotors.club / greenmotors.cc)."""
    if "?id=" not in link:
        raise DirectDownloadLinkException(
            "ERROR: greenmotors URL requires the '?id=' query parameter (intermediate pages like /homelander/ cannot be bypassed alone)"
        )
    try:
        with cffi_requests.Session(impersonate="chrome136", timeout=30) as s:
            res = s.get(link, allow_redirects=True)
            match_payload = search(
                r"s\(\s*['\"]o['\"]\s*,\s*['\"]([^'\"]+)['\"]", res.text
            )
            if not match_payload:
                raise DirectDownloadLinkException(
                    "ERROR: greenmotors payload not found on page"
                )
            raw = match_payload.group(1)

            # Base64 -> Base64 -> ROT13 -> Base64 -> JSON
            s1 = base64.b64decode(raw).decode("utf-8")
            s2 = base64.b64decode(s1).decode("utf-8")
            s3 = codecs.decode(s2, "rot_13")
            data = json.loads(base64.b64decode(s3).decode("utf-8"))
            if "o" not in data:
                raise DirectDownloadLinkException(
                    "ERROR: greenmotors decoded data missing 'o' key"
                )
            return base64.b64decode(data["o"]).decode("utf-8")
    except DirectDownloadLinkException:
        raise
    except Exception as e:
        raise DirectDownloadLinkException(
            f"ERROR: greenmotors bypass failed: {e}"
        ) from e
