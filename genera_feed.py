#!/usr/bin/env python3
"""Feed prodotti Google Merchant Center per Universo Ceramiche.

Tutti i dati arrivano dalle API Storeden (Connect API v1.1):
  - elenco a pagine: prezzo, giacenza, stato, foto e data di ultima modifica
    di ogni prodotto, letto per intero ogni notte (~130 chiamate);
  - scheda completa (EAN, peso, m², su ordinazione, galleria, categorie) e
    traduzioni di titolo e descrizione: lette solo per i prodotti nuovi o
    modificati; il resto si riprende dallo stato salvato la notte prima.

Le traduzioni costano una chiamata per campo, prodotto e lingua: il primo
riempimento richiede qualche notte. Ogni giro si ferma a MAX_CHIAMATE (o al
limite Storeden di 100.000/24 h), pubblica quello che ha e riprende dopo.

Uso:
  python3 genera_feed.py                  giro normale
  python3 genera_feed.py --limite 200     prova sui primi N prodotti
  python3 genera_feed.py --lingue it,es   solo alcune lingue
  python3 genera_feed.py --campione       salva il JSON grezzo di un prodotto

Chiave API: variabili d'ambiente STOREDEN_KEY e STOREDEN_EXCHANGE, oppure il
file .chiave-storeden.txt accanto allo script con le righe «key: ...» e
«exchange: ...». Basta una chiave di sola lettura.

Uscita: out/feed-<lingua>.xml               feed Google Merchant Center
        out/clerk/products-<locale>.json     feed Clerk.io (JSON Feed V2), stessi
        out/clerk/categories-<locale>.json   campi del file di No Gravity
        out/rapporto.json, out/index.html    (tutto in out/ è pubblicato)
        stato/stato.json.gz (NON pubblicato: contiene le giacenze esatte)
"""
import argparse, datetime, gzip, html, json, math, os, queue, re, ssl, struct, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from xml.sax.saxutils import escape

QUI = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(QUI, 'out')
STATO = os.path.join(QUI, 'stato', 'stato.json.gz')
API = 'https://connect.storeden.com/v1.1/'
IMG = 'https://egress.storeden.net/gallery/'
SEGNAPOSTO = IMG + '67448c93202628c07df34a63'   # immagine «senza foto» usata da Clerk

LINGUE = {
    # lingua: (locale Storeden, dominio, valuta)
    'it': ('it_IT', 'https://universoceramiche.it', 'EUR'),
    'en': ('en_US', 'https://universoceramiche.com', 'EUR'),
    'de': ('de_DE', 'https://universoceramiche.de', 'EUR'),
    'es': ('es_ES', 'https://universoceramiche.es', 'EUR'),
    'fr': ('fr_FR', 'https://universoceramiche.fr', 'EUR'),
}
ORDINE_TRADUZIONI = ['es', 'en', 'de', 'fr']   # chi viene riempito prima

MAX_CHIAMATE = int(os.environ.get('MAX_CHIAMATE', 90000))
# giacenza pubblicata al massimo 50 («50+»): i file dei feed sono pubblici e la giacenza esatta non va mostrata.
# Clerk e Meta usano solo «> 0» / «== 0», che non cambiano.
GIACENZA_MAX = 50  # Storeden: 100.000 / 24 h per chiave
GIORNI_SU_ORDINAZIONE = 14     # se il prodotto non indica i giorni di consegna
GIORNI_CATEGORIE = 7           # ogni quanto rileggere albero e nomi delle categorie
GIORNI_TRADUZIONI = 30         # ogni quanto rileggere comunque una traduzione (correzioni fatte in Storeden)
THREAD_API = 6
PASSAGGI_ELENCO = 2            # letture dell'elenco unite (paginated_list salta qualche prodotto)
MAX_RICONTROLLI = 3000         # prodotti assenti dall'elenco ricontrollati con la scheda, per giro
# Cambi fatti via API che non toccano update_at (es. PUT products/category.json,
# CHG-427 del tema) non farebbero mai rileggere la scheda: ogni giro si rileggono
# anche le schede lette da più tempo, fino a ROTAZIONE_SCHEDE.
ROTAZIONE_SCHEDE = int(os.environ.get('ROTAZIONE_SCHEDE', 2000))
# Immagini: Google rifiuta oltre 64 megapixel (alcune importazioni hanno foto
# 8268×8268) e sotto i 100 px; sotto 500 px le segnala. Le dimensioni si leggono
# dall'intestazione del file (Range) e restano in cache: le immagini Storeden
# non cambiano mai a parità di id. Non contano nel budget API.
MAX_MISURE = int(os.environ.get('MAX_MISURE', 40000))
THREAD_IMMAGINI = 16
MISURE = {}                    # id immagine → [larghezza, altezza, formato]   # le categorie arrivano già dalla mappa notturna

# Categoria Google (tassonomia ufficiale). Le regole sono provate in ordine:
# la prima che corrisponde vince. «nome» cerca nel titolo italiano,
# «cat» nel percorso della categoria italiana. Tutto in minuscolo.
REGOLE_CATEGORIA = [
    # ricambi e parti esterne prima dei prodotti finiti
    ({'nome': r'^(parte esterna|parti esterne|parti ext|parti est|set esterno|cartuccia|ricambio|kit ricambio|vitone|o-ring|guarnizione)'}, 1489),  # Faucet Accessories
    ({'nome': r'^(maniglia|leva|manopola|comando remoto)'}, 8116),          # Faucet Handles & Controls
    ({'nome': r'^(aeratore|rompigetto)'}, 8115),                            # Faucet Aerators
    ({'nome': r'^(soffione|doccetta|doccino)'}, 581),                       # Shower Heads
    ({'nome': r'^(asta saliscendi|saliscendi|colonna doccia|set doccia|supporto doccia|braccio)'}, 2088),  # Shower Columns
    ({'nome': r'^(flessibile|presa acqua|raccordo)'}, 504638),              # Shower Arms & Connectors
    ({'nome': r'^(piletta|sifone|scarico|canalina|griglia)'}, 504637),      # Drain Components
    ({'nome': r'^(placca|pulsante)'}, 2478),                                # Toilet Trim
    ({'nome': r'^(sedile|copriwater)'}, 1865),                              # Toilet & Bidet Seats
    ({'nome': r'^(porta salviett|portasalviett|porta asciugaman|anello|barra)'}, 586),  # Towel Racks
    ({'nome': r'^(porta sapone|portasapone|dispenser)'}, 4971),             # Soap Dispensers
    ({'nome': r'^(porta scopino|porta rotolo|porta bicchiere|appendino|gancio|mensola)'}, 574),  # Bathroom Accessories
    ({'nome': r'^(specchio)'}, 595),                                        # Mirrors
    ({'nome': r'^(box doccia|parete doccia|porta doccia|cerniera|morsetto)'}, 1779),  # Shower Doors & Enclosures
    # tutto il resto della rubinetteria è un rubinetto, anche se il nome dice
    # «lavabo» o «vasca» (Gessi: «Lavabo a parete bocca lunga» = miscelatore)
    ({'cat': r'rubinetteria'}, 2032),                                       # Faucets
    ({'nome': r'^(miscelatore|mix |gruppo|batteria|rubinetto|bocca|termostatico)'}, 2032),
    ({'nome': r'\bpiatto doccia\b', 'cat': r'piatti doccia'}, 4728),       # Shower Bases
    ({'nome': r'^(cassetta)'}, 5665),                                       # Toilet Tanks
    ({'nome': r'\bvasca\b'}, 1636),                                        # Bathtubs
    ({'nome': r'\bbidet\b'}, 2376),
    ({'nome': r'\b(vaso|wc)\b'}, 1921),
    ({'nome': r'\b(lavabo|lavandino|lavamani)\b', 'cat': r'lavabi'}, 2886),  # Bathroom Sinks
    ({'nome': r'\blavello\b'}, 2757),
    ({'cat': r'termoarredi', 'nome': r'\b(radiatore|termoarredo|scaldasalviette)\b'}, 7110),  # non esiste «radiatori»
    ({'cat': r'fughe', 'nome': r'^(stucco|fuga)|ultracolor|kerapoxy'}, 499876),  # Grout
    ({'cat': r'collanti', 'nome': r'^(collante|adesivo|colla)'}, 503742),   # Glue & Adhesives
    ({'cat': r'utensili'}, 2181),                                           # Masonry Tools
    ({'cat': r'arredo bagno > arredo'}, 2081),                              # Bathroom Vanities
    ({'cat': r'arredo bagno > sanitari'}, 2062),                            # Toilets & Bidets
    ({'cat': r'^rivestimenti', 'nome': r'\b(listello|fascia|decoro)\b'}, 7136),  # Wall & Ceiling Tile
    ({'cat': r'^pavimenti'}, 2826),                                         # Flooring & Carpet
    ({'nome': r'(gres|piastrell|mosaico|battiscopa|\bserie\b|\d+([.,]\d+)?\s*[x×]\s*\d+([.,]\d+)?\s*cm)'}, 2826),
]


