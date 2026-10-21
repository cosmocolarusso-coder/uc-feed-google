"""Campi per il configuratore pavimenti, aggiunti al feed Clerk (prefisso cfg_).

Il configuratore sul sito chiede a Clerk i prodotti filtrati per risposta
(lavoro, effetto, tonalità, fascia di prezzo) e mostra foto e totali.
Qui si ricavano i dati che Clerk non ha:

  cfg_lavoro    gres | spc | pareti | esterno   (vuoto = non è un pavimento/rivestimento da proporre)
  cfg_effetto   legno | marmo | pietra | cemento | cotto | tinta
  cfg_decoro    1 per decori, rilievi, muretti, cementine, mosaici… (non rappresentano una scelta)
  cfg_m2box     m² per scatola
  cfg_giorni    giorni di consegna
  cfg_campione  id del campione a 10 € (codice <codice>_sample), 0 se non c'è
  cfg_foto_amb  foto ambientata (una stanza vera), se c'è
  cfg_foto_tex  foto della sola superficie, adatta a mostrare il colore
  cfg_tono      chiaro | beige | grigio | scuro | colore (azzurri, verdi…), misurato sulla foto

Le foto si analizzano con Pillow sulle miniature 300×300 di Storeden
(cdn.storeden.net/secache/<id>); i risultati restano in stato/immagini.json.gz
e ogni notte si analizzano al massimo MAX_IMMAGINI foto nuove. Senza Pillow
i campi foto e tono restano vuoti e il resto funziona lo stesso.
"""
import gzip, io, json, os, re, threading, queue, urllib.request

QUI = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(QUI, 'stato', 'immagini.json.gz')
MAX_IMMAGINI = int(os.environ.get('MAX_IMMAGINI', 10000))
THREAD = 8
MINI = 'https://cdn.storeden.net/secache/'
FOTO_PER_PRODOTTO = 3

ACCESSORI = re.compile(r'battiscop|cordol|profil|zoccol|gradin|angolar|pezz[oi] special|campion|sample|colla|tappetin|torello|'
                       r'scalin|\bstep\b|terminal|bordo|sigillant|stucco|fuga\b|fughe|distanziator|primer|detergent', re.I)
DECORO = re.compile(r'struttura\s*3d|(?<!reactive )\b3d\b|muretto|decor|savana|floret|petal|gelosia|patchwork|\bmix\b|segment|'
                    r'\bbande\b|cementin|optical|fiori|foglie|flora|inserto|listell|mosaic|boiserie|eivissa', re.I)
EFFETTI = {'legno': 'legno', 'marmo': 'marmo', 'travertino': 'marmo', 'onice': 'marmo', 'pietra': 'pietra',
           'cemento': 'cemento', 'resina': 'cemento', 'cotto': 'cotto'}
RE_EFFETTO = re.compile(r'effetto\s+(legno|marmo|travertino|onice|pietra|cemento|resina|cotto)', re.I)


def _norm(s):
    return re.sub(r'\s+', ' ', (s or '').replace('_', ' ')).strip().lower()


def antenati(categorie, albero):
    nomi = set()
    for c in categorie:
        visti = 0
        while c in albero and visti < 12:
            nomi.add(_norm(albero[c]['nome']))
            c = albero[c].get('padre')
            visti += 1
    return nomi


def lavoro(r, nomi):
    t = r.get('titolo') or ''
    if not (r.get('m2') or (r.get('w') or 0) > 0) or ACCESSORI.search(t):
        return ''
    if re.match(r'\s*(rivestiment|muretto)', t, re.I):
        return 'pareti'
    if 'rivestimenti' in nomi and 'pavimenti' not in nomi:
        return 'pareti'
    if nomi & {'antiscivolo da esterno', 'gres spessore 2cm'} or re.search(r'\b(sp\.?\s*2\s*cm|20\s*mm)\b', t, re.I):
        return 'esterno'
    if nomi & {'spc', 'lvt autoposanti', 'lvt interlocking', 'laminato'}:
        return 'spc'
    if nomi & {'gres porcellanato', 'gres grandi formati', 'gres porcellanato sottile'} or ('pavimenti' in nomi and re.search(r'\bgres\b', t, re.I)):
        return 'gres'
    return ''


