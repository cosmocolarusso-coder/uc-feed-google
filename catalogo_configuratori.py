"""[CHG-592] Catalogo dei configuratori (rubinetti, pavimenti, mobili bagno) preso da Shopify, al posto di Clerk.

Le app dei configuratori (snippet uc-app-*, CHG-586) chiedevano i prodotti a Clerk, cioè al catalogo Storeden: id Storeden,
prezzi e foto di Storeden, campi cfg_* calcolati ogni notte da feed-google/configuratore.py. Qui tutto viene da Shopify:
- prodotti, varianti, prezzi, disponibilità, foto, marca (vendor), serie/finitura/m² per scatola/giorni (metafield uc_scheda),
  formato (uc_filtri.formato), categorie (tag sd-cat-<uid>), nomi tradotti (translatableResources);
- i campi propri del configuratore pavimenti sono metafield uc_cfg.* sul prodotto (modificabili dall'admin):
  lavoro, effetto, tono, decoro, parete, tipo, foto_amb, foto_tex (indirizzo della foto Shopify), serie e finitura
  (Clerk le leggeva dalle righe «Serie:»/«Finitura:» della descrizione; uc_scheda.serie/finitura mancano su quasi
  tutta la rubinetteria), popolarita (posizione nella classifica «popular» di Clerk del 03/11/2026).
  Si copiano una volta dal feed Clerk italiano (out/fonti/clerk-it.json) e dall'API di Clerk con la fase «metafield».
Ordine delle righe = popolarità, come le risposte di Clerk (le app usano la posizione per scegliere serie ed esempi):
prima i pezzi venduti negli ordini Shopify degli ultimi 365 giorni, a parità uc_cfg.popolarita.
Nei file restano solo i prodotti acquistabili (tutte le domande delle app chiedono available = true e prezzo > 0);
le parti ripetute di foto e indirizzi stanno in «p» (prefisso per colonna) e le categorie sono solo quelle usate dalle app.
Il risultato sono i file assets/uc-cfg-<rub|pav>-<lingua>-indice.json più un file per blocco (-gres, -set, …), letti
dalle app con lo stesso linguaggio dei filtri di Clerk (SHIM in shopify/tema-uc/configuratori.py del repository del tema).
L'id di ogni riga è la variante Shopify da mettere nel carrello.
[CHG-770] Catalogo a blocchi: prima ogni app scaricava un file intero (6,5–13,5 MB, 0,65–1,6 MB compressi) all'avvio. Ora
l'indice (poche decine di KB) descrive colonne, prefissi, dizionari e i blocchi con i valori che contengono (riassunti «s»:
«in» = soli valori presenti, «out» = valori mai presenti); l'app scarica solo i blocchi che il filtro non esclude e gli altri
a pagina ferma. Blocchi: pavimenti per lavoro (gres, spc, pareti, esterno) + «m2» (coordinati al m²) + «altro» + «mob»;
rubinetti «set» (categoria 31596071), «cromo» e «lavabo» (31596070 per finitura), «serie» (altri pezzi delle serie).
L'ordine globale per popolarità si ricompone con la stringa «o» (blocco di ogni riga). Nelle righe: url assente = handle
ricavato dal nome (regola «h», stesso calcolo in Python e JS), colonne costanti in «k», dizionari «d» per le colonne con
pochi valori, «e» = valore true → uguale a un'altra colonna (cfg_foto_tex = image, list_price = price).
[CHG-595] Gira ogni notte su GitHub Actions (.github/workflows/configuratori-shopify.yml, fase «notte»): credenziali
dell'app Shopify dai segreti SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET, negozio e tema da SHOPIFY_SHOP / SHOPIFY_TEMA.
In locale (cartella feed-google dentro il repository del tema) le legge da ../shopify/.env e scrive in ../shopify/out e
../shopify/tema-uc/tema/assets come prima.
Fasi:
  esporta    query bulk di tutti i prodotti attivi → out/configuratori/prodotti.jsonl (~10 min)
  metafield  definizioni uc_cfg + valori dal feed Clerk (mutazione bulk); poi rifare «esporta»
  nomi       titoli tradotti dei prodotti dei configuratori → out/configuratori/nomi.json
  vendite    pezzi venduti per prodotto negli ultimi 365 giorni (ordini Shopify) → out/configuratori/vendite.json
  catalogo   scrive tema/assets/uc-cfg-*.json
  tutto      esporta + nomi + vendite + catalogo
  carica     carica indici e blocchi sul tema (solo se le righe non sono calate troppo: MIN_RIGHE)
  notte      tutto + carica (GitHub Actions)
Uso: python3 -I catalogo_configuratori.py <fase> [--prova]"""
import datetime, json, os, pathlib, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

