#!/usr/bin/env python3
"""Le Fil : relevé des flux RSS, sans algorithme.

Exécuté par GitHub Actions toutes les 10 minutes environ. Lit sources.toml et la
mémoire du relevé précédent (etat.json), interroge tous les flux, puis écrit :

  site/articles.json   la liste publiée, lue par la page
  site/sources.json    l'état de chaque source
  etat.json            la mémoire du relevé (articles, doublons, caches HTTP)

En local :  python3 releve.py   puis   python3 -m http.server -d site 8000
"""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import json
import logging
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import feedparser
import httpx

ICI = Path(__file__).resolve().parent
CONFIG = tomllib.loads((ICI / "sources.toml").read_text(encoding="utf-8"))
R = {
    "age_max_heures": 48,
    "historique": 300,
    "images_par_releve": 30,
    **CONFIG.get("reglages", {}),
}
SOURCES: list[dict] = CONFIG["source"]
FICHIER_ETAT = ICI / "etat.json"
SITE = ICI / "site"
UA = R.get("user_agent") or (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
MAINTENANT = time.time()

logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("le-fil")

# Mémoire du relevé, chargée depuis etat.json dans main()
articles: dict[str, dict] = {}       # id -> article
vus: dict[str, int] = {}             # id déjà rencontrés (anti-doublons), avec la date
connues: set[str] = set()            # sources déjà relevées au moins une fois
retenus: dict[str, list[int]] = {}   # horodatages par source, pour max_par_heure
memo_sources: dict[str, dict] = {}   # par source : flux trouvé, ETag, Last-Modified


# ---------------------------------------------------------------- nettoyage

BALISE = re.compile(r"<[^>]+>")
ESPACES = re.compile(r"\s+")
# Mentions ajoutées automatiquement en fin de résumé (WordPress, « Lire la suite », Hacker News).
# Elles doivent commencer une phrase, pour ne pas couper un « de l'article » au milieu du texte.
FIN_PARASITE = re.compile(
    r"(?:^|(?<=[.!?…»\"”)]))\s*(?:"
    r"L[’']article\s.{0,300}?\sest apparu en premier sur\s.*"
    r"|The post\s.{0,300}?\sappeared first on\s.*"
    r"|(?:Lire|Voir) la suite\b.{0,80}|Continue reading\b.{0,80}|Read more\b.{0,80}"
    r"|Article URL:.*)$",
    re.S,
)
PISTAGE = re.compile(r"^(utm_|xtor$|at_|fbclid$|gclid$|mc_cid$|mc_eid$)", re.I)
IMG = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
EXT_IMAGE = re.compile(r"\.(jpe?g|png|webp|gif|avif)(\?|$)", re.I)
IMG_INUTILE = re.compile(
    r"(/emoji/|feeds\.feedburner\.com|gravatar\.com|stats\.wp\.com|/1x1|spacer\.gif|\.svg(\?|$))", re.I
)


def texte(brut: str | None) -> str:
    s = html.unescape(BALISE.sub(" ", brut or ""))
    s = BALISE.sub(" ", s)  # balises qui arrivaient échappées
    return ESPACES.sub(" ", s).strip()


def extrait(entree, titre: str) -> str:
    brut = entree.get("summary") or ""
    if not brut and entree.get("content"):
        brut = entree["content"][0].get("value", "")
    s = FIN_PARASITE.sub("", texte(brut)).strip()
    s = re.sub(r"\s*\[(…|\.\.\.)\]\s*$", "…", s)
    if len(s) > 320:
        s = s[:320].rsplit(" ", 1)[0].rstrip(",;:.-–") + "…"
    return "" if s == titre else s


def sans_pistage(url: str) -> str:
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not PISTAGE.match(k)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def identifiant(url: str) -> str:
    p = urlsplit(url)
    cle = p.netloc.lower().removeprefix("www.") + p.path.rstrip("/") + ("?" + p.query if p.query else "")
    return hashlib.sha1(cle.encode()).hexdigest()[:16]


def image_utilisable(candidat: str | None, base: str) -> str | None:
    if not candidat:
        return None
    u = urljoin(base, html.unescape(candidat.strip()))
    if u.startswith(("http://", "https://")) and not IMG_INUTILE.search(u):
        return u
    return None


def image(entree, base: str) -> str | None:
    cands = [m.get("url") for m in entree.get("media_thumbnail") or []]
    for m in entree.get("media_content") or []:
        if m.get("medium") == "image" or (m.get("type") or "").startswith("image/") \
                or EXT_IMAGE.search(m.get("url") or ""):
            cands.append(m.get("url"))
    for lien in entree.get("links") or []:
        if lien.get("rel") == "enclosure" and ((lien.get("type") or "").startswith("image/")
                                              or EXT_IMAGE.search(lien.get("href") or "")):
            cands.append(lien.get("href"))
    brut = " ".join([c.get("value", "") for c in entree.get("content") or []] + [entree.get("summary") or ""])
    cands += IMG.findall(brut)
    for c in cands:
        if u := image_utilisable(c, base):
            return u
    return None


# ---------------------------------------------------------------- réseau

class LecteurEntete(HTMLParser):
    """Lit le <head> d'une page : image de partage (og:image) et flux annoncés."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images: list[str] = []
        self.flux: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            cle = (a.get("property") or a.get("name") or "").lower()
            if cle in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image",
                       "twitter:image:src") and a.get("content"):
                self.images.append(a["content"])
        elif tag == "link" and "alternate" in a.get("rel", "").lower() \
                and a.get("type", "").lower() in ("application/rss+xml", "application/atom+xml") \
                and a.get("href"):
            self.flux.append(a["href"])


async def lire_entete(client: httpx.AsyncClient, url: str) -> tuple[LecteurEntete, str]:
    morceaux: list[bytes] = []
    async with client.stream("GET", url, headers={"Accept": "text/html,application/xhtml+xml"}) as r:
        r.raise_for_status()
        async for bloc in r.aiter_bytes():
            morceaux.append(bloc)
            fin = b"".join(morceaux[-2:]).lower()
            if b"</head>" in fin or sum(map(len, morceaux)) > 600_000:
                break
        base, enc = str(r.url), r.charset_encoding or "utf-8"
    lecteur = LecteurEntete()
    try:
        lecteur.feed(b"".join(morceaux).decode(enc, errors="replace"))
    except Exception:
        pass
    return lecteur, base


async def decouvrir(client: httpx.AsyncClient, site: str) -> str | None:
    """Cherche l'adresse du flux annoncée dans la page d'accueil d'un site."""
    lecteur, base = await lire_entete(client, site)
    candidats = [h for h in lecteur.flux if "comment" not in h.lower()] or lecteur.flux
    return urljoin(base, candidats[0]) if candidats else None


async def telecharger_flux(client: httpx.AsyncClient, url: str, memo: dict):
    entetes = {}
    if memo.get("url_cache") == url:  # requête conditionnelle : « rien de neuf ? »
        if memo.get("etag"):
            entetes["If-None-Match"] = memo["etag"]
        if memo.get("modifie"):
            entetes["If-Modified-Since"] = memo["modifie"]
    r = await client.get(url, headers=entetes)
    if r.status_code == 304:
        return "inchange", None, url
    r.raise_for_status()
    flux = await asyncio.to_thread(
        feedparser.parse, r.content,
        response_headers={"content-location": str(r.url), "content-type": r.headers.get("content-type", "")},
    )
    if not flux.entries:
        raise ValueError("flux vide ou illisible")
    memo.update(url_cache=url, etag=r.headers.get("etag"), modifie=r.headers.get("last-modified"))
    return "ok", flux, str(r.url)


async def relever(client: httpx.AsyncClient, src: dict, sem: asyncio.Semaphore) -> tuple[dict, list[dict]]:
    nom = src["nom"]
    memo = memo_sources.setdefault(nom, {})
    if memo.get("flux_config") != src.get("flux"):  # adresse modifiée dans sources.toml
        memo.clear()
        memo["flux_config"] = src.get("flux")
    bilan = {"nom": nom, "categorie": src.get("categorie", "tech"), "site": src.get("site")}
    nouveaux: list[dict] = []
    async with sem:
        try:
            url = memo.get("flux") or src.get("flux")
            statut = flux = base = None
            erreur_initiale: Exception | None = None
            if url:
                try:
                    statut, flux, base = await telecharger_flux(client, url, memo)
                except Exception as e:
                    erreur_initiale = e
            if statut is None:
                # Le flux ne répond pas : on cherche celui que le site annonce.
                try:
                    trouve = await decouvrir(client, src["site"]) if src.get("site") else None
                except Exception as e:
                    raise erreur_initiale or e
                if not trouve or trouve == url:
                    raise erreur_initiale or ValueError("aucun flux annoncé sur la page d'accueil")
                statut, flux, base = await telecharger_flux(client, trouve, memo)
                log.info("%s : flux trouvé automatiquement -> %s", nom, trouve)
                url = trouve
            memo["flux"] = url
            if statut == "ok":
                nouveaux = traiter(src, flux, base)
            bilan.update(ok=True, erreur=None, flux=url)
        except Exception as e:
            bilan.update(ok=False, erreur=f"{type(e).__name__} : {e}"[:240], flux=memo.get("flux") or src.get("flux"))
            log.warning("%s : %s", nom, bilan["erreur"])
    bilan["nouveaux"] = len(nouveaux)
    return bilan, nouveaux


def traiter(src: dict, flux, base: str) -> list[dict]:
    nom = src["nom"]
    plancher = MAINTENANT - R["age_max_heures"] * 3600
    plafond = src.get("max_par_heure")
    recents = [t for t in retenus.get(nom, []) if t > MAINTENANT - 3600]
    premiere_fois = nom not in connues
    nouveaux = []

    for e in flux.entries:
        lien, titre = (e.get("link") or "").strip(), texte(e.get("title"))
        if not lien or not titre:
            continue
        lien = sans_pistage(urljoin(base, lien))
        if not lien.startswith(("http://", "https://")):
            continue
        ident = identifiant(lien)
        if ident in vus:
            continue
        vus[ident] = int(MAINTENANT)
        t = e.get("published_parsed") or e.get("updated_parsed")
        publie = min(calendar.timegm(t), MAINTENANT) if t else MAINTENANT
        if publie < plancher:
            continue
        if plafond is not None and len(recents) >= plafond:
            continue  # quota horaire atteint pour cette source
        recents.append(int(MAINTENANT))
        a = {
            "id": ident, "titre": titre, "lien": lien, "extrait": extrait(e, titre),
            "image": image(e, base), "source": nom, "categorie": src.get("categorie", "tech"),
            "publie": int(publie * 1000),
        }
        if premiere_fois:
            a["discret"] = True  # premier relevé de la source : ajout sans animation
        articles[ident] = a
        nouveaux.append(a)

    retenus[nom] = recents
    connues.add(nom)
    return nouveaux


async def completer_image(client: httpx.AsyncClient, a: dict, sem: asyncio.Semaphore) -> None:
    """Article sans image dans le flux : on prend l'image de partage de sa page."""
    async with sem:
        try:
            lecteur, base = await lire_entete(client, a["lien"])
        except Exception:
            return
    for c in lecteur.images:
        if u := image_utilisable(c, base):
            a["image"] = u
            return


# ---------------------------------------------------------------- écriture

def ecrire_json(chemin: Path, donnees) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(".tmp")
    tmp.write_text(json.dumps(donnees, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(chemin)


def resume_github(bilans: list[dict], total: int, publies: int) -> None:
    """Tableau affiché sur la page du relevé dans l'onglet Actions."""
    chemin = os.environ.get("GITHUB_STEP_SUMMARY")
    if not chemin:
        return
    lignes = [f"### Le Fil : {total} nouvel(s) article(s), {publies} publiés", "",
              "| Source | État | Nouveaux |", "|---|---|---|"]
    for b in bilans:
        etat = "OK" if b["ok"] else f"Erreur : {b['erreur']}".replace("|", "/")
        lignes.append(f"| {b['nom']} | {etat} | {b['nouveaux']} |")
    with open(chemin, "a", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")


# ---------------------------------------------------------------- programme

def charger_etat() -> None:
    noms = {s["nom"] for s in SOURCES}
    try:
        d = json.loads(FICHIER_ETAT.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.info("Pas de mémoire précédente : premier relevé.")
        return
    except Exception as e:
        log.warning("Mémoire illisible (%s), on repart de zéro.", e)
        return
    articles.update({a["id"]: a for a in d.get("articles", []) if a.get("source") in noms})
    vus.update(d.get("vus", {}))
    connues.update(n for n in d.get("connues", []) if n in noms)
    retenus.update({k: v for k, v in d.get("retenus", {}).items() if k in noms})
    memo_sources.update({k: v for k, v in d.get("sources", {}).items() if k in noms})


async def principal() -> None:
    charger_etat()
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20, connect=10),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=20),
        headers={
            "User-Agent": UA,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
        },
    ) as client:
        sem = asyncio.Semaphore(8)
        resultats = await asyncio.gather(*(relever(client, s, sem) for s in SOURCES))
        bilans = [b for b, _ in resultats]
        nouveaux = [a for _, liste in resultats for a in liste]
        sans_image = [a for a in nouveaux if not a["image"]][: R["images_par_releve"]]
        sem_images = asyncio.Semaphore(6)
        await asyncio.gather(*(completer_image(client, a, sem_images) for a in sans_image))

    # On ne garde que les articles les plus récents, et une mémoire des doublons bornée.
    garder = sorted(articles.values(), key=lambda a: a["publie"], reverse=True)[: R["historique"]]
    if len(vus) > 25000:
        for k, _ in sorted(vus.items(), key=lambda kv: kv[1])[: len(vus) - 20000]:
            del vus[k]

    genere = int(time.time() * 1000)
    compte: dict[str, int] = {}
    for a in garder:
        compte[a["source"]] = compte.get(a["source"], 0) + 1
    for b in bilans:
        b["articles"] = compte.get(b["nom"], 0)

    ecrire_json(SITE / "articles.json", {"genere": genere, "articles": garder})
    ecrire_json(SITE / "sources.json", {"genere": genere, "sources": bilans})
    ecrire_json(FICHIER_ETAT, {
        "articles": garder, "vus": vus, "connues": sorted(connues),
        "retenus": retenus, "sources": memo_sources,
    })

    en_erreur = sum(not b["ok"] for b in bilans)
    log.info("Relevé terminé : %d nouveau(x), %d publiés, %d source(s) en erreur sur %d.",
             len(nouveaux), len(garder), en_erreur, len(bilans))
    resume_github(bilans, len(nouveaux), len(garder))


if __name__ == "__main__":
    asyncio.run(principal())
