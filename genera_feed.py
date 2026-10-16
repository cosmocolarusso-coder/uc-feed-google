#!/usr/bin/env python3
"""Feed prodotti Google Merchant Center per Universo Ceramiche.

Fonti:
  - API Storeden (prezzi a confezione, giacenza, stato, EAN, peso, immagini)
  - file Clerk pubblici di No Gravity su S3 (titoli, descrizioni, link e
    categorie tradotte nelle altre lingue)

Uso:
  python3 genera_feed.py                  feed completo da API + Clerk
  python3 genera_feed.py --solo-clerk     anteprima senza chiave API
  python3 genera_feed.py --campione       salva il JSON grezzo di un prodotto API
  python3 genera_feed.py --limite 200     prova su pochi prodotti
  python3 genera_feed.py --lingue it,fr   solo alcune lingue
  python3 genera_feed.py --da-cache       rigenera dai dati API dell'ultimo giro
                                          (out/grezzi.json.gz), senza chiamate

Chiave API: variabili d'ambiente STOREDEN_KEY e STOREDEN_EXCHANGE, oppure il
file .chiave-storeden.txt accanto allo script con le righe «key: ...» e
«exchange: ...». Basta una chiave di sola lettura.

Uscita in out/: feed-<lingua>.xml, rapporto.json, index.html.
"""
import argparse, datetime, gzip, html, json, os, queue, re, ssl, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from xml.sax.saxutils import escape

QUI = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(QUI, 'out')
API = 'https://connect.storeden.com/v1.1/'
S3 = 'https://universoceramiche.s3.eu-south-1.amazonaws.com/'
IMG = 'https://egress.storeden.net/gallery/'

LINGUE = {
    # lingua: (locale Clerk, dominio, valuta)
    'it': ('it_IT', 'https://universoceramiche.it', 'EUR'),
    'en': ('en_US', 'https://universoceramiche.com', 'EUR'),
    'de': ('de_DE', 'https://universoceramiche.de', 'EUR'),
    'es': ('es_ES', 'https://universoceramiche.es', 'EUR'),
    'fr': ('fr_FR', 'https://universoceramiche.fr', 'EUR'),
}

GIORNI_SU_ORDINAZIONE = 14   # se il prodotto non indica packageTime
THREAD_API = 6

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


def scarica(url, tentativi=4):
    for t in range(tentativi):
        try:
            with urllib.request.urlopen(url, timeout=180, context=SSL) as r:
                return r.read()
        except Exception as e:
            if t == tentativi - 1:
                raise
            log('riprovo', url, e)
            time.sleep(5 * (t + 1))


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


def prendi(d, *chiavi):
    """Primo valore non vuoto tra più chiavi possibili (anche «a.b»)."""
    for k in chiavi:
        v = d
        for p in k.split('.'):
            v = v.get(p) if isinstance(v, dict) else None
        if v not in (None, '', [], {}):
            return v
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
        sys.exit('Chiave API mancante: imposta STOREDEN_KEY/STOREDEN_EXCHANGE '
                 'o crea .chiave-storeden.txt (oppure usa --solo-clerk).')
    return k, e


class Storeden:
    def __init__(self):
        self.key, self.exchange = credenziali()
        self.chiamate = 0
        self.lock = threading.Lock()

    def get(self, percorso, **parametri):
        url = API + percorso + ('?' + urllib.parse.urlencode(parametri) if parametri else '')
        for t in range(6):
            req = urllib.request.Request(url, headers={'key': self.key, 'exchange': self.exchange})
            with self.lock:
                self.chiamate += 1
            try:
                with urllib.request.urlopen(req, timeout=60, context=SSL) as r:
                    return json.loads(r.read().decode() or 'null')
            except urllib.error.HTTPError as e:
                if e.code == 509:
                    sys.exit('Limite giornaliero API superato (509).')
                if e.code in (429, 500, 502, 503, 504) and t < 5:
                    time.sleep(3 * (t + 1))
                    continue
                raise RuntimeError(f'{percorso} {parametri}: HTTP {e.code} {e.read()[:200]!r}')
            except (urllib.error.URLError, TimeoutError):
                if t < 5:
                    time.sleep(3 * (t + 1))
                    continue
                raise