QUI = pathlib.Path(__file__).resolve().parent
SHOP = QUI.parent / 'shopify'            # in locale: repository del tema (credenziali, sorgenti Clerk, cartella out)
OUT = pathlib.Path(os.environ.get('CFG_OUT') or SHOP / 'out' / 'configuratori')
ASSETS = pathlib.Path(os.environ.get('CFG_ASSETS') or SHOP / 'tema-uc' / 'tema' / 'assets')
PRODOTTI = OUT / 'prodotti.jsonl'
NOMI = OUT / 'nomi.json'
VENDITE = OUT / 'vendite.json'
TEMI = ['gid://shopify/OnlineStoreTheme/' + x.strip() for x in os.environ.get('SHOPIFY_TEMA', '201462514008').split(',') if x.strip()]
# [CHG-770] tema di sviluppo «UC – Casa accolta (sviluppo)» (CHG-739); prima il tema di prova 201114878296 (CHG-720), fermo dal 9/11/2026
# come copia: ha ancora gli snippet vecchi, che leggono i file interi. Più temi: SHOPIFY_TEMA="id1,id2".
MIN_RIGHE = {'rub': 25000, 'pav': 12000}   # 03/11/2026: 35.756 e 18.327; sotto queste soglie l'esportazione è sospetta
VERSIONE = '2026-10'
LINGUE = ['it', 'en', 'fr', 'de', 'es']
FOTO = 'https://cdn.shopify.com/s/files/1/1024/4220/5528/files/'   # prefisso delle foto del negozio (nei file è in «p»)
PROVA = '--prova' in sys.argv
# categorie Storeden usate dalle app: rubinetteria (lavabo/bidet e set), mobili bagno (MOB_CAT del configuratore pavimenti)
CAT_RUB = {31596070, 31596071}
CAT_MOB = {31596063}
CFG = [  # (chiave uc_cfg, nome, tipo, campo del feed Clerk)
    ('lavoro', 'Configuratore: lavoro', 'single_line_text_field', 'cfg_lavoro'),
    ('effetto', 'Configuratore: effetto', 'single_line_text_field', 'cfg_effetto'),
    ('tono', 'Configuratore: tonalità', 'single_line_text_field', 'cfg_tono'),
    ('decoro', 'Configuratore: decoro (1 decoro, 2 rilievo)', 'number_integer', 'cfg_decoro'),
    ('parete', 'Configuratore: posabile a parete', 'boolean', 'cfg_parete'),
    ('tipo', 'Configuratore: tipo di prodotto', 'single_line_text_field', 'tipo_prodotto'),
    ('foto_amb', 'Configuratore: foto ambientata', 'single_line_text_field', 'cfg_foto_amb'),
    ('foto_tex', 'Configuratore: foto della superficie', 'single_line_text_field', 'cfg_foto_tex'),
    ('serie', 'Configuratore: serie', 'single_line_text_field', 'collezione'),
    ('finitura', 'Configuratore: finitura', 'single_line_text_field', 'finitura'),
    ('popolarita', 'Configuratore: posizione per popolarità (Clerk 03/11/2026)', 'number_integer', '_pop'),
]
METAFIELD = {  # alias → (namespace, key)
    'uid': ('uc_scheda', 'storeden_uid'), 'serie': ('uc_scheda', 'serie'), 'finitura': ('uc_scheda', 'finitura'),
    'mq': ('uc_scheda', 'mq_confezione'), 'giorni': ('uc_scheda', 'giorni_consegna'), 'dim': ('uc_scheda', 'dimensioni'),
    'formato': ('uc_filtri', 'formato'),
    **{f'cfg_{k}': ('uc_cfg', k) for k, *_ in CFG},
}
ESPORTA = '''{ products(query: "status:active") { edges { node {
  id handle title vendor tags
  %s
  featuredMedia { preview { image { url } } }
  media(first: 20) { edges { node { preview { image { url } } } } }
  variants { edges { node { id sku price compareAtPrice availableForSale inventoryQuantity unitPrice { amount } } } }
} } } }''' % '\n  '.join(f'{a}: metafield(namespace: "{n}", key: "{k}") {{ value }}' for a, (n, k) in METAFIELD.items())
SET_BULK = ('mutation call($metafields: [MetafieldsSetInput!]!) { metafieldsSet(metafields: $metafields) '
            '{ userErrors { field message } } }')


