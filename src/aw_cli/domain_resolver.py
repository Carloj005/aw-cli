"""
Modulo per la risoluzione dinamica e l'auto-aggiornamento dei domini dei provider
(AnimeUnity / AnimeWorld), con supporto per:
1. Normalizzazione dell'origine canonica dopo redirect HTTP (es. https://animeunity.co -> https://www.animeunity.co).
2. Recupero automatico del nuovo dominio dai canali Telegram pubblici / pagine ponte e tracker community.
3. Scansione intelligente dei TLD con verifica anti-phishing tramite firma HTML/CSRF del sito autentico.
4. Fallback DNS-over-HTTPS (Cloudflare 1.1.1.1 / Google 8.8.8.8) contro blocchi DNS ISP.
5. Persistenza automatica del dominio funzionante in config.toml.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlsplit
from httpx import Client, HTTPError
from . import utilities as ut

# Domini predefiniti ufficiali (comunicati sui canali Telegram ufficiali)
DEFAULT_ANIMEUNITY_URL = "https://animeunity.co"
DEFAULT_ANIMEWORLD_URL = "https://www.animeworld.ac"

# Fonti pubbliche (canali Telegram web preview e repository community aggiornate)
ANIMEUNITY_DISCOVERY_SOURCES = [
    "https://t.me/s/animeunitytv",
    "https://raw.githubusercontent.com/fexh10/aw-cli/main/src/aw_cli/providers/animeunity.py",
    "https://raw.githubusercontent.com/doGior/doGiorsHadEnough/master/AnimeUnity/src/main/kotlin/it/dogior/hadEnough/AnimeUnityPlugin.kt",
]

ANIMEWORLD_DISCOVERY_SOURCES = [
    "https://t.me/s/AnimeWorldITA2",
    "https://raw.githubusercontent.com/fexh10/aw-cli/main/src/aw_cli/providers/animeworld.py",
]

# TLD candidati in ordine di probabilità storica per lo scan di emergenza
ANIMEUNITY_CANDIDATE_TLDS = [
    "co", "so", "to", "tv", "it", "cc", "io", "me", "ac", "org", "net", "sh", "sx"
]

ANIMEWORLD_CANDIDATE_TLDS = [
    "ac", "so", "to", "tv", "it", "co", "cc", "io", "me", "org", "net"
]

# Endpoint DNS-over-HTTPS (RFC 8484 JSON)
DOH_ENDPOINTS = [
    "https://cloudflare-dns.com/dns-query",
    "https://1.1.1.1/dns-query",
    "https://dns.google/resolve",
]

_DOH_IP_CACHE: dict[str, str] = {}
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_DOH_HOOK_INSTALLED = False


def normalize_base_url(url: str) -> str:
    """Normalizza un URL mantenendo solo scheme://netloc senza slash finale."""
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parts = urlsplit(url)
    if not parts.netloc:
        return url
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def extract_origin(url: str, fallback: str = "") -> str:
    """Estrae scheme://netloc da un URL completo (es. URL finale post-redirect)."""
    parts = urlsplit(str(url))
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    return normalize_base_url(fallback or str(url))


def is_valid_animeunity_html(html: str) -> bool:
    """
    Verifica che l'HTML appartenga realmente ad AnimeUnity e non a una pagina
    di blocco ISP, Cloudflare challenge o sito clone malevolo.
    """
    if not html:
        return False
    has_csrf = bool(re.search(r'<meta[^>]*name="csrf-token"[^>]*content="([^"]+)"', html))
    has_marker = (
        "<layout-items" in html
        or "animeunity" in html.lower()
        or "/livesearch" in html
    )
    return has_csrf and has_marker


def is_valid_animeworld_html(html: str) -> bool:
    """Verifica che l'HTML appartenga realmente ad AnimeWorld."""
    if not html:
        return False
    return (
        'id="csrf-token"' in html
        or 'class="poster"' in html
        or "document.cookie" in html
    )