# ---------------------------------------------------------------- utilità

def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def contesto_ssl():
    for f in ('/etc/ssl/cert.pem', '/etc/ssl/certs/ca-certificates.crt'):
        if os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()


SSL = contesto_ssl()


def testo(s):
    """HTML → testo semplice su una riga."""
    s = html.unescape(s or '')
    s = re.sub(r'(?i)<br\s*/?>|</p>|</li>|</div>', '. ', s)
    s = re.sub(r'<[^>]+>', ' ', s)
    s = re.sub(r'\s*\.\s*(\.\s*)+', '. ', s)
    return re.sub(r'\s+', ' ', s).strip(' .')


def spazi(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def numero(v):
    try:
        return float(str(v).replace(',', '.'))
    except (TypeError, ValueError):
        return None


def ean_valido(v):
    s = re.sub(r'\D', '', str(v or ''))
    if len(s) not in (8, 12, 13, 14) or set(s) == {'0'}:
        return None
    cifre = [int(c) for c in s]
    controllo = cifre.pop()
    somma = sum(c * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(cifre)))
    return s if (10 - somma % 10) % 10 == controllo else None


def m2_confezione(*testi):
    for t in testi:
        m = re.search(r'(?:confezione|pacco contiene|pacco|scatola)(?:[^0-9]{0,15}|[^.]{0,40}?equivalenti a\s*)([0-9]+[.,]?[0-9]*)\s*(?:m2|m²|mq)', t or '', re.I)
        if m:
            v = numero(m.group(1))
            if v and 0.05 <= v <= 20:
                return v
    return None


# ---------------------------------------------------------------- API Storeden

class Stop(Exception):
    """Budget di chiamate finito o limite Storeden (509) raggiunto."""


def credenziali():
    k, e = os.environ.get('STOREDEN_KEY'), os.environ.get('STOREDEN_EXCHANGE')
    f = os.path.join(QUI, '.chiave-storeden.txt')
    if not (k and e) and os.path.exists(f):
        for riga in open(f, encoding='utf-8'):
            m = re.match(r'\s*(key|exchange)\s*[:=]\s*(\S+)', riga, re.I)
            if m:
                if m.group(1).lower() == 'key':
                    k = m.group(2)
                else:
                    e = m.group(2)
    if not (k and e):
        sys.exit('Chiave API mancante: imposta STOREDEN_KEY/STOREDEN_EXCHANGE o crea .chiave-storeden.txt.')
    return k, e


class Storeden:
    def __init__(self, massimo=MAX_CHIAMATE):
        self.key, self.exchange = credenziali()
        self.chiamate, self.massimo = 0, massimo
        self.fermo = None          # motivo dello stop, se c'è
        self.lock = threading.Lock()

    def get(self, percorso, **parametri):
        url = API + percorso + ('?' + urllib.parse.urlencode(parametri) if parametri else '')
        for t in range(6):
            with self.lock:
                if self.fermo:
                    raise Stop(self.fermo)
                if self.chiamate >= self.massimo:
                    self.fermo = f'budget di {self.massimo} chiamate esaurito'
                    raise Stop(self.fermo)
                self.chiamate += 1
            req = urllib.request.Request(url, headers={'key': self.key, 'exchange': self.exchange})
            try:
                with urllib.request.urlopen(req, timeout=60, context=SSL) as r:
                    return json.loads(r.read().decode() or 'null')
            except urllib.error.HTTPError as e:
                if e.code == 509:
                    with self.lock:
                        self.fermo = 'limite Storeden di 100.000 chiamate in 24 ore raggiunto (509)'
                    raise Stop(self.fermo)
                if e.code in (429, 500, 502, 503, 504) and t < 5:
                    time.sleep(3 * (t + 1))
                    continue
                raise RuntimeError(f'{percorso} {parametri}: HTTP {e.code} {e.read()[:200]!r}')
            except (urllib.error.URLError, TimeoutError):
                if t < 5:
                    time.sleep(3 * (t + 1))
                    continue
                raise


def in_parallelo(sd, compiti, funzione, nome):
    """Esegue funzione(compito) su più thread. Si ferma pulito su Stop.
    Ritorna (fatti, errori)."""
    q = queue.Queue()
    for c in compiti:
        q.put(c)
    fatti, errori, lock = [0], [], threading.Lock()

    def lavora():
        while not sd.fermo:
            try:
                c = q.get_nowait()
            except queue.Empty:
                return
            try:
                funzione(c)
                with lock:
                    fatti[0] += 1
                    if fatti[0] % 2000 == 0:
                        log(nome, fatti[0], 'di', len(compiti), '- chiamate', sd.chiamate)
            except Stop:
                return
            except Exception as e:
                with lock:
                    errori.append((c, str(e)[:200]))

    th = [threading.Thread(target=lavora) for _ in range(THREAD_API)]
    [t.start() for t in th]
    [t.join() for t in th]
    return fatti[0], errori


def base_da_api(p):
    """Dati base di un prodotto (stessi campi da paginated_list e product.json)."""
    return {
        'prezzo': numero(p.get('price')), 'prezzo_finale': numero(p.get('final_price')),
        'stato': str(p.get('status')), 'giacenza': numero(p.get('stock_count')) or 0,
        'foto': str(p.get('image_id') or '').strip(),
        'upd': (p.get('update_at') or {}).get('sec') or 0,
    }