def effetto(r, nomi, attr_effetto=''):
    t = r.get('titolo') or ''
    m = RE_EFFETTO.search(t)
    if m:
        return EFFETTI[m.group(1).lower()]
    if re.search(r'tinta unita', t, re.I) or 'tinta unita' in nomi:
        return 'tinta'
    for n in nomi:
        m = RE_EFFETTO.search(n)
        if m:
            return EFFETTI[m.group(1).lower()]
    a = _norm(attr_effetto)
    for k, v in EFFETTI.items():
        if k in a:
            return v
    if re.search(r'travertin', t, re.I):
        return 'marmo'
    return ''


# ------------------------------------------------------------ foto

def _metriche(dati):
    """[sd celle, bianco, bordo bianco, luminosità, calore, texture piena, saturazione] su una miniatura."""
    from PIL import Image
    im = Image.open(io.BytesIO(dati)).convert('RGB').resize((96, 96))
    px = list(im.getdata())
    W = H = 96
    L = [0.3 * r + 0.59 * g + 0.11 * b for r, g, b in px]
    celle = []
    for gy in range(4):
        for gx in range(4):
            s = 0
            for y in range(gy * 24, gy * 24 + 24):
                s += sum(L[y * W + gx * 24: y * W + gx * 24 + 24])
            celle.append(s / 576)
    m = sum(celle) / 16
    sd = (sum((c - m) ** 2 for c in celle) / 16) ** .5
    bianco = lambda p: p[0] > 238 and p[1] > 238 and p[2] > 238
    wh = sum(1 for i in range(0, len(px), 2) if bianco(px[i])) / (len(px) / 2)
    bordo = [px[i] for i in range(W)] + [px[(H - 1) * W + i] for i in range(W)] + [px[j * W] for j in range(H)] + [px[j * W + W - 1] for j in range(H)]
    bw = sum(1 for p in bordo if bianco(p)) / len(bordo)
    vere = [p for p in px if not (p[0] > 246 and p[1] > 246 and p[2] > 246)] or px
    lum = sum(0.3 * r + 0.59 * g + 0.11 * b for r, g, b in vere) / len(vere)
    cal = sum(r - b for r, g, b in vere) / len(vere)
    sat = sum(max(p) - min(p) for p in vere) / len(vere)
    # ritaglio dei bordi bianchi: texture piena se dentro il ritaglio non resta sfondo
    col_vuota = lambda x: sum(1 for y in range(0, H, 2) if not bianco(px[y * W + x])) < H * .02
    riga_vuota = lambda y: sum(1 for x in range(0, W, 2) if not bianco(px[y * W + x])) < W * .02
    l, r_, t, b = 0, W - 1, 0, H - 1
    while l < r_ and col_vuota(l): l += 1
    while r_ > l and col_vuota(r_): r_ -= 1
    while t < b and riga_vuota(t): t += 1
    while b > t and riga_vuota(b): b -= 1
    area = (r_ - l + 1) * (b - t + 1) / (W * H)
    dentro = [px[y * W + x] for y in range(t, b + 1, 3) for x in range(l, r_ + 1, 3)]
    piena = 1 if area >= .18 and sum(1 for p in dentro if bianco(p)) / max(1, len(dentro)) < .10 else 0
    return [round(sd, 1), round(wh, 3), round(bw, 3), round(lum, 1), round(cal, 1), piena, round(sat, 1)]