class Shopify:
    """Admin GraphQL con token client credentials (24 h); riprova sul throttling."""

    def __init__(self):
        env = dict(os.environ)
        if not env.get('SHOPIFY_CLIENT_SECRET') and (SHOP / '.env').exists():
            for r in (SHOP / '.env').read_text().splitlines():
                if '=' in r and not r.lstrip().startswith('#'):
                    k, _, v = r.partition('=')
                    env.setdefault(k.strip(), v.strip())
        self.env, self.negozio = env, env['SHOPIFY_SHOP']
        self._token, self._scade = None, 0

    def _curl(self, args, dati):
        return subprocess.run(['curl', '-s', '--max-time', '300'] + args, input=dati, capture_output=True, text=True).stdout

    def token(self):
        if not self._token or time.time() > self._scade:
            corpo = json.dumps({'grant_type': 'client_credentials', 'client_id': self.env['SHOPIFY_CLIENT_ID'],
                                'client_secret': self.env['SHOPIFY_CLIENT_SECRET']})
            j = json.loads(self._curl(['-X', 'POST', f'https://{self.negozio}/admin/oauth/access_token',
                                       '-H', 'Content-Type: application/json', '--data-binary', '@-'], corpo))
            self._token, self._scade = j['access_token'], time.time() + int(j.get('expires_in', 86399)) - 600
        return self._token

    def gql(self, query, variabili=None):
        corpo = json.dumps({'query': query, 'variables': variabili or {}})
        out = ''
        for t in range(8):
            out = self._curl(['-X', 'POST', f'https://{self.negozio}/admin/api/{VERSIONE}/graphql.json',
                              '-H', 'Content-Type: application/json', '-H', 'X-Shopify-Access-Token: ' + self.token(),
                              '--data-binary', '@-'], corpo)
            try:
                j = json.loads(out)
            except ValueError:
                time.sleep(2 * (t + 1))
                continue
            err = j.get('errors')
            if err and any((e.get('extensions') or {}).get('code') == 'THROTTLED' for e in err if isinstance(e, dict)):
                time.sleep(2 * (t + 1))
                continue
            if err:
                raise RuntimeError(json.dumps(err, ensure_ascii=False)[:1500])
            if (j.get('extensions') or {}).get('cost', {}).get('throttleStatus', {}).get('currentlyAvailable', 1000) < 200:
                time.sleep(2)
            return j['data']
        raise RuntimeError('Shopify non risponde: ' + out[:300])


def staged(sh, percorso):
    st = sh.gql('''mutation($i:[StagedUploadInput!]!){stagedUploadsCreate(input:$i){stagedTargets{url parameters{name value}} userErrors{message}}}''',
                {'i': [{'resource': 'BULK_MUTATION_VARIABLES', 'filename': os.path.basename(percorso), 'mimeType': 'text/jsonl', 'httpMethod': 'POST'}]})
    t = st['stagedUploadsCreate']['stagedTargets'][0]
    args = ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', '-X', 'POST', t['url']]
    for p in t['parameters']:
        args += ['-F', f"{p['name']}={p['value']}"]
    args += ['-F', f'file=@{percorso}']
    print('caricamento', subprocess.run(args, capture_output=True, text=True).stdout, flush=True)
    return next(p['value'] for p in t['parameters'] if p['name'] == 'key')


def aspetta(sh, bid):
    while True:
        time.sleep(20)
        o = sh.gql('query($id:ID!){node(id:$id){... on BulkOperation{status objectCount errorCode url partialDataUrl}}}', {'id': bid})['node']
        print(time.strftime('%H:%M:%S'), o['status'], o['objectCount'], flush=True)
        if o['status'] not in ('CREATED', 'RUNNING'):
            return o


def senza_v(u):
    return (u or '').split('?')[0]


def leggi():
    """Prodotti dall'export bulk: le righe figlie (media, varianti) seguono il padre con __parentId."""
    prodotti = {}
    for riga in open(PRODOTTI):
        x = json.loads(riga)
        if 'variants' not in x and '__parentId' in x:
            p = prodotti.get(x['__parentId'])
            if p is None:
                continue
            if x['id'].startswith('gid://shopify/ProductVariant/') if 'id' in x else False:
                p['_var'].append(x)
            elif 'preview' in x:
                u = ((x['preview'] or {}).get('image') or {}).get('url')
                if u:
                    p['_media'].append(senza_v(u))
            continue
        x['_var'], x['_media'] = [], []
        for a in METAFIELD:
            x[a] = (x.get(a) or {}).get('value')
        prodotti[x['id']] = x
    return prodotti