def leggi_elenco(sd, limite):
    """uid → dati base sempre freschi (una chiamata ogni 500 prodotti).
    paginated_list salta ogni tanto qualche prodotto (l'elenco si sposta
    mentre lo si scorre: 2026-10-18 mancavano 34 prodotti in vendita), quindi
    lo si legge due volte e si uniscono i risultati (~150 chiamate in più)."""
    elenco = {}
    for giro in range(1 if limite else PASSAGGI_ELENCO):
        pagina, pagine, prima = 1, 1, len(elenco)
        while pagina <= pagine:
            j = sd.get('products/paginated_list.json', page=pagina, limit=500,
                       nodes='update_at,stock_count,image_id')
            pagine = j.get('total_pages') or 1
            for p in j.get('products', []):
                elenco[str(p['uid'])] = base_da_api(p)
            if limite and len(elenco) >= limite:
                return dict(list(elenco.items())[:limite])
            pagina += 1
        if giro:
            log(f'elenco, passaggio {giro + 1}: {len(elenco) - prima} prodotti in più')
    return elenco


def ricontrolla_mancanti(sd, elenco, prodotti, rapporto, extra=()):
    """Prodotti già nello stato ma assenti dall'elenco: prima di toglierli
    (e con loro le traduzioni) si rilegge la scheda. Se è attiva resta.
    `extra`: uid attivi secondo products/list.json che l'elenco a pagine ha
    saltato in tutti i passaggi (mai visti prima): si leggono allo stesso modo."""
    mancanti = [u for u in prodotti if u not in elenco]
    mancanti += [u for u in extra if u not in elenco and u not in prodotti]
    ripresi, eliminati, letti = [], [], set()

    def controlla(u):
        try:
            p = sd.get('products/product.json', uid=u)
        except RuntimeError as e:
            if 'Product not found' in str(e):      # eliminato da Storeden: va tolto
                eliminati.append(u)
                letti.add(u)
                return
            raise
        letti.add(u)
        if isinstance(p, dict) and p.get('status') is not None:
            elenco[u] = base_da_api(p)
            if elenco[u]['stato'] == '1':
                ripresi.append(u)
        else:
            eliminati.append(u)
    _, errori = in_parallelo(sd, mancanti[:MAX_RICONTROLLI], controlla, 'ricontrolli')
    # scheda non letta (errore, budget finito, oltre MAX_RICONTROLLI): il prodotto
    # resta com'era stanotte, si ricontrolla al prossimo giro
    tenuti = 0
    for u in mancanti:
        if u not in letti and u not in elenco and u in prodotti:
            vecchio = prodotti[u]
            elenco[u] = {'prezzo': vecchio.get('prezzo'), 'prezzo_finale': vecchio.get('prezzo_finale'),
                         'stato': '1' if vecchio.get('visibile') else '0', 'giacenza': vecchio.get('giacenza') or 0,
                         'foto': '', 'upd': vecchio.get('upd')}
            tenuti += 1
    if mancanti:
        log(f'assenti dall\'elenco: {len(mancanti)}, ancora attivi: {len(ripresi)}, '
            f'eliminati: {len(eliminati)}, non ricontrollati (tenuti): {tenuti}')
        rapporto['ricontrollati'] = {'assenti_elenco': len(mancanti), 'ancora_attivi': len(ripresi),
                                     'eliminati': len(eliminati), 'tenuti_senza_controllo': tenuti}
        if errori:
            rapporto['avvisi'].append(f'{len(errori)} ricontrolli non riusciti, es. {errori[:2]}')


def uid_attivi_da_lista(sd, rapporto):
    """products/list.json: tutti i prodotti in una chiamata (senza giacenza né
    data di modifica). Serve solo a trovare gli uid che l'elenco a pagine salta."""
    try:
        lista = sd.get('products/list.json')
    except Stop:
        raise
    except Exception as e:
        rapporto['avvisi'].append(f'products/list.json non letto: {str(e)[:150]}')
        return set()
    return {str(p['uid']) for p in lista or [] if isinstance(p, dict) and str(p.get('status')) == '1'}


def mappa_categorie(sd, albero):
    """uid → categorie, leggendo l'elenco prodotti di ogni categoria
    (~250 chiamate). PUT products/category.json non cambia update_at, quindi
    senza questa lettura i cambi di categoria non arriverebbero mai.
    Ritorna None se la lettura non è completa (in quel caso non si applica)."""
    mappa, lock = {}, threading.Lock()

    def leggi(cid):
        pagina, pagine = 1, 1
        while pagina <= pagine:
            j = sd.get('products/paginated_list.json', page=pagina, limit=500, categoryUID=cid)
            pagine = j.get('total_pages') or 1
            with lock:
                for p in j.get('products', []):
                    mappa.setdefault(str(p['uid']), set()).add(cid)
            pagina += 1
    fatti, errori = in_parallelo(sd, list(albero), leggi, 'categorie')
    return None if errori or fatti < len(albero) else mappa


def applica_categorie(r, cat_u, albero):
    """Categorie della scheda aggiornate con la mappa: stesse posizioni per
    quelle confermate (la prima decide product_type e categoria Google), le
    nuove in coda; restano anche quelle che la mappa non conosce."""
    vecchie = [c for c in r['categorie'] if c in cat_u or c not in albero]
    return vecchie + sorted(c for c in cat_u if c not in vecchie)


def normalizza_api(p):
    """Record da una scheda prodotto (campi verificati il 2026-10-16).
    Come nel tema: «Su ordinazione» = data.unlimited, che vince sulla
    giacenza; giorni di consegna = dimension.z; m² per confezione =
    dimension.w. Nessun dato riservato (es. purchase_price) entra nello stato."""
    # la galleria può contenere voci vuote («…/gallery/» senza id)
    immagini = []
    for iid in [p.get('image_id')] + [g.get('id') if isinstance(g, dict) else g for g in p.get('gallery') or []]:
        iid = str(iid or '').strip()
        if iid and IMG + iid not in immagini:
            immagini.append(IMG + iid)
    data = p.get('data') or {}
    dim = p.get('dimension') or {}
    categorie = []
    for c in p.get('categories') or []:
        cid = c.get('category_id') if isinstance(c, dict) else c
        if cid:
            categorie.append(str(cid))
    html_descr = p.get('description') or ''
    w = numero(dim.get('w'))
    return {
        'codice': spazi(str(p.get('sku') or p.get('code') or '')),
        'titolo': spazi(p.get('title')),
        'titolo_originale': p.get('title') or '',   # con gli spazi doppi, come lo vuole Clerk
        'descrizione': html_descr[:20000],     # HTML originale (Clerk lo vuole così)
        'privato': bool(p.get('privateProduct')),
        'illimitato': data.get('unlimited') in (True, 1, '1', 'true'),
        'giorni_consegna': numero(dim.get('z')),
        'immagini': immagini,
        'brand': p.get('brand_name'),
        'categorie': categorie,
        'ean': ean_valido(data.get('ean13')),
        'mpn': spazi(data.get('mpn')),
        'peso': numero(p.get('weight')),
        'spedizione_gratuita': numero(p.get('shipping_cost')) == -1,
        'w': w or 0, 'h': numero(dim.get('h')) or 0,
        # m² per confezione: dimension.w, altrimenti dalla descrizione («pacco contiene 1,9m²»)
        'm2': (w if w and 0.05 <= w <= 20 else None) or m2_confezione(testo(html_descr), p.get('title')),
    }