def carica_cache():
    try:
        with gzip.open(CACHE, 'rt', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def salva_cache(c):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with gzip.open(CACHE + '.tmp', 'wt', encoding='utf-8') as f:
        json.dump(c, f, separators=(',', ':'))
    os.replace(CACHE + '.tmp', CACHE)


def _id(url):
    return url.rstrip('/').rsplit('/', 1)[-1]


def analizza_foto(da_fare, cache, log, ssl_ctx=None):
    """Scarica e misura le foto mancanti (al massimo MAX_IMMAGINI). Ritorna quante."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        log('configuratore: Pillow non installato, foto non analizzate')
        return 0
    ids = [i for i in dict.fromkeys(da_fare) if i not in cache][:MAX_IMMAGINI]
    q = queue.Queue()
    [q.put(i) for i in ids]
    lock, fatti = threading.Lock(), [0]

    def lavora():
        while True:
            try:
                iid = q.get_nowait()
            except queue.Empty:
                return
            try:
                req = urllib.request.Request(MINI + iid, headers={'User-Agent': 'uc-feed'})
                dati = urllib.request.urlopen(req, timeout=30, context=ssl_ctx).read()
                m = _metriche(dati)
            except Exception:
                m = None   # foto illeggibile: non si riprova ogni notte
            with lock:
                cache[iid] = m
                fatti[0] += 1
                if fatti[0] % 1000 == 0:
                    log('configuratore: foto analizzate', fatti[0], 'di', len(ids))
    th = [threading.Thread(target=lavora) for _ in range(THREAD)]
    [t.start() for t in th]
    [t.join() for t in th]
    return fatti[0]


def _quantile(v, q):
    v = sorted(v)
    return v[int(q * (len(v) - 1))] if v else 0


# ------------------------------------------------------------ campi

def calcola(prodotti, albero, attributi_di, log, ssl_ctx=None):
    """{uid: {cfg_...}} per i prodotti da proporre; analizza le foto mancanti."""
    campioni = {}
    for u, r in prodotti.items():
        c = r.get('codice') or ''
        if r.get('visibile') and re.search(r'_sample$', c, re.I):
            campioni[c[:-7].lower()] = int(u)
    base = {}
    for u, r in prodotti.items():
        if not r.get('visibile'):
            continue
        nomi = antenati(r.get('categorie') or [], albero)
        lav = lavoro(r, nomi)
        if not lav:
            continue
        att = attributi_di(r)
        dec = 1 if DECORO.search(r.get('titolo') or '') or att.get('tipo_prodotto') in ('Decoro', 'Mosaico') else 0
        base[u] = {'cfg_lavoro': lav, 'cfg_effetto': effetto(r, nomi, att.get('effetto', '')), 'cfg_decoro': dec,
                   'cfg_m2box': round(r.get('m2') or r.get('w') or 0, 3), 'cfg_giorni': int(r.get('giorni_consegna') or 0),
                   'cfg_campione': campioni.get((r.get('codice') or '').lower(), 0)}
    cache = carica_cache()
    da_fare = [_id(i) for u in base if not base[u]['cfg_decoro'] for i in prodotti[u]['immagini'][:FOTO_PER_PRODOTTO]]
    nuove = analizza_foto(da_fare, cache, log, ssl_ctx)
    if nuove:
        salva_cache(cache)
    # foto ambientata e texture per prodotto, poi soglie di tonalità relative a tutto il catalogo
    sorgenti = {}
    for u, f in base.items():
        imm = [(i, cache.get(_id(i))) for i in prodotti[u]['immagini'][:FOTO_PER_PRODOTTO]]
        imm = [(i, m) for i, m in imm if m]
        amb = [x for x in imm if x[1][0] >= 18.5 and x[1][2] < .5]
        tex = [x for x in imm if x[1][0] <= 20 and x[1][5]]
        f['cfg_foto_amb'] = max(amb, key=lambda x: x[1][0])[0] if amb else ''
        f['cfg_foto_tex'] = min(tex, key=lambda x: x[1][0])[0] if tex else ''
        src = min(tex, key=lambda x: x[1][0])[1] if tex else (imm[0][1] if imm and not amb else None)
        if src:
            sorgenti[u] = src
    lum = [m[3] for m in sorgenti.values()]
    alto, basso = _quantile(lum, .65), _quantile(lum, .15)
    cal_medi = [m[4] for m in sorgenti.values() if basso <= m[3] < alto]
    soglia_cal = max(14, _quantile(cal_medi, .5))
    for u, f in base.items():
        m = sorgenti.get(u)
        f['cfg_tono'] = '' if not m else 'colore' if len(m) > 6 and m[6] >= 40 and m[4] < 10 else 'chiaro' if m[3] >= alto else 'scuro' if m[3] < basso else 'beige' if m[4] >= soglia_cal else 'grigio'
    misurati = sum(1 for u in base if sorgenti.get(u))
    log('configuratore:', len(base), 'prodotti; foto nuove analizzate', nuove, '; con tonalità', misurati,
        '; soglie luminosità', round(basso), round(alto), 'calore', round(soglia_cal))
    return base, {'prodotti': len(base), 'foto_nuove': nuove, 'con_tono': misurati,
                  'per_lavoro': {k: sum(1 for f in base.values() if f['cfg_lavoro'] == k) for k in ('gres', 'spc', 'pareti', 'esterno')}}