def esporta():
    OUT.mkdir(parents=True, exist_ok=True)
    sh = Shopify()
    r = sh.gql('''mutation($q:String!){bulkOperationRunQuery(query:$q){bulkOperation{id} userErrors{message}}}''', {'q': ESPORTA})['bulkOperationRunQuery']
    if r['userErrors']:
        raise RuntimeError(r['userErrors'])
    o = aspetta(sh, r['bulkOperation']['id'])
    if o['status'] != 'COMPLETED':
        raise RuntimeError(o)
    subprocess.run(['curl', '-s', '-o', str(PRODOTTI), o['url']], check=True)
    print('prodotti', len(leggi()))


def metafield():
    sh = Shopify()
    for k, nome, tipo, _ in CFG:
        r = sh.gql('''mutation($d:MetafieldDefinitionInput!){metafieldDefinitionCreate(definition:$d){userErrors{message code}}}''',
                   {'d': {'namespace': 'uc_cfg', 'key': k, 'name': nome, 'type': tipo, 'ownerType': 'PRODUCT'}})
        e = r['metafieldDefinitionCreate']['userErrors']
        print('definizione', k, 'ok' if not e else e[0]['code'])
    per_uid = {p['uid']: p for p in leggi().values() if p['uid']}
    righe, foto_perse, mancanti = [], 0, 0
    clerk = json.load(open(SHOP / 'out' / 'fonti' / 'clerk-it.json'))   # solo in locale (fase da fare una volta)
    pop = popolarita_clerk()
    for c in clerk:
        c['_pop'] = pop.get(c['id'])
    serie = {(c.get('brand_name'), c['collezione']) for c in clerk
             if c.get('collezione') and (c.get('cfg_lavoro') or set(c['categories']) & CAT_RUB)}
    for c in clerk:
        if not (c.get('cfg_lavoro') or set(c['categories']) & (CAT_RUB | CAT_MOB) or (c.get('brand_name'), c.get('collezione')) in serie):
            continue
        p = per_uid.get(str(c['id']))
        if not p:
            mancanti += 1
            continue
        mf = []
        for k, _, tipo, campo in CFG:
            v = c.get(campo)
            if campo.startswith('cfg_foto'):
                # foto Storeden → foto Shopify con lo stesso id all'inizio del nome del file (importa_tutto.py)
                sid = str(v or '').rstrip('/').split('/')[-1]
                v = next((u for u in p['_media'] if sid and u.rsplit('/', 1)[-1].startswith(sid)), '') if sid else ''
                if sid and not v:
                    foto_perse += 1
            if tipo == 'boolean':
                v = 'true' if v else 'false'
            elif tipo == 'number_integer':
                v = str(int(v or 0))
            if v in (None, '') or (campo in ('collezione', 'finitura') and v == p[k]):
                continue   # serie/finitura: solo dove uc_scheda non ha già lo stesso valore
            mf.append({'ownerId': p['id'], 'namespace': 'uc_cfg', 'key': k, 'type': tipo, 'value': str(v)})
        if mf:
            righe.append({'metafields': mf})
    percorso = OUT / 'metafield_cfg.jsonl'
    with open(percorso, 'w') as f:
        for r in righe:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    print('prodotti', len(righe), 'non su Shopify', mancanti, 'foto non trovate', foto_perse)
    if PROVA:
        return
    r = sh.gql('''mutation($m:String!,$p:String!){bulkOperationRunMutation(mutation:$m,stagedUploadPath:$p){bulkOperation{id} userErrors{message}}}''',
               {'m': SET_BULK, 'p': staged(sh, str(percorso))})['bulkOperationRunMutation']
    if r['userErrors']:
        raise RuntimeError(r['userErrors'])
    o = aspetta(sh, r['bulkOperation']['id'])
    ris = subprocess.run(['curl', '-s', o.get('url') or o.get('partialDataUrl') or ''], capture_output=True, text=True).stdout if (o.get('url') or o.get('partialDataUrl')) else ''
    err = [l for l in ris.splitlines() if '"message"' in l]
    print(o['status'], 'righe', len(ris.splitlines()), 'errori', len(err), err[:3])


def popolarita_clerk():
    """Classifica «popular» di Clerk (catalogo italiano, chiave pubblica delle app): id Storeden → posizione da 1."""
    chiave, ids = '9ImBHhIDtPBy2QCKZiwJhv0GMnWYFki1', []
    while True:
        u = ('https://api.clerk.io/v2/recommendations/popular?key=%s&limit=10000&offset=%d&attributes=%%5B%%22id%%22%%5D'
             % (chiave, len(ids)))
        d = json.loads(subprocess.run(['curl', '-s', u], capture_output=True, text=True, check=True).stdout)
        if d.get('status') != 'ok':
            raise RuntimeError(d)
        ids += d['result']
        if not d['result'] or len(ids) >= d['count']:
            break
    print('popolarità Clerk', len(ids))
    return {i: n + 1 for n, i in enumerate(ids)}