def misura_immagine(url):
    """[larghezza, altezza, formato] leggendo solo l'inizio del file."""
    dati = b''
    for fine in (65535, 524287):
        req = urllib.request.Request(url, headers={'Range': f'bytes=0-{fine}'})
        with urllib.request.urlopen(req, timeout=30, context=SSL) as r:
            dati = r.read()
        if dati[:8] == b'\x89PNG\r\n\x1a\n':
            w, h = struct.unpack('>II', dati[16:24])
            return [w, h, 'PNG']
        if dati[:4] == b'RIFF' and dati[8:12] == b'WEBP':
            return [0, 0, 'WEBP']                    # accettato da Google, misura non necessaria
        if dati[:3] == b'GIF':
            w, h = struct.unpack('<HH', dati[6:10])
            return [w, h, 'GIF']
        if dati[:2] != b'\xff\xd8':
            return [0, 0, (dati[:12].hex() or 'vuoto')]
        i = 2
        while i + 9 < len(dati):
            if dati[i] != 0xFF:
                i += 1
                continue
            m = dati[i + 1]
            if m in (0xC0, 0xC1, 0xC2, 0xC3):
                h, w = struct.unpack('>HH', dati[i + 5:i + 9])
                return [w, h, 'JPEG']
            if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                i += 2
                continue
            i += 2 + struct.unpack('>H', dati[i + 2:i + 4])[0]
        if len(dati) <= fine:                        # file finito senza SOF
            break
    return [0, 0, 'JPEG?']


def problema_immagine(url):
    """None se va bene (o non ancora misurata), altrimenti il motivo."""
    d = MISURE.get(url.rsplit('/', 1)[-1])
    if not d:
        return None
    w, h, fmt = d
    if fmt not in ('JPEG', 'PNG', 'GIF', 'WEBP'):
        return f'formato non supportato ({fmt})'
    if fmt == 'WEBP' or not w:
        return None
    if w * h > 64_000_000:
        return f'troppo grande ({w}x{h})'
    if min(w, h) < 100:
        return f'troppo piccola ({w}x{h})'
    return None


def scegli_immagini(r):
    """Per Google: prima la foto valida più adatta (≥500 px se c'è), poi le
    altre valide. Se nessuna è valida resta l'originale (Google la segnalerà,
    e il prodotto finisce in foto-da-sistemare.csv)."""
    valide = [i for i in r['immagini'] if not problema_immagine(i)]
    if not valide:
        return r['immagini']

    def piccola(i):
        d = MISURE.get(i.rsplit('/', 1)[-1])
        return bool(d and d[0] and min(d[0], d[1]) < 500)
    return sorted(valide, key=piccola)


def misura_immagini(prodotti, rapporto):
    """Misura le foto dei prodotti per Google non ancora in cache: prima la
    principale, poi (se quella non va) le altre dello stesso prodotto."""
    fatte, errori, lock = [0], [0], threading.Lock()

    def misura(url):
        try:
            d = misura_immagine(url)
        except Exception:
            with lock:
                errori[0] += 1
            return
        with lock:
            MISURE[url.rsplit('/', 1)[-1]] = d
            fatte[0] += 1
            if fatte[0] % 10000 == 0:
                log('immagini misurate', fatte[0])

    for giro in (0, 1):
        compiti = []
        for r in prodotti.values():
            if motivo_esclusione(r) or not r['immagini']:
                continue
            if giro == 0:
                lista = r['immagini'][:1]
            elif problema_immagine(r['immagini'][0]):
                lista = r['immagini'][1:11]
            else:
                continue
            compiti += [i for i in lista if i.rsplit('/', 1)[-1] not in MISURE]
        compiti = list(dict.fromkeys(compiti))[:max(0, MAX_MISURE - fatte[0])]
        q = queue.Queue()
        for c in compiti:
            q.put(c)

        def lavora():
            while True:
                try:
                    u = q.get_nowait()
                except queue.Empty:
                    return
                misura(u)
        th = [threading.Thread(target=lavora) for _ in range(THREAD_IMMAGINI)]
        [t.start() for t in th]
        [t.join() for t in th]
    rapporto['immagini_misurate'] = {'stanotte': fatte[0], 'errori': errori[0], 'in_cache': len(MISURE)}
    log('immagini misurate stanotte', fatte[0], 'errori', errori[0], 'in cache', len(MISURE))


def traduzione(sd, uid, tipo, campo, locale):
    j = sd.get('translations/translation.json', uid=uid, type=tipo, selector=campo, locale=locale)
    return j.get('value') if isinstance(j, dict) and j.get('found') else None


# ---------------------------------------------------------------- stato

def carica_stato():
    if os.path.exists(STATO):
        try:
            with gzip.open(STATO, 'rt', encoding='utf-8') as f:
                s = json.load(f)
            log('stato precedente:', len(s.get('prodotti', {})), 'prodotti')
            return s
        except Exception as e:
            log('stato illeggibile, riparto da zero:', e)
    return {}