def uid_da_api(sd, limite):
    uids, pagina, pagine = [], 1, 1
    while pagina <= pagine:
        j = sd.get('products/paginated_list.json', page=pagina, limit=500)
        pagine = j.get('total_pages') or 1
        uids += [p['uid'] for p in j.get('products', [])]
        if limite and len(uids) >= limite:
            return uids[:limite]
        pagina += 1
    return uids


def prodotti_da_api(sd, uids):
    q, risultati, errori = queue.Queue(), {}, []
    for u in uids:
        q.put(u)
    lock = threading.Lock()

    def lavora():
        while True:
            try:
                u = q.get_nowait()
            except queue.Empty:
                return
            try:
                p = sd.get('products/product.json', uid=u, nodes='categories_name,brand_name,gallery')
                with lock:
                    risultati[str(u)] = p
                    if len(risultati) % 2000 == 0:
                        log('letti', len(risultati), 'di', len(uids))
            except Exception as e:
                with lock:
                    errori.append((u, str(e)[:200]))

    th = [threading.Thread(target=lavora) for _ in range(THREAD_API)]
    [t.start() for t in th]
    [t.join() for t in th]
    return risultati, errori


def normalizza_api(p):
    """Record comune da un prodotto API (campi verificati il 2026-10-16 con
    --campione). Come nel tema: «Su ordinazione» = data.unlimited, che vince
    sulla giacenza; giorni di consegna = dimension.z; m² per confezione =
    dimension.w."""
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
    return {
        'uid': str(p.get('uid')),
        'codice': spazi(str(p.get('sku') or p.get('code') or '')),
        'titolo': spazi(p.get('title')),
        'descrizione': p.get('description') or '',
        'prezzo': numero(p.get('price')),
        'prezzo_finale': numero(p.get('final_price')),
        'visibile': str(p.get('status')) == '1' and not p.get('privateProduct'),
        'giacenza': numero(p.get('stock_count', p.get('stockCount'))) or 0,
        'illimitato': data.get('unlimited') in (True, 1, '1', 'true'),
        'giorni_consegna': numero(dim.get('z')),
        'immagini': immagini,
        'brand': p.get('brand_name'),
        'categorie': categorie,
        'ean': ean_valido(data.get('ean13')),
        'mpn': spazi(data.get('mpn')),
        'peso': numero(p.get('weight')),
        # m² per confezione: dimension.w, altrimenti dalla descrizione («pacco contiene 1,9m²»)
        'm2': (numero(dim.get('w')) if numero(dim.get('w')) and 0.05 <= numero(dim.get('w')) <= 20 else None)
              or m2_confezione(testo(p.get('description')), p.get('title')),
        'varianti': len(p.get('variants') or []) if str(p.get('usevariants')) == '1' else 0,
    }


# ---------------------------------------------------------------- Clerk

def carica_clerk(lingue):
    prodotti, categorie = {}, {}
    for l in lingue:
        loc = LINGUE[l][0]
        log('scarico Clerk', loc)
        prodotti[l] = {str(x['id']): x for x in json.loads(scarica(S3 + f'products-{loc}.json'))}
        categorie[l] = json.loads(scarica(S3 + f'categories-{loc}.json'))
    return prodotti, categorie