def vendite():
    # data vera dell'ordine = processedAt (createdAt degli ordini migrati da Storeden è il giorno dell'importazione)
    sh, conta, dopo = Shopify(), {}, None
    da = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
    q = '''query($a:String,$q:String){orders(first:100, after:$a, query:$q){pageInfo{hasNextPage endCursor}
      nodes{lineItems(first:100){nodes{quantity product{id}}}}}}'''
    while True:
        r = sh.gql(q, {'a': dopo, 'q': f'processed_at:>={da}'})['orders']
        for o in r['nodes']:
            for li in o['lineItems']['nodes']:
                if li['product']:
                    conta[li['product']['id']] = conta.get(li['product']['id'], 0) + li['quantity']
        if not r['pageInfo']['hasNextPage']:
            break
        dopo = r['pageInfo']['endCursor']
    json.dump(conta, open(VENDITE, 'w'))
    print('prodotti venduti', len(conta), 'pezzi', sum(conta.values()))


def categorie(p):
    return [int(t[7:]) for t in p['tags'] if t.startswith('sd-cat-') and t[7:].isdigit()]


def serie_di(p):
    return p['cfg_serie'] or p['serie'] or ''


def in_scope(prodotti):
    """Prodotti dei due file: rub = rubinetteria; pav = con uc_cfg.lavoro e mobili bagno. In tutti e due anche gli altri
    pezzi delle stesse serie (doccini, bocche, deviatori in altre categorie; coordinati dei pavimenti): le app li cercano
    con «collezione = … and finitura = …» senza categoria."""
    rub, pav = set(), set()
    s_rub, s_pav = set(), set()
    for g, p in prodotti.items():
        cat = set(categorie(p))
        if cat & CAT_RUB:
            rub.add(g)
            if serie_di(p):
                s_rub.add((p['vendor'], serie_di(p)))
        if p['cfg_lavoro'] or cat & CAT_MOB:
            pav.add(g)
        if p['cfg_lavoro'] and serie_di(p):
            s_pav.add((p['vendor'], serie_di(p)))
    for g, p in prodotti.items():
        k = (p['vendor'], serie_di(p))
        if k[1] and k in s_rub:
            rub.add(g)
        if k[1] and k in s_pav:
            pav.add(g)
    return rub, pav