def salva_stato(s):
    os.makedirs(os.path.dirname(STATO), exist_ok=True)
    with gzip.open(STATO + '.tmp', 'wt', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(STATO + '.tmp', STATO)


def percorsi(albero, nomi):
    """cid → «Padre > Figlio» con i nomi della lingua (italiano se manca)."""
    out = {}
    for cid in albero:
        parti, c = [], cid
        while c in albero and len(parti) < 8:
            parti.append(spazi(nomi.get(c) or albero[c]['nome']).rstrip('_ '))
            c = albero[c]['padre']
        out[cid] = ' > '.join(reversed(parti))
    return out


# ---------------------------------------------------------------- regole feed

def categoria_google(titolo_it, percorso_it):
    n, c = (titolo_it or '').lower(), (percorso_it or '').lower()
    for regola, gid in REGOLE_CATEGORIA:
        if ('nome' in regola and re.search(regola['nome'], n)) or ('cat' in regola and re.search(regola['cat'], c)):
            return gid
    return None


def pulisci_brand(b):
    b = spazi(str(b or '')).rstrip('_ ').strip()
    return '' if b.lower() in ('', 'unknown', 'none') else b


def motivo_esclusione(r):
    """Motivi validi per tutte le lingue (None = prodotto idoneo)."""
    if not r.get('visibile'):
        return 'nascosto o non disponibile'
    if not r['immagini']:
        return 'senza immagine'
    if not (r['prezzo_finale'] or r['prezzo']) or (r['prezzo_finale'] or r['prezzo']) <= 0:
        return 'prezzo 0 o mancante'
    if not r['codice']:
        return 'senza codice'
    # campioni da 10 € (codice «…_sample»): su Shopping toglierebbero clic alla piastrella vera
    if re.search(r'[_-]sample$', r['codice'], re.I) or re.match(r'campion[ei]\b', r['titolo'], re.I):
        return 'campione'
    return None


def da_clerk(r):
    """Prodotti nel feed Clerk: attivi, con codice, non campioni (come No Gravity)."""
    return r.get('visibile') and r['codice'] and not (
        re.search(r'[_-]sample$', r['codice'], re.I) or re.match(r'campion[ei]\b', r['titolo'], re.I))


# Tabella «Etichetta: | valore» delle descrizioni importate (Marazzi, Ragno,
# Tagina, Piemme, Gessi, Tarkett…): diventa attributi Clerk per filtri,
# faccette ed elementi («stessa serie», «pezzi speciali»), invece di farli
# indovinare ai modificatori AI dai nomi (che le importazioni hanno cambiato).
ETICHETTE_ATTRIBUTI = {'Serie': 'collezione', 'Collezione': 'collezione', 'Formato': 'formato',
                       'Spessore': 'spessore', 'Finitura': 'finitura', 'Effetto': 'effetto',
                       'Colore': 'colore', 'Bordo': 'bordo', 'Materiale': 'materiale',
                       'Tipologia': 'tipologia'}
RE_ATTRIBUTO = re.compile(r'(?:^|\|)\s*(' + '|'.join(ETICHETTE_ATTRIBUTI) + r')\s*:\s*\|[\s|]*([^|]{1,60}?)\s*(?=\|)')
# tipo_prodotto tradotto come nei filtri degli elementi Clerk esteri
# (es. «Pezzi speciali» EN: tipo_prodotto in ["Skirting","Special piece"])
TIPI_TRADOTTI = {
    'en': {'Piastrella': 'Tile', 'Mosaico': 'Mosaic', 'Decoro': 'Decor', 'Battiscopa': 'Skirting', 'Pezzo speciale': 'Special piece'},
    'es': {'Piastrella': 'Baldosa', 'Mosaico': 'Mosaico', 'Decoro': 'Decorado', 'Battiscopa': 'Rodapié', 'Pezzo speciale': 'Pieza especial'},
    'de': {'Piastrella': 'Fliese', 'Mosaico': 'Mosaik', 'Decoro': 'Dekor', 'Battiscopa': 'Sockelleiste', 'Pezzo speciale': 'Formteil'},
    'fr': {'Piastrella': 'Carreau', 'Mosaico': 'Mosaïque', 'Decoro': 'Décor', 'Battiscopa': 'Plinthe', 'Pezzo speciale': 'Pièce spéciale'},
}
MARCHI_PIASTRELLE = {'MARAZZI', 'RAGNO', 'TAGINA', 'PIEMME CERAMICHE', 'FLAVIKER', 'IDEA CERAMICA', 'LA FENICE'}
TIPI_PIASTRELLA = [(r'\bbattiscopa\b', 'Battiscopa'), (r'\bmosaic', 'Mosaico'),
                   (r'\b(decoro|decori|listello|fascia|inserto)\b', 'Decoro'),
                   (r'\b(gradino|angolo|angolare|torello|scalino|zoccolo|terminale|pezzo speciale|elemento)\b', 'Pezzo speciale')]


def attributi_da_descrizione(r):
    t = html.unescape(r.get('descrizione') or '')
    t = re.sub(r'<[^>]+>', '|', t)
    out = {}
    for etichetta, valore in RE_ATTRIBUTO.findall(t):
        chiave, valore = ETICHETTE_ATTRIBUTI[etichetta], spazi(valore).rstrip('.')
        if valore and chiave not in out:
            out[chiave] = valore
    marchio_piastrelle = pulisci_brand(r.get('brand')).upper() in MARCHI_PIASTRELLE
    if out.get('formato') and (re.match(r'(gres|ceramica|monocottura|bicottura|pasta|klinker|cotto)', out.get('materiale', '').lower())
                               or marchio_piastrelle and 'spessore' in out):
        nome = (r.get('titolo') or '').lower()
        out['tipo_prodotto'] = next((v for k, v in TIPI_PIASTRELLA if re.search(k, nome)), 'Piastrella')
    return out


def scrivi_clerk(l, prodotti, trad_l, cat, rapporto, cfg=None):
    """Clerk.io JSON Feed V2 con gli stessi campi e significati del file di
    No Gravity, su cui si basano modificatori, campagne e design di Clerk:
    - piastrelle (dimension.w > 0): price e list_price al m², sqmPrice al m²;
      confezioni grandi (dimension.h = 2): price della confezione, sqmPrice al m²
    - brand_name come in Storeden, compreso il «_» finale (es. GESSI_)
    - no_image con l'immagine segnaposto; available = su ordinazione o giacenza
    - description: HTML italiano in tutte le lingue, come No Gravity: i
      modificatori AI di Clerk (Marazzi, Ragno, Piemme…) leggono quel testo
    - brand vuoto = «Unknown»
    Nelle lingue estere il nome è tradotto; se manca la traduzione resta l'italiano."""
    loc, dominio, _ = LINGUE[l]
    out, in_italiano = [], 0
    for u, r in prodotti.items():
        if not da_clerk(r):
            continue
        t = trad_l.get(u) or {}
        if l != 'it' and not t.get('t'):
            in_italiano += 1
        nome = t.get('t') if l != 'it' and t.get('t') else r.get('titolo_originale') or r['titolo']
        finale = r['prezzo_finale'] or r['prezzo'] or 0
        pieno = r['prezzo'] or finale
        w, h = r.get('w') or 0, r.get('h') or 0
        if w > 0:
            mq = finale / w
            prezzo, listino = (finale, pieno) if h == 2 else (finale / w, pieno / w)
        else:
            mq, prezzo, listino = 0, finale, pieno
        out.append({
            'id': int(u), 'name': nome, 'description': r['descrizione'], 'price': prezzo, 'list_price': listino,
            'image': r['immagini'][0] if r['immagini'] else SEGNAPOSTO,
            'url': f'{dominio}/product/{u}', 'categories': [int(c) for c in r['categorie']],
            'created_at': r.get('visto') or 0, 'sku': r['codice'], 'stock': min(int(r['giacenza']), GIACENZA_MAX),
            'free_shipping': bool(r.get('spedizione_gratuita')), 'brand_name': r['brand'] or 'Unknown',
            'no_image': not r['immagini'], 'available': bool(r['illimitato'] or r['giacenza'] > 0),
            'sqmPrice': math.floor(mq * 100 + 0.5 + 1e-9) / 100,   # come No Gravity: mezzo centesimo per eccesso
        })
        attributi = attributi_da_descrizione(r)
        if 'tipo_prodotto' in attributi and l in TIPI_TRADOTTI:
            attributi['tipo_prodotto'] = TIPI_TRADOTTI[l].get(attributi['tipo_prodotto'], attributi['tipo_prodotto'])
        out[-1].update(attributi)
        if cfg and u in cfg:   # campi del configuratore pavimenti (configuratore.py)
            out[-1].update(cfg[u])
    figli = {}
    for cid, c in cat['albero'].items():
        figli.setdefault(c['padre'], []).append(int(cid))
    nomi, descr = cat.get('nomi', {}).get(l, {}), cat.get('descrizioni', {}).get(l, {})
    categorie = [{'id': int(cid), 'name': (nomi.get(cid) if l != 'it' else '') or c['nome'],
                  'description': (descr.get(cid) if l != 'it' else '') or c['descrizione'],
                  'url': f'{dominio}/shop/category/{cid}', 'subcategories': figli.get(cid, [])}
                 for cid, c in cat['albero'].items() if not c['disattivata']]
    os.makedirs(os.path.join(OUT, 'clerk'), exist_ok=True)
    for nome_file, dati in ((f'products-{loc}.json', out), (f'categories-{loc}.json', categorie)):
        f = os.path.join(OUT, 'clerk', nome_file)
        with open(f + '.tmp', 'w', encoding='utf-8') as fh:
            json.dump(dati, fh, ensure_ascii=False, separators=(',', ':'))
        os.replace(f + '.tmp', f)
    rapporto.setdefault('clerk', {})[l] = {'prodotti': len(out), 'categorie': len(categorie),
                                           'testo_italiano_per_traduzione_mancante': in_italiano if l != 'it' else 0}
    log('clerk', loc, len(out), 'prodotti,', len(categorie), 'categorie')


def costruisci_item(uid, r, l, trad_l, percorsi_l, percorsi_it, oggi, motivi):
    """Ritorna (xml dell'item, dati per il rapporto) oppure None se escluso."""
    def escludi(motivo):
        motivi[motivo] = motivi.get(motivo, 0) + 1
        return None

    m = motivo_esclusione(r)
    if m:
        return escludi(m)
    if l == 'it':
        titolo, descr = r['titolo'], testo(r['descrizione'])
    else:
        t = trad_l.get(uid)
        if not t:
            return escludi('traduzione non ancora letta')
        if not t.get('t'):
            return escludi('non tradotto in Storeden')
        titolo, descr = spazi(t['t']), t.get('d') or ''

    _, dominio, valuta = LINGUE[l]
    finale, pieno = r['prezzo_finale'] or r['prezzo'], r['prezzo'] or r['prezzo_finale']
    cat_id = r['categorie'][0] if r['categorie'] else None
    percorso_it = percorsi_it.get(cat_id, '') if cat_id else ''
    percorso_l = percorsi_l.get(cat_id, '') if cat_id else ''
    gcat = categoria_google(r['titolo'], percorso_it)

    if r['illimitato']:
        giorni = int(r['giorni_consegna'] or GIORNI_SU_ORDINAZIONE)
        disp, data_disp = 'backorder', (oggi + datetime.timedelta(days=giorni)).strftime('%Y-%m-%dT00:00+01:00')
    elif r['giacenza'] > 0:
        disp, data_disp = 'in_stock', None
    else:
        disp, data_disp = 'out_of_stock', None

    brand = pulisci_brand(r['brand'])
    immagini = scegli_immagini(r)
    c = [
        ('g:id', r['codice']),
        ('g:title', titolo[:150]),
        ('g:description', (descr or titolo)[:5000]),
        ('g:link', f'{dominio}/product/{uid}'),
        ('g:image_link', immagini[0]),
    ]
    c += [('g:additional_image_link', i) for i in immagini[1:11]]
    c += [('g:condition', 'new'), ('g:availability', disp)]
    if data_disp:
        c.append(('g:availability_date', data_disp))
    if pieno > finale + 0.009:
        c += [('g:price', f'{pieno:.2f} {valuta}'), ('g:sale_price', f'{finale:.2f} {valuta}')]
    else:
        c.append(('g:price', f'{finale:.2f} {valuta}'))
    if r['m2']:
        c += [('g:unit_pricing_measure', f"{r['m2']:g}sqm"), ('g:unit_pricing_base_measure', '1sqm')]
    if brand:
        c.append(('g:brand', brand))
    if r['ean']:
        c.append(('g:gtin', r['ean']))
    elif brand:
        c.append(('g:mpn', r.get('mpn') or r['codice']))
    else:
        c.append(('g:identifier_exists', 'no'))
    if gcat:
        c.append(('g:google_product_category', str(gcat)))
    if percorso_l:
        c.append(('g:product_type', percorso_l))
    if r['peso'] and r['peso'] > 0:
        c.append(('g:shipping_weight', f"{r['peso']:g} kg"))
    c += [
        ('g:custom_label_0', brand or 'senza brand'),
        ('g:custom_label_1', percorso_it.split(' > ')[0] if percorso_it else 'senza categoria'),
        ('g:custom_label_2', disp),
        ('g:custom_label_3', 'sotto 50' if finale < 50 else 'sotto 200' if finale < 200 else 'sotto 1000' if finale < 1000 else 'oltre 1000'),
    ]
    # quantità per Meta (Shops): un commento che scrivi_feed trasforma nel campo Meta
    # e toglie dal feed Google; su ordinazione = 999, esaurito = 0
    quantita = 999 if disp == 'backorder' else min(int(max(r['giacenza'], 0)), GIACENZA_MAX) if disp == 'in_stock' else 0
    xml = f'<item><!--q:{quantita}-->' + ''.join(f'<{k}>{escape(str(v))}</{k}>' for k, v in c) + '</item>\n'
    return xml, {'disp': disp, 'gcat': gcat, 'ean': bool(r['ean']), 'm2': bool(r['m2']), 'peso': bool(r['peso'])}


def scrivi_feed(l, items):
    """Feed Google (feed-<l>.xml.gz) e Meta (feed-meta-<l>.xml.gz), solo
    compressi: GitHub Pages ha un limite di 1 GB e i feed Clerk pesano già
    ~90 MB per lingua. Google e Meta leggono i .gz senza problemi.
    Meta non conosce «backorder»: i prodotti su ordinazione sono ordinabili,
    quindi lì risultano «in stock» (senza availability_date)."""
    _, dominio, _ = LINGUE[l]
    testa = ('<?xml version="1.0" encoding="utf-8"?>\n<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0"><channel>\n'
             f'<title>Universo Ceramiche</title><link>{dominio}</link><description>Feed prodotti {l.upper()}</description>\n')
    coda = '</channel></rss>\n'
    meta_disp = re.compile(r'<g:availability>backorder</g:availability><g:availability_date>[^<]*</g:availability_date>')
    quantita = re.compile(r'<!--q:(\d+)-->')

    def per_meta(i):
        i = meta_disp.sub('<g:availability>in stock</g:availability>', i)
        return quantita.sub(r'<g:quantity_to_sell_on_facebook>\1</g:quantity_to_sell_on_facebook>', i)
    for nome, righe in ((f'feed-{l}.xml.gz', (quantita.sub('', i) for i in items)),
                        (f'feed-meta-{l}.xml.gz', (per_meta(i) for i in items))):
        percorso = os.path.join(OUT, nome)
        with gzip.open(percorso + '.tmp', 'wt', encoding='utf-8', compresslevel=6) as f:
            f.write(testa)
            f.writelines(righe)
            f.write(coda)
        os.replace(percorso + '.tmp', percorso)
    vecchio = os.path.join(OUT, f'feed-{l}.xml')       # versione non compressa dei giri precedenti
    if os.path.exists(vecchio):
        os.remove(vecchio)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--campione', action='store_true', help='salva il JSON di un prodotto API ed esce')
    ap.add_argument('--limite', type=int, default=0, help='solo i primi N prodotti (prova)')
    ap.add_argument('--lingue', default='it,en,de,es,fr')
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    lingue = [l.strip() for l in a.lingue.split(',') if l.strip() in LINGUE]
    inizio = time.time()
    sd = Storeden()

    if a.campione:
        uid = next(iter(leggi_elenco(sd, 1)))
        p = sd.get('products/product.json', uid=uid, nodes='categories_name,brand_name,gallery')
        f = os.path.join(OUT, 'campione-prodotto.json')
        json.dump(p, open(f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        log('salvato', f, '→ campi:', ', '.join(sorted(p)))
        log('normalizzato:', json.dumps(normalizza_api(p), ensure_ascii=False)[:1500])
        return

    rapporto = {'generato': datetime.datetime.now().isoformat(timespec='seconds'),
                'fonte': 'API Storeden', 'lingue': {}, 'avvisi': []}
    stato = carica_stato()
    prodotti = stato.setdefault('prodotti', {})
    trad = stato.setdefault('traduzioni', {})
    cat = stato.setdefault('categorie', {})

    # 1) elenco completo: prezzi, giacenze, stato, foto, data di modifica
    elenco = leggi_elenco(sd, a.limite)
    log('prodotti in Storeden:', len(elenco), '- chiamate', sd.chiamate)
    if not a.limite:
        attivi_lista = uid_attivi_da_lista(sd, rapporto)
        saltati = [u for u in attivi_lista if u not in elenco]
        log('products/list.json:', len(attivi_lista), 'attivi, saltati dall\'elenco a pagine:', len(saltati))
        rapporto['saltati_elenco_pagine'] = len(saltati)
        ricontrolla_mancanti(sd, elenco, prodotti, rapporto, extra=saltati)
    # prodotti attivi: con foto vanno su Google e Clerk, senza foto solo su Clerk
    candidati = [u for u, e in elenco.items() if e['stato'] == '1']
    if not a.limite:
        tenere = set(candidati)
        for u in list(prodotti):
            if u not in tenere:
                prodotti.pop(u)
                for l in trad.values():
                    l.pop(u, None)

    # 2) categorie: albero e nomi tradotti, riletti una volta a settimana
    if not cat.get('albero') or time.time() - cat.get('letto', 0) > GIORNI_CATEGORIE * 86400:
        lista = sd.get('categories/list.json')
        cat.update(albero={str(c['uid']): {'nome': c['name'], 'padre': str(c.get('parentUID') or c.get('parent') or 0),
                                           'descrizione': c.get('description') or '', 'disattivata': bool(c.get('disabled'))}
                           for c in lista},
                   nomi={}, descrizioni={}, letto=time.time())
    nomi = cat.setdefault('nomi', {})
    descrizioni = cat.setdefault('descrizioni', {})
    compiti = [(l, cid) for l in ORDINE_TRADUZIONI if l in lingue for cid in cat['albero'] if cid not in nomi.get(l, {})]

    def leggi_nome(c):
        l, cid = c
        loc = LINGUE[l][0]
        v = traduzione(sd, cid, 'category', 'title', loc)
        d = traduzione(sd, cid, 'category', 'description', loc) if v else None
        descrizioni.setdefault(l, {})[cid] = d or ''
        nomi.setdefault(l, {})[cid] = spazi(v) if v else ''
    in_parallelo(sd, compiti, leggi_nome, 'categorie tradotte')

    # mappa uid → categorie, letta prima delle schede (poche chiamate) e
    # applicata dopo, così vale anche per le schede appena rilette
    mappa = mappa_categorie(sd, cat['albero']) if not a.limite else None

    # 3) schede complete solo per i prodotti nuovi o modificati (i nuovi prima)
    da_leggere = sorted((u for u in candidati if prodotti.get(u, {}).get('upd') != elenco[u]['upd']),
                        key=lambda u: (u in prodotti, not elenco[u]['foto']))
    # rotazione: dopo le nuove/modificate, le schede lette da più tempo (i record
    # vecchi senza 'letta' vengono per primi), così entrano anche i cambi che non
    # toccano update_at (categorie via API). Prima delle traduzioni.
    gia = set(da_leggere)
    rotazione = sorted((u for u in candidati if u in prodotti and u not in gia),
                       key=lambda u: prodotti[u].get('letta', 0))[:ROTAZIONE_SCHEDE if not a.limite else 0]
    log('schede da leggere:', len(da_leggere), '+ rotazione', len(rotazione))
    da_leggere += rotazione

    def leggi_scheda(u):
        p = sd.get('products/product.json', uid=u, nodes='categories_name,brand_name,gallery')
        r = normalizza_api(p)
        r['upd'] = elenco[u]['upd']
        r['letta'] = int(time.time())
        vecchio = prodotti.get(u, {})
        r['visto'] = vecchio.get('visto') or int(time.time())   # prima volta visto (created_at Clerk)
        # testo italiano invariato (es. cambio prezzo da un'importazione, o
        # rilettura per rotazione): le traduzioni già lette restano valide
        if vecchio and vecchio.get('titolo_originale') == r['titolo_originale'] and vecchio.get('descrizione') == r['descrizione']:
            for tl in trad.values():
                if tl.get(u, {}).get('upd') == vecchio.get('upd'):
                    tl[u]['upd'] = r['upd']
        elif vecchio:
            # testo cambiato anche se update_at no: traduzioni da rileggere
            for tl in trad.values():
                if u in tl:
                    tl[u]['upd'] = None
        prodotti[u] = r
    letti, errori = in_parallelo(sd, da_leggere, leggi_scheda, 'schede')
    rapporto['schede_lette'] = letti
    if errori:
        rapporto['avvisi'].append(f'{len(errori)} schede non lette, es. {errori[:3]}')
    if len(errori) > max(50, (letti + len(errori)) * 0.05):
        sys.exit(f'Troppi errori API ({len(errori)}): feed non aggiornato.')

    # categorie fresche per tutti i prodotti (anche quelli non riletti)
    if not a.limite:
        if mappa is None:
            rapporto['avvisi'].append('Mappa categorie incompleta: categorie lasciate come nelle schede.')
        else:
            cambiate = 0
            for u, r in prodotti.items():
                if u in mappa:
                    nuove = applica_categorie(r, mappa[u], cat['albero'])
                    if nuove != r['categorie']:
                        r['categorie'] = nuove
                        cambiate += 1
            rapporto['categorie_aggiornate_da_mappa'] = cambiate
            log('categorie aggiornate dalla mappa:', cambiate)

    # dati che cambiano spesso: sempre dall'elenco di stanotte
    for u, r in prodotti.items():
        e = elenco.get(u)
        if e:
            r.update(prezzo=e['prezzo'], prezzo_finale=e['prezzo_finale'], giacenza=e['giacenza'],
                     visibile=e['stato'] == '1' and not r.get('privato'))

    # 3b) file di partenza (seed/traduzioni-<locale>.json.gz, {sku: [titolo, descrizione]}),
    #     esportati dall'app Storeden «Traduzioni catalogo»: riempiono in un colpo le
    #     traduzioni mai lette (il primo riempimento via API richiede notti).
    #     Un titolo uguale all'italiano vuol dire «non tradotto»: si lascia alle API.
    rapporto['traduzioni_da_seed'] = {}
    for l in ORDINE_TRADUZIONI:
        f = os.path.join(QUI, 'seed', f'traduzioni-{LINGUE[l][0]}.json.gz')
        if l not in lingue or not os.path.exists(f):
            continue
        with gzip.open(f, 'rt', encoding='utf-8') as fh:
            seed = json.load(fh)
        tl, n = trad.setdefault(l, {}), 0
        for u, r in prodotti.items():
            v = seed.get(r['codice'])
            if u in tl or not v or not spazi(v[0]) or spazi(v[0]) == r['titolo']:
                continue
            tl[u] = {'t': spazi(v[0]), 'd': testo(v[1])[:5000] if len(v) > 1 and v[1] else '',
                     'upd': r['upd'], 'letta': int(time.time())}
            n += 1
        rapporto['traduzioni_da_seed'][l] = n
        log('traduzioni da seed', l, n)

    # 4) traduzioni dei prodotti nuovi o con scheda cambiata. Ordine: prima i
    #    prodotti per Google in tutte le lingue (titolo + descrizione), poi
    #    quelli solo per Clerk (solo titolo: Clerk usa la descrizione italiana)
    lingue_tr = [l for l in ORDINE_TRADUZIONI if l in lingue]
    compiti = []
    for per_google in (True, False):
        for l in lingue_tr:
            tl = trad.setdefault(l, {})
            compiti += [(l, u, per_google) for u, r in prodotti.items()
                        if da_clerk(r) and (not motivo_esclusione(r)) == per_google
                        and (tl.get(u, {}).get('upd') != r['upd']
                             or time.time() - tl[u].get('letta', time.time()) > GIORNI_TRADUZIONI * 86400)]
    log('traduzioni da leggere:', len(compiti))
    contatori = {l: 0 for l in lingue_tr}
    lock_tr = threading.Lock()

    def leggi_traduzione(c):
        l, u, per_google = c
        loc = LINGUE[l][0]
        t = traduzione(sd, u, 'product', 'title', loc)
        d = traduzione(sd, u, 'product', 'description', loc) if t and per_google else None
        trad[l][u] = {'t': spazi(t) if t else '', 'd': testo(d)[:5000] if d else '',   # d solo per Google
                      'upd': prodotti[u]['upd'], 'letta': int(time.time())}
        with lock_tr:
            contatori[l] += 1
    fatti, err = in_parallelo(sd, compiti, leggi_traduzione, 'traduzioni')
    rapporto['traduzioni_lette'] = contatori
    rapporto['traduzioni_mancanti'] = len(compiti) - fatti
    if err:
        rapporto['avvisi'].append(f'{len(err)} traduzioni non lette, es. {err[:2]}')

    if sd.fermo:
        rapporto['avvisi'].append(f'Letture interrotte: {sd.fermo}. Il resto si completa nei prossimi giri.')
    # 4b) dimensioni delle foto (fuori dal budget API), con cache nello stato
    MISURE.update(stato.get('misure_immagini', {}))
    misura_immagini(prodotti, rapporto)
    stato['misure_immagini'] = MISURE
    if not a.limite:
        salva_stato(stato)
    rapporto['chiamate_api'] = sd.chiamate

    # prodotti per Google senza nessuna foto valida: elenco da sistemare in Storeden
    da_sistemare = []
    for u, r in prodotti.items():
        if motivo_esclusione(r):
            continue
        problemi = [problema_immagine(i) for i in r['immagini']]
        if problemi and all(problemi):
            da_sistemare.append((r['codice'], u, problemi[0], r['immagini'][0]))
    with open(os.path.join(OUT, 'foto-da-sistemare.csv'), 'w', encoding='utf-8') as f:
        f.write('codice;id;problema;immagine\n')
        f.writelines(f'{c};{u};{p};{i}\n' for c, u, p, i in sorted(da_sistemare))
    rapporto['foto_da_sistemare'] = len(da_sistemare)

    # 5) feed per lingua
    albero = cat['albero']
    percorsi_it = percorsi(albero, {})
    visti, doppi = set(), set()
    for r in prodotti.values():
        (doppi if r['codice'] in visti else visti).add(r['codice'])
    if doppi:
        rapporto['avvisi'].append(f'{len(doppi)} codici usati da più prodotti (tenuto il primo), es. {sorted(doppi)[:5]}')

    # campi del configuratore pavimenti per Clerk (lavoro, effetto, tonalità, foto…)
    cfg = {}
    try:
        sys.path.insert(0, QUI)
        import configuratore
        cfg, rapporto['configuratore'] = configuratore.calcola(prodotti, albero, attributi_da_descrizione, log, contesto_ssl())
    except Exception as e:
        rapporto['avvisi'].append(f'configuratore non calcolato: {e}')
        log('configuratore non calcolato:', e)

    oggi = datetime.date.today()
    for l in lingue:
        items, motivi, stat, usati = [], {}, [], set()
        percorsi_l = percorsi_it if l == 'it' else percorsi(albero, nomi.get(l, {}))
        for u, r in prodotti.items():
            if r['codice'] in usati:
                motivi['codice doppio'] = motivi.get('codice doppio', 0) + 1
                continue
            res = costruisci_item(u, r, l, trad.get(l, {}), percorsi_l, percorsi_it, oggi, motivi)
            if res:
                usati.add(r['codice'])
                items.append(res[0])
                stat.append(res[1])
        scrivi_feed(l, items)
        n = len(stat) or 1
        rapporto['lingue'][l] = {
            'file': f'feed-{l}.xml.gz', 'prodotti': len(items), 'esclusi': motivi,
            'disponibilita': {d: sum(1 for s in stat if s['disp'] == d) for d in ('in_stock', 'backorder', 'out_of_stock')},
            'con_categoria_google_%': round(100 * sum(1 for s in stat if s['gcat']) / n, 1),
            'con_ean_%': round(100 * sum(1 for s in stat if s['ean']) / n, 1),
            'con_m2_%': round(100 * sum(1 for s in stat if s['m2']) / n, 1),
            'con_peso_%': round(100 * sum(1 for s in stat if s['peso']) / n, 1),
        }
        log(l, len(items), 'prodotti; esclusi', motivi)
        scrivi_clerk(l, prodotti, trad.get(l, {}), cat, rapporto, cfg)

    rapporto['durata_minuti'] = round((time.time() - inizio) / 60, 1)
    json.dump(rapporto, open(os.path.join(OUT, 'rapporto.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    righe = ''.join(f'<li><a href="feed-{l}.xml.gz">feed-{l}.xml.gz</a> (Google) · <a href="feed-meta-{l}.xml.gz">feed-meta-{l}.xml.gz</a> (Meta) — {v["prodotti"]} prodotti · Clerk: '
                    f'<a href="clerk/products-{LINGUE[l][0]}.json">prodotti</a>, <a href="clerk/categories-{LINGUE[l][0]}.json">categorie</a></li>'
                    for l, v in rapporto['lingue'].items())
    open(os.path.join(OUT, 'index.html'), 'w', encoding='utf-8').write(
        '<!doctype html><meta charset="utf-8"><meta name="robots" content="noindex"><title>Feed UC</title>'
        f'<h1>Feed Universo Ceramiche</h1><p>Generato {rapporto["generato"]}</p><ul>{righe}</ul>'
        '<p><a href="rapporto.json">rapporto.json</a> · <a href="foto-da-sistemare.csv">foto-da-sistemare.csv</a></p>')
    log('fatto in', rapporto['durata_minuti'], 'minuti, chiamate', sd.chiamate)


if __name__ == '__main__':
    main()