def resolve_via_doh(hostname: str) -> str | None:
    """
    Risolve un hostname tramite DNS-over-HTTPS (Cloudflare / Google)
    per aggirare eventuali blocchi o avvelenamenti DNS dell'ISP locale.
    """
    if hostname in _DOH_IP_CACHE:
        return _DOH_IP_CACHE[hostname]

    with Client(timeout=4.0, follow_redirects=True) as doh_client:
        for endpoint in DOH_ENDPOINTS:
            try:
                resp = doh_client.get(
                    endpoint,
                    params={"name": hostname, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                )
                if resp.status_code != 200:
                    continue
                data = resp.json()
                for answer in data.get("Answer", []):
                    ip = str(answer.get("data", "")).strip()
                    try:
                        parsed_ip = ipaddress.ip_address(ip)
                        if parsed_ip.version == 4 and parsed_ip.is_global:
                            _DOH_IP_CACHE[hostname] = ip
                            return ip
                    except ValueError:
                        continue
            except Exception:
                continue
    return None


def install_doh_fallback() -> None:
    """
    Installa un fallback trasparente su socket.getaddrinfo: se il DNS di sistema
    fallisce o restituisce un IP di loopback per i domini animeunity/animeworld/vixcloud,
    utilizza automaticamente DNS-over-HTTPS (Cloudflare/Google).
    """
    global _DOH_HOOK_INSTALLED
    if _DOH_HOOK_INSTALLED:
        return

    def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        host_str = host.decode("utf-8", errors="ignore") if isinstance(host, bytes) else str(host)
        is_target = any(
            kw in host_str.lower()
            for kw in ("animeunity.", "animeworld.", "vixcloud.", "vix-content.", "sweetpixel.")
        )
        if is_target and host_str in _DOH_IP_CACHE:
            return _ORIGINAL_GETADDRINFO(_DOH_IP_CACHE[host_str], port, family, type, proto, flags)

        try:
            res = _ORIGINAL_GETADDRINFO(host, port, family, type, proto, flags)
            if is_target and res:
                first_ip = res[0][4][0]
                if str(first_ip).startswith(("127.", "0.")):
                    doh_ip = resolve_via_doh(host_str)
                    if doh_ip:
                        return _ORIGINAL_GETADDRINFO(doh_ip, port, family, type, proto, flags)
            return res
        except socket.gaierror:
            if is_target:
                doh_ip = resolve_via_doh(host_str)
                if doh_ip:
                    return _ORIGINAL_GETADDRINFO(doh_ip, port, family, type, proto, flags)
            raise

    socket.getaddrinfo = _patched_getaddrinfo
    _DOH_HOOK_INSTALLED = True


def get_configured_provider_url(provider_name: str, default_url: str) -> str:
    """
    Restituisce l'URL configurato per il provider (da variabile d'ambiente,
    da config.toml, oppure il valore predefinito).
    """
    env_key = f"AW_CLI_{provider_name.upper()}_URL"
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        return normalize_base_url(env_val)

    cfg_url = ut.config_data.get("provider", {}).get(f"{provider_name}_url", "").strip()
    if cfg_url:
        return normalize_base_url(cfg_url)

    return normalize_base_url(default_url)


def save_configured_provider_url(provider_name: str, working_url: str) -> None:
    """
    Salva il dominio funzionante in memoria e nel file config.toml dell'utente
    (se esistente) in modo che i successivi avvii usino subito il dominio aggiornato.
    """
    normalized = normalize_base_url(working_url)
    if "provider" not in ut.config_data or not isinstance(ut.config_data["provider"], dict):
        ut.config_data["provider"] = {}

    key = f"{provider_name}_url"
    if ut.config_data["provider"].get(key) == normalized:
        return

    ut.config_data["provider"][key] = normalized
    try:
        config_path = ut.get_config_path()
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                disk_cfg = ut.toml.load(f)
            if "provider" not in disk_cfg or not isinstance(disk_cfg["provider"], dict):
                disk_cfg["provider"] = {}
            disk_cfg["provider"][key] = normalized
            with open(config_path, "w", encoding="utf-8") as f:
                ut.toml.dump(disk_cfg, f)
            try:
                os.chmod(config_path, 0o600)
            except OSError:
                pass
    except Exception:
        pass


def _discover_urls_from_sources(client: Client, sources: list[str], domain_keyword: str) -> list[str]:
    """
    Estrae potenziali URL del provider da canali Telegram pubblici (t.me/s/...)
    e tracker community.
    """
    found: list[str] = []
    pattern = re.compile(
        rf"https?://(?:www\.)?{re.escape(domain_keyword)}\.[a-z]{{2,10}}",
        re.IGNORECASE,
    )
    custom_tg = (
        os.environ.get("AW_CLI_TELEGRAM_URL", "").strip()
        or ut.config_data.get("provider", {}).get("telegram_url", "").strip()
    )
    valid_custom_tg = (
        [custom_tg]
        if custom_tg and urlsplit(custom_tg).scheme.lower() == "https" and urlsplit(custom_tg).netloc
        else []
    )
    all_sources = valid_custom_tg + list(sources)

    for src in all_sources:
        try:
            resp = client.get(src, timeout=5.0)
            if resp.status_code == 200:
                for match in pattern.findall(resp.text):
                    norm = normalize_base_url(match)
                    if norm not in found:
                        found.append(norm)
        except Exception:
            continue
    return found


def resolve_animeunity_session(client: Client, initial_url: str) -> tuple[str, str]:
    """
    Tenta di connettersi ad AnimeUnity partendo da `initial_url` (es. https://animeunity.co),
    seguendo automaticamente i redirect verso l'origine canonica (es. https://www.animeunity.co).
    Se il dominio non risponde o è cambiato, avvia la scoperta automatica tramite Telegram,
    tracker community, DNS-over-HTTPS e scansione TLD.

    Returns:
        tuple[str, str]: (canonical_base_url, html_homepage)
    """
    install_doh_fallback()

    candidates: list[str] = []
    for base in (initial_url, DEFAULT_ANIMEUNITY_URL, "https://www.animeunity.co"):
        norm = normalize_base_url(base)
        if norm not in candidates:
            candidates.append(norm)

    def _try_candidate(url: str) -> tuple[str, str] | None:
        try:
            response = client.get(url)
            status = getattr(response, "status_code", 200)
            if status == 200 and is_valid_animeunity_html(response.text):
                canonical = extract_origin(str(getattr(response, "url", url)), fallback=url)
                return canonical, response.text
        except Exception:
            # Se fallisce per DNS/connessione, prova a pre-risolvere via DoH e ritenta
            host = urlsplit(url).hostname
            if host and resolve_via_doh(host):
                try:
                    response = client.get(url)
                    status = getattr(response, "status_code", 200)
                    if status == 200 and is_valid_animeunity_html(response.text):
                        canonical = extract_origin(str(getattr(response, "url", url)), fallback=url)
                        return canonical, response.text
                except Exception:
                    pass
        return None

    # 1. Prova i candidati diretti (configurato + https://animeunity.co + https://www.animeunity.co)
    for candidate in candidates:
        res = _try_candidate(candidate)
        if res is not None:
            return res

    # 2. Il dominio principale potrebbe essere cambiato (annuncio Telegram): cerca dalle fonti
    ut.console.print(
        "Dominio AnimeUnity non raggiungibile, avvio ricerca automatica del nuovo dominio...",
        style="warning",
    )
    discovered = _discover_urls_from_sources(client, ANIMEUNITY_DISCOVERY_SOURCES, "animeunity")
    for tld in ANIMEUNITY_CANDIDATE_TLDS:
        for prefix in ("https://animeunity.", "https://www.animeunity."):
            cand = f"{prefix}{tld}"
            if cand not in candidates and cand not in discovered:
                discovered.append(cand)

    for candidate in discovered:
        if candidate in candidates:
            continue
        res = _try_candidate(candidate)
        if res is not None:
            ut.console.print(f"Nuovo dominio AnimeUnity rilevato: {res[0]}", style="success")
            save_configured_provider_url("animeunity", res[0])
            return res

    raise HTTPError(f"Impossibile raggiungere AnimeUnity (ultimo dominio tentato: {initial_url})")


def resolve_animeworld_url(client: Client, initial_url: str) -> str:
    """
    Risolve il dominio attivo di AnimeWorld seguendo redirect e usando fallback
    da Telegram/tracker/TLD se il dominio principale non è raggiungibile.
    """
    install_doh_fallback()

    candidates: list[str] = []
    for base in (initial_url, DEFAULT_ANIMEWORLD_URL, "https://animeworld.ac"):
        norm = normalize_base_url(base)
        if norm not in candidates:
            candidates.append(norm)

    def _try_candidate(url: str) -> str | None:
        try:
            response = client.get(url)
            if is_valid_animeworld_html(response.text):
                return extract_origin(str(response.url))
        except Exception:
            host = urlsplit(url).hostname
            if host and resolve_via_doh(host):
                try:
                    response = client.get(url)
                    if is_valid_animeworld_html(response.text):
                        return extract_origin(str(response.url))
                except Exception:
                    pass
        return None

    for candidate in candidates:
        res = _try_candidate(candidate)
        if res is not None:
            return res

    discovered = _discover_urls_from_sources(client, ANIMEWORLD_DISCOVERY_SOURCES, "animeworld")
    for tld in ANIMEWORLD_CANDIDATE_TLDS:
        for prefix in ("https://www.animeworld.", "https://animeworld."):
            cand = f"{prefix}{tld}"
            if cand not in candidates and cand not in discovered:
                discovered.append(cand)

    for candidate in discovered:
        if candidate in candidates:
            continue
        res = _try_candidate(candidate)
        if res is not None:
            ut.console.print(f"Nuovo dominio AnimeWorld rilevato: {res}", style="success")
            save_configured_provider_url("animeworld", res)
            return res

    return initial_url