def nomi():
    prodotti = leggi()
    rub, pav = in_scope(prodotti)
    ids = sorted(rub | pav)
    q = 'query($ids:[ID!]!){translatableResourcesByIds(first:250, resourceIds:$ids){nodes{resourceId %s}}}' % ' '.join(
        f'{l}: translations(locale: "{l}") {{ key value }}' for l in LINGUE[1:])
    sh = Shopify()

    def gruppo(i):
        n = sh.gql(q, {'ids': ids[i:i + 250]})['translatableResourcesByIds']['nodes']
        return {x['resourceId']: {l: next((t['value'] for t in x[l] if t['key'] == 'title'), '') for l in LINGUE[1:]} for x in n}

    out = {}
    with ThreadPoolExecutor(4) as ex:
        for d in ex.map(gruppo, range(0, len(ids), 250)):
            out.update(d)
    json.dump(out, open(NOMI, 'w'), ensure_ascii=False)
    print('nomi', len(out), 'di', len(ids))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def riga(p, lingua, tradotti, nome_it=False):
    """Una riga per prodotto, con la variante principale (non il campione); i campi hanno i nomi del feed Clerk.
    nome_it: «name» resta italiano anche nei siti esteri e il nome tradotto va in «nl» (rubinetti: il tipo di pezzo si
    riconosce dal nome italiano, CHG-543); altrimenti «name» è nella lingua del sito, come nei cataloghi Clerk esteri."""
    var = [v for v in p['_var'] if not re.search(r'_sample$', v.get('sku') or '', re.I)]
    if not var or not any(x['availableForSale'] and num(x['price']) > 0 for x in var):
        return None
    v = next(x for x in var if x['availableForSale'] and num(x['price']) > 0)
    camp = next((x for x in p['_var'] if re.search(r'_sample$', x.get('sku') or '', re.I) and x['availableForSale']), None)
    prezzo = num(v['price'])
    mq = num(p['mq'])
    al_mq = num((v.get('unitPrice') or {}).get('amount')) > 0
    mq_prezzo = num((v.get('unitPrice') or {}).get('amount')) or (round(prezzo / mq, 2) if mq else 0)
    pre = '' if lingua == 'it' else '/' + lingua
    nome_l = (tradotti.get(p['id']) or {}).get(lingua) or p['title']
    img = senza_v(((p.get('featuredMedia') or {}).get('preview') or {}).get('image', {}).get('url')) if p.get('featuredMedia') else ''
    return {
        'id': int(v['id'].rsplit('/', 1)[-1]),
        'name': p['title'] if lingua == 'it' or nome_it else nome_l,
        'nl': nome_l if nome_l != p['title'] else '',
        'url': f'{pre}/products/{p["handle"]}',
        'image': img or (p['_media'][0] if p['_media'] else ''),
        'brand_name': p['vendor'] or '',
        'categories': [c for c in categorie(p) if c in CAT_RUB | CAT_MOB],
        'available': bool(v['availableForSale']),
        # venduti al m² (prezzo unitario Shopify): prezzo e listino al m² come nel feed Clerk (le app confrontano sqmPrice
        # con list_price); battiscopa e simili restano a confezione anche se hanno i m² nella scheda
        'price': mq_prezzo if al_mq else prezzo,
        'list_price': round(max(prezzo, num(v.get('compareAtPrice'))) * (mq_prezzo / prezzo if al_mq and prezzo else 1), 2),
        'stock': max(0, int(v.get('inventoryQuantity') or 0)),
        'sqmPrice': mq_prezzo if mq else 0,
        'collezione': serie_di(p),
        'finitura': p['cfg_finitura'] or p['finitura'] or '',
        'formato': p['formato'] or p['dim'] or '',
        'tipo_prodotto': p['cfg_tipo'] or '',
        'cfg_lavoro': p['cfg_lavoro'] or '',
        'cfg_effetto': p['cfg_effetto'] or '',
        'cfg_tono': p['cfg_tono'] or '',
        'cfg_decoro': int(p['cfg_decoro'] or 0),
        'cfg_parete': 1 if p['cfg_parete'] == 'true' else 0,
        'cfg_m2box': mq,
        'cfg_giorni': int(num(p['giorni'])) or 15,
        'cfg_campione': int(camp['id'].rsplit('/', 1)[-1]) if camp else 0,
        'cfg_foto_amb': p['cfg_foto_amb'] or '',
        'cfg_foto_tex': p['cfg_foto_tex'] or '',
    }


COLONNE = {
    'rub': ['id', 'name', 'nl', 'url', 'image', 'brand_name', 'categories', 'available', 'price', 'list_price', 'stock',
            'collezione', 'finitura'],
    'pav': ['id', 'name', 'url', 'image', 'brand_name', 'categories', 'available', 'price', 'list_price', 'stock',
            'sqmPrice', 'collezione', 'finitura', 'formato', 'tipo_prodotto', 'cfg_lavoro', 'cfg_effetto', 'cfg_tono',
            'cfg_decoro', 'cfg_parete', 'cfg_m2box', 'cfg_giorni', 'cfg_campione', 'cfg_foto_amb', 'cfg_foto_tex'],
}
# [CHG-770] blocchi: ordine = quello in cui le app li chiedono; «pre» = scaricare a pagina ferma anche se nessuna domanda
# lo ha ancora chiesto (mob è il modulo mobili bagno, parcheggiato: solo a richiesta)
ORDINE_BLOCCHI = {'pav': ['gres', 'spc', 'pareti', 'esterno', 'm2', 'altro', 'mob'], 'rub': ['set', 'cromo', 'lavabo', 'serie']}
SENZA_PRE = {'mob'}
UGUALI = {'cfg_foto_tex': 'image', 'list_price': 'price'}   # «e»: true nella riga = stesso valore dell'altra colonna
NO_DIZIONARIO = {'id', 'name', 'nl', 'url', 'image', 'cfg_foto_amb', 'cfg_foto_tex', 'categories'}
MAX_DIZIONARIO = 4096   # colonne di testo con al più tanti valori distinti → dizionario nell'indice, indici nelle righe
MAX_RIASSUNTO = 32      # colonne con al più tanti valori distinti in un blocco → elenco «in» nel riassunto del blocco