def testi_es_feed_vecchio():
    """Riserva per lo spagnolo finché il file Clerk ES è una copia del FR:
    titoli e descrizioni dal vecchio g-feed-es.xml di No Gravity (fermo al
    15/07/2026). Prezzi, disponibilità e il resto arrivano comunque dalle API;
    i prodotti creati dopo luglio non ci sono e restano fuori dal feed ES."""
    log('scarico g-feed-es.xml (testi spagnoli di riserva)')
    s = scarica(S3 + 'g-feed-es.xml').decode('utf-8')
    out = {}
    for it in re.findall(r'<item>(.*?)</item>', s, re.S):
        def g(t):
            m = re.search(r'<g:%s>(.*?)</g:%s>' % (t, t), it, re.S)
            return html.unescape(m.group(1)) if m else ''
        m = re.search(r'/product/(\d+)', g('link'))
        if m and g('title'):
            uid = m.group(1)
            out[uid] = {'name': g('title'), 'description': g('description'),
                        'url': LINGUE['es'][1] + '/product/' + uid}
    return out


def percorsi_categorie(lista):
    per_id = {str(c['id']): c for c in lista}
    padre = {}
    for c in lista:
        for s in c.get('subcategories', []):
            padre[str(s)] = str(c['id'])
    out = {}
    for i in per_id:
        nomi, j = [], i
        while j in per_id and len(nomi) < 8:
            nomi.append(spazi(per_id[j]['name']).rstrip('_ '))
            j = padre.get(j)
        out[i] = ' > '.join(reversed(nomi))
    return out


def normalizza_clerk(x):
    """Record comune dal solo file Clerk (anteprima senza API). Il prezzo
    Clerk delle piastrelle è al m²: lo riportiamo alla confezione."""
    m2 = m2_confezione(x.get('description'), x.get('name'))
    prezzo, listino = x.get('price') or 0, x.get('list_price') or 0
    if x.get('sqmPrice'):
        if not m2:
            prezzo = listino = 0   # senza m² per confezione non sappiamo il prezzo reale
        else:
            prezzo, listino = prezzo * m2, listino * m2
    return {
        'uid': str(x['id']), 'codice': spazi(x.get('sku')), 'titolo': spazi(x.get('name')),
        'descrizione': x.get('description') or '',
        'prezzo': round(max(prezzo, listino), 2), 'prezzo_finale': round(prezzo, 2),
        'visibile': bool(x.get('available')), 'giacenza': x.get('stock') or 0,
        'illimitato': bool(x.get('available')), 'giorni_consegna': None,
        'immagini': [] if x.get('no_image') or not x.get('image') else [x['image']],
        'brand': x.get('brand_name'), 'categorie': [str(c) for c in x.get('categories', [])],
        'ean': None, 'mpn': '', 'peso': None, 'm2': m2, 'varianti': 0,
    }


# ---------------------------------------------------------------- regole feed

def categoria_google(titolo_it, percorso_it):
    n, c = (titolo_it or '').lower(), (percorso_it or '').lower()
    for regola, gid in REGOLE_CATEGORIA:
        ok = False
        if 'nome' in regola and re.search(regola['nome'], n):
            ok = True
        if 'cat' in regola and re.search(regola['cat'], c):
            ok = True
        if ok:
            return gid
    return None


def pulisci_brand(b):
    b = spazi(str(b or '')).rstrip('_ ').strip()
    return '' if b.lower() in ('', 'unknown', 'none') else b


def costruisci_item(r, l, clerk_l, percorsi_l, percorsi_it, oggi, motivi):
    """Ritorna (xml dell'item, dati per il rapporto) oppure None se escluso."""
    def escludi(motivo):
        motivi[motivo] = motivi.get(motivo, 0) + 1
        return None

    if not r['visibile']:
        return escludi('nascosto o non disponibile')
    if not r['immagini']:
        return escludi('senza immagine')
    finale, pieno = r['prezzo_finale'] or r['prezzo'], r['prezzo'] or r['prezzo_finale']
    if not finale or finale <= 0:
        return escludi('prezzo 0 o mancante')
    if not r['codice']:
        return escludi('senza codice')
    # campioni da 10 € (codice «…_sample»): su Shopping toglierebbero clic alla piastrella vera
    if re.search(r'[_-]sample$', r['codice'], re.I) or re.match(r'campion[ei]\b', r['titolo'], re.I):
        return escludi('campione')

    _, dominio, valuta = LINGUE[l]
    tradotto = clerk_l.get(r['uid'])
    if l != 'it' and not tradotto:
        return escludi('assente nel file Clerk della lingua')
    titolo = spazi(tradotto['name']) if tradotto and l != 'it' else r['titolo']
    descr = testo(tradotto.get('description') if tradotto and l != 'it' else r['descrizione'])
    link = (tradotto or {}).get('url') or f"{dominio}/product/{r['uid']}"
    if not link.startswith(dominio):
        return escludi('link di un altro dominio nel file Clerk')

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
    c = [
        ('g:id', r['codice']),
        ('g:title', titolo[:150]),
        ('g:description', (descr or titolo)[:5000]),
        ('g:link', link),
        ('g:image_link', r['immagini'][0]),
    ]
    c += [('g:additional_image_link', i) for i in r['immagini'][1:11]]
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
    xml = '<item>' + ''.join(f'<{k}>{escape(str(v))}</{k}>' for k, v in c) + '</item>\n'
    return xml, {'disp': disp, 'gcat': gcat, 'ean': bool(r['ean']), 'm2': bool(r['m2']), 'peso': bool(r['peso'])}