def handle_da_nome(nome):
    """Stessa regola di ucHandle nel SHIM: via i caratteri non ASCII, minuscole, tutto il resto diventa «-»."""
    s = re.sub(r'[^\x00-\x7f]', '', nome).lower()
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def blocco_di(nome, r):
    cat = set(r['categories'] or [])
    if nome == 'rub':
        if 31596071 in cat:
            return 'set'
        if 31596070 in cat:
            return 'cromo' if (r['finitura'] or '').lower() == 'cromo' else 'lavabo'
        return 'serie'
    if r['cfg_lavoro']:
        return re.sub(r'[^a-z0-9]+', '-', r['cfg_lavoro'].lower()).strip('-') or 'altro'
    if cat & CAT_MOB:
        return 'mob'
    return 'm2' if r['sqmPrice'] and r['cfg_m2box'] else 'altro'


def riassunto(righe, col):
    """Per ogni colonna con pochi valori distinti nel blocco: {'in': [valori]} (null compreso se c'è)."""
    s = {}
    for i, c in enumerate(col):
        vals, troppi = set(), False
        for r in righe:
            v = r[i]
            for x in (v if isinstance(v, list) else [v]):
                vals.add(json.dumps(x, ensure_ascii=False))
                if len(vals) > MAX_RIASSUNTO:
                    troppi = True
                    break
            if troppi:
                break
        if not troppi:
            s[c] = {'in': [json.loads(x) for x in sorted(vals)]}
    return s