def scrivi_feed(l, items):
    _, dominio, _ = LINGUE[l]
    percorso = os.path.join(OUT, f'feed-{l}.xml')
    with open(percorso + '.tmp', 'w', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0"><channel>\n')
        f.write(f'<title>Universo Ceramiche</title><link>{dominio}</link><description>Feed prodotti {l.upper()}</description>\n')
        f.writelines(items)
        f.write('</channel></rss>\n')
    os.replace(percorso + '.tmp', percorso)
    return percorso


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--solo-clerk', action='store_true', help='anteprima senza API Storeden')
    ap.add_argument('--campione', action='store_true', help='salva il JSON di un prodotto API ed esce')
    ap.add_argument('--limite', type=int, default=0, help='solo i primi N prodotti')
    ap.add_argument('--lingue', default='it,en,de,es,fr')
    ap.add_argument('--da-cache', action='store_true', help="usa out/grezzi.json.gz dell'ultimo giro API")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    lingue = [l.strip() for l in a.lingue.split(',') if l.strip() in LINGUE]
    inizio = time.time()

    if a.campione:
        sd = Storeden()
        uid = uid_da_api(sd, 1)[0]
        p = sd.get('products/product.json', uid=uid, nodes='categories_name,brand_name,gallery')
        f = os.path.join(OUT, 'campione-prodotto.json')
        json.dump(p, open(f, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        log('salvato', f, '→ campi:', ', '.join(sorted(p)))
        log('normalizzato:', json.dumps(normalizza_api(p), ensure_ascii=False)[:1500])
        return

    # il francese serve anche per riconoscere il file ES copiato dal FR
    clerk, cat_clerk = carica_clerk(sorted(set(lingue) | {'it'} | ({'fr'} if 'es' in lingue else set())))
    percorsi = {l: percorsi_categorie(cat_clerk[l]) for l in cat_clerk}

    rapporto = {'generato': datetime.datetime.now().isoformat(timespec='seconds'),
                'fonte': 'solo Clerk (anteprima)' if a.solo_clerk else 'API Storeden + Clerk',
                'lingue': {}, 'avvisi': []}

    # file Clerk ES identico a FR: non pubblicare un feed spagnolo in francese
    if 'es' in clerk and 'fr' in clerk:
        campione = list(clerk['es'])[:500]
        uguali = sum(1 for u in campione if u in clerk['fr'] and clerk['es'][u]['name'] == clerk['fr'][u]['name'])
        if campione and uguali / len(campione) > 0.9:
            clerk['es'] = testi_es_feed_vecchio()
            rapporto['avvisi'].append('File Clerk ES identico a FR: testi spagnoli presi dal vecchio '
                                      f'g-feed-es.xml di luglio ({len(clerk["es"])} prodotti).')

    cache = os.path.join(OUT, 'grezzi.json.gz')
    if a.solo_clerk:
        record = [normalizza_clerk(x) for x in clerk['it'].values()]
        if a.limite:
            record = record[:a.limite]
    elif a.da_cache:
        rapporto['fonte'] = 'cache API ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(cache))) + ' + Clerk'
        record = [normalizza_api(p) for p in json.load(gzip.open(cache, 'rt', encoding='utf-8')).values()]
    else:
        sd = Storeden()
        uids = uid_da_api(sd, a.limite)
        log('prodotti in Storeden:', len(uids))
        grezzi, errori = prodotti_da_api(sd, uids)
        if errori:
            rapporto['avvisi'].append(f'{len(errori)} prodotti non letti dalle API, es. {errori[:3]}')
        if len(errori) > len(uids) * 0.05:
            sys.exit(f'Troppi errori API ({len(errori)}): feed non aggiornato.')
        if not a.limite:
            with gzip.open(cache, 'wt', encoding='utf-8') as f:
                json.dump(grezzi, f, ensure_ascii=False)
        record = [normalizza_api(p) for p in grezzi.values()]
        rapporto['chiamate_api'] = sd.chiamate
        rapporto['prodotti_con_varianti'] = sum(1 for r in record if r['varianti'])

    # stesso codice su più prodotti: Google vuole id unici
    visti, doppi = set(), set()
    for r in record:
        (doppi if r['codice'] in visti else visti).add(r['codice'])
    if doppi:
        rapporto['avvisi'].append(f'{len(doppi)} codici usati da più prodotti (tenuto il primo), es. {sorted(doppi)[:5]}')

    oggi = datetime.date.today()
    for l in lingue:
        items, motivi, stat, usati = [], {}, [], set()
        for r in record:
            if r['codice'] in usati:
                motivi['codice doppio'] = motivi.get('codice doppio', 0) + 1
                continue
            res = costruisci_item(r, l, clerk.get(l, {}), percorsi.get(l, {}), percorsi['it'], oggi, motivi)
            if res:
                usati.add(r['codice'])
                items.append(res[0])
                stat.append(res[1])
        f = scrivi_feed(l, items)
        n = len(stat) or 1
        rapporto['lingue'][l] = {
            'file': os.path.basename(f), 'prodotti': len(items), 'esclusi': motivi,
            'disponibilita': {d: sum(1 for s in stat if s['disp'] == d) for d in ('in_stock', 'backorder', 'out_of_stock')},
            'con_categoria_google_%': round(100 * sum(1 for s in stat if s['gcat']) / n, 1),
            'con_ean_%': round(100 * sum(1 for s in stat if s['ean']) / n, 1),
            'con_m2_%': round(100 * sum(1 for s in stat if s['m2']) / n, 1),
            'con_peso_%': round(100 * sum(1 for s in stat if s['peso']) / n, 1),
        }
        log(l, len(items), 'prodotti; esclusi', motivi)

    rapporto['durata_minuti'] = round((time.time() - inizio) / 60, 1)
    json.dump(rapporto, open(os.path.join(OUT, 'rapporto.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    righe = ''.join(f'<li><a href="feed-{l}.xml">feed-{l}.xml</a> — {v["prodotti"]} prodotti</li>' for l, v in rapporto['lingue'].items())
    open(os.path.join(OUT, 'index.html'), 'w', encoding='utf-8').write(
        f'<!doctype html><meta charset="utf-8"><title>Feed UC</title><h1>Feed Universo Ceramiche</h1>'
        f'<meta name="robots" content="noindex"><p>Generato {rapporto["generato"]}</p><ul>{righe}</ul><p><a href="rapporto.json">rapporto.json</a></p>')
    log('fatto in', rapporto['durata_minuti'], 'minuti')


if __name__ == '__main__':
    main()