def catalogo():
    prodotti = leggi()
    tradotti = json.load(open(NOMI)) if NOMI.exists() else {}
    venduti = json.load(open(VENDITE)) if VENDITE.exists() else {}
    ordine = lambda g: (-venduti.get(g, 0), int(prodotti[g]['cfg_popolarita'] or 10 ** 9), g)
    gruppi = dict(zip(('rub', 'pav'), in_scope(prodotti)))
    versione = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')
    for nome, ids in gruppi.items():
        col = COLONNE[nome]
        for l in LINGUE:
            righe = [r for r in (riga(prodotti[g], l, tradotti, nome == 'rub') for g in sorted(ids, key=ordine)) if r]
            pref = {'url': f'{"" if l == "it" else "/" + l}/products/'}
            pref.update({c: FOTO for c in ('image', 'cfg_foto_amb', 'cfg_foto_tex') if c in col})
            for r in righe:
                for c, x in pref.items():
                    if r[c] and r[c].startswith(x):
                        r[c] = r[c][len(x):]
                if r['url'] == handle_da_nome(r['name']):
                    r['url'] = None           # «h»: lo ricava l'app dal nome
                for c, altra in UGUALI.items():
                    if c in col and r[c] not in ('', 0, None) and r[c] == r[altra]:
                        r[c] = True           # «e»: uguale all'altra colonna
            # colonne costanti («k») e dizionari («d»)
            costanti = {c: righe[0][c] for c in col if righe and all(r[c] == righe[0][c] for r in righe) and righe[0][c] not in ('', 0, None, [])}
            dizion = {}
            for c in col:
                if c in NO_DIZIONARIO or c in costanti:
                    continue
                vals = {r[c] for r in righe if isinstance(r[c], str) and r[c]}
                if vals and all(isinstance(r[c], str) or r[c] in ('', None) for r in righe) and len(vals) <= MAX_DIZIONARIO:
                    dizion[c] = sorted(vals)
            indice_diz = {c: {v: i for i, v in enumerate(vs)} for c, vs in dizion.items()}
            col_righe = [c for c in col if c not in costanti]

            def valore(r, c):
                """Valore come lo vede l'app: vuoto → null (tranne stock 0), true = uguale all'altra colonna."""
                v = r[c]
                if v is True and c in UGUALI:
                    return True
                if v in ('', [], None) or (v == 0 and c not in ('available', 'stock')):
                    return None
                return v

            def cella(r, c):
                v = valore(r, c)
                return indice_diz[c][v] if c in indice_diz and isinstance(v, str) else v

            # blocchi nell'ordine stabilito, poi eventuali valori nuovi di cfg_lavoro in coda (prima di altro e mob)
            per_blocco = {}
            for r in righe:
                per_blocco.setdefault(blocco_di(nome, r), []).append(r)
            noti = [b for b in ORDINE_BLOCCHI[nome] if b in per_blocco]
            nuovi = sorted(b for b in per_blocco if b not in ORDINE_BLOCCHI[nome])
            chiavi = [b for b in noti if b not in ('altro', 'mob')] + nuovi + [b for b in noti if b in ('altro', 'mob')]
            blocchi, tabelle = [], {}
            for b in chiavi:
                tab = [[cella(r, c) for c in col_righe] for r in per_blocco[b]]
                s = riassunto([[valore(r, c) for c in col_righe] for r in per_blocco[b]], col_righe)   # valori veri, non gli indici
                blocchi.append({'f': f'uc-cfg-{nome}-{l}-{b}.json', 'n': len(tab), 's': s, 'pre': 0 if b in SENZA_PRE else 1})
                tabelle[b] = tab
            # «out»: valori che definiscono altri blocchi e qui mancano (es. finitura Cromo nel blocco «lavabo»)
            for b, blk in zip(chiavi, blocchi):
                for c in col_righe:
                    if c in blk['s']:
                        continue
                    altrove = {json.dumps(x, ensure_ascii=False) for b2, blk2 in zip(chiavi, blocchi) if b2 != b for x in blk2['s'].get(c, {}).get('in', [])}
                    qui = set()
                    for r in per_blocco[b]:
                        v = valore(r, c)
                        for x in (v if isinstance(v, list) else [v]):
                            qui.add(json.dumps(x, ensure_ascii=False))
                    out = sorted(altrove - qui)
                    if out:
                        blk['s'][c] = {'out': [json.loads(x) for x in out]}
            pos = {b: i for i, b in enumerate(chiavi)}
            indice = {'v': versione, 'c': col_righe, 'p': pref, 'k': costanti, 'd': dizion, 'h': {'url': 'name'},
                      'e': {c: a for c, a in UGUALI.items() if c in col_righe}, 'b': blocchi,
                      'o': ''.join(chr(48 + pos[blocco_di(nome, r)]) for r in righe)}
            f = ASSETS / f'uc-cfg-{nome}-{l}-indice.json'
            f.write_text(json.dumps(indice, ensure_ascii=False, separators=(',', ':')))
            tot = f.stat().st_size
            for b, blk in zip(chiavi, blocchi):
                fb = ASSETS / blk['f']
                fb.write_text(json.dumps({'v': versione, 'r': tabelle[b]}, ensure_ascii=False, separators=(',', ':')))
                tot += fb.stat().st_size
            print(f.name, len(righe), 'righe', f.stat().st_size // 1024, 'KB; blocchi', ', '.join(f"{b['f'].split('-')[-1][:-5]} {b['n']}" for b in blocchi),
                  '; totale', tot // 1024, 'KB')


def file_catalogo(nome, l):
    """Indice e blocchi di un catalogo, blocchi prima: l'indice si carica per ultimo, così non indica mai un file assente."""
    indice = json.loads((ASSETS / f'uc-cfg-{nome}-{l}-indice.json').read_text())
    return [b['f'] for b in indice['b']] + [f'uc-cfg-{nome}-{l}-indice.json'], sum(b['n'] for b in indice['b'])


def carica():
    """Carica indici e blocchi sul tema (o sui temi), un file alla volta. Se un catalogo ha troppe poche righe non carica
    nulla: sul tema resta il catalogo della notte prima."""
    nomi = []
    for n in MIN_RIGHE:
        for l in LINGUE:
            f, righe = file_catalogo(n, l)
            if righe < MIN_RIGHE[n]:
                raise SystemExit(f'uc-cfg-{n}-{l}: {righe} righe, meno di {MIN_RIGHE[n]}: non carico nulla')
            nomi += f
    sh = Shopify()
    for tema_id in TEMI:
        tema = sh.gql('query($id:ID!){theme(id:$id){name role}}', {'id': tema_id})['theme']
        print('tema', tema_id.rsplit('/', 1)[-1], tema, flush=True)
        if not tema or 'non usare' in tema['name'].lower():   # [CHG-720] mai sul tema di riserva
            raise SystemExit(f'tema {tema_id} non valido ({tema}): controllare SHOPIFY_TEMA, non carico nulla')
        for n in nomi:
            r = sh.gql('''mutation($id:ID!,$f:[OnlineStoreThemeFilesUpsertFileInput!]!){themeFilesUpsert(themeId:$id,files:$f){
              upsertedThemeFiles{filename} userErrors{filename code message}}}''',
                       {'id': tema_id, 'f': [{'filename': 'assets/' + n, 'body': {'type': 'TEXT', 'value': (ASSETS / n).read_text()}}]})['themeFilesUpsert']
            if r['userErrors']:
                raise RuntimeError(r['userErrors'])
            print('caricato', n, flush=True)


if __name__ == '__main__':
    fase = sys.argv[1]
    OUT.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    fasi = {'esporta': [esporta], 'metafield': [metafield], 'nomi': [nomi], 'vendite': [vendite], 'catalogo': [catalogo],
            'carica': [carica], 'tutto': [esporta, nomi, vendite, catalogo], 'notte': [esporta, nomi, vendite, catalogo, carica]}
    for f in fasi[fase]:
        f()
