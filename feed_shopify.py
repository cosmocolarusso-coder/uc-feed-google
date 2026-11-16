#!/usr/bin/env python3
"""[CHG-920] Feed Google e Meta letti da Shopify, con gli stessi codici dei feed di oggi (fonte Storeden).

Serve per il giorno dell'apertura: i feed devono passare ai link nuovi nello stesso momento dei domini.
Fino ad allora NON sostituisce i feed pubblicati: scrive in out/shopify/ e confronta.

Dati (Admin GraphQL, esportazione in blocco, solo lettura):
  - prodotti attivi: titolo, descrizione, marca (vendor), foto, varianti (codice, prezzo, prezzo barrato,
    giacenza, «continua a vendere» = su ordinazione, EAN, peso), metafield uc_scheda (numero Storeden,
    m² per confezione, giorni di consegna);
  - traduzioni di titolo e descrizione (translatableResourcesByIds) per en/fr/de/es.
Stesse regole di genera_feed.py: id = codice del prodotto, campioni / senza foto / prezzo 0 esclusi,
su ordinazione = backorder con data, Meta = «in stock» + quantità. Link: <dominio>/products/<handle>
(un mercato per dominio al passaggio). Il percorso di categoria (product_type, custom_label_1) e la
categoria Google si prendono dai feed di oggi per codice (seed/percorsi-<l>.json.gz, `percorsi`), così
non cambiano le campagne; per i prodotti nuovi la categoria Google si ricava dal titolo.

Uso:  python3 -I feed_shopify.py percorsi   salva i percorsi dai feed pubblicati di oggi
      python3 -I feed_shopify.py            scrive out/shopify/feed[-meta]-<l>.xml.gz e il confronto
      python3 -I feed_shopify.py --lingue it,en"""
import argparse, datetime, gzip, html, json, os, re, subprocess, sys, time, pathlib
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

QUI = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(QUI))
from catalogo_configuratori import Shopify  # noqa: E402
from genera_feed import categoria_google, pulisci_brand, spazi, testo, GIACENZA_MAX, GIORNI_SU_ORDINAZIONE, LINGUE  # noqa: E402

OUT = QUI / 'out' / 'shopify'
SEED = QUI / 'seed'
PAGES = 'https://cosmocolarusso-coder.github.io/uc-feed-google/'
LOCALE = {'en': 'en', 'fr': 'fr', 'de': 'de', 'es': 'es'}
ESPORTA = '''{ products(query: "status:active") { edges { node {
  id handle title descriptionHtml vendor tags onlineStoreUrl
  uid: metafield(namespace: "uc_scheda", key: "storeden_uid") { value }
  mq: metafield(namespace: "uc_scheda", key: "mq_confezione") { value }
  giorni: metafield(namespace: "uc_scheda", key: "giorni_consegna") { value }
  media(first: 11) { edges { node { preview { image { url } } } } }
  variants { edges { node { sku price compareAtPrice inventoryQuantity inventoryPolicy barcode
    inventoryItem { measurement { weight { value unit } } } } } }
} } } }'''


def log(*a):
    print(datetime.datetime.now().strftime('%H:%M:%S'), *a, flush=True)


def esporta(sp):
    r = sp.gql('mutation($q:String!){bulkOperationRunQuery(query:$q){bulkOperation{id} userErrors{message}}}', {'q': ESPORTA})
    if r['bulkOperationRunQuery']['userErrors']:
        sys.exit(r['bulkOperationRunQuery']['userErrors'])
    while True:
        op = sp.gql('{currentBulkOperation{status url errorCode objectCount}}')['currentBulkOperation']
        if op['status'] not in ('CREATED', 'RUNNING'):
            break
        time.sleep(5)
    if op['status'] != 'COMPLETED':
        sys.exit(op)
    righe = subprocess.run(['curl', '-s', op['url']], capture_output=True, text=True, check=True).stdout.splitlines()
    prod = {}
    for r in righe:
        x = json.loads(r)
        if '__parentId' not in x:
            x['_media'], x['_var'] = [], []
            prod[x['id']] = x
        elif 'sku' in x:
            prod[x['__parentId']]['_var'].append(x)
        elif 'preview' in x:
            u = ((x.get('preview') or {}).get('image') or {}).get('url')
            if u:
                prod[x['__parentId']]['_media'].append(u)
    return prod


def traduzioni(sp, ids, lingue):
    """{lingua: {id prodotto: (titolo, descrizione html)}}, 250 prodotti per chiamata."""
    alias = ' '.join(f't_{l}: translations(locale:"{LOCALE[l]}"){{key value}}' for l in lingue)
    q = 'query($ids:[ID!]!){translatableResourcesByIds(resourceIds:$ids, first:250){nodes{resourceId %s}}}' % alias
    out = {l: {} for l in lingue}

    def blocco(i):
        return sp.gql(q, {'ids': ids[i:i + 250]})['translatableResourcesByIds']['nodes']
    with ThreadPoolExecutor(4) as ex:
        for nodi in ex.map(blocco, range(0, len(ids), 250)):
            for n in nodi:
                for l in lingue:
                    t = {x['key']: x['value'] for x in n['t_' + l]}
                    if t.get('title'):
                        out[l][n['resourceId']] = (t['title'], t.get('body_html') or '')
    return out


def leggi_feed(url):
    """Item di un feed pubblicato: {id: {campo: valore}} (solo i campi semplici)."""
    dati = subprocess.run(['curl', '-s', url], capture_output=True, check=True).stdout
    x = gzip.decompress(dati).decode() if dati[:2] == b'\x1f\x8b' else dati.decode()
    out = {}
    for it in re.findall(r'<item>(.*?)</item>', x, re.S):
        c = {k: html.unescape(v) for k, v in re.findall(r'<g:([a-z_0-9]+)>([^<]*)</g:\1>', it)}
        out[c.get('id', '')] = c
    return out


def percorsi(lingue):
    SEED.mkdir(exist_ok=True)
    for l in lingue:
        f = leggi_feed(f'{PAGES}feed-{l}.xml.gz')
        d = {i: [c.get('product_type', ''), c.get('custom_label_1', ''), c.get('google_product_category', '')] for i, c in f.items()}
        with gzip.open(SEED / f'percorsi-{l}.json.gz', 'wt', encoding='utf-8') as g:
            json.dump(d, g, ensure_ascii=False, separators=(',', ':'))
        log(l, len(d), 'percorsi salvati')


def variante(p):
    # un solo item per prodotto, come Storeden: la variante più economica (il prezzo «da» del modello)
    vv = [v for v in p['_var'] if v.get('sku') and not re.search(r'[_-]sample$', v['sku'], re.I)]
    con_prezzo = [v for v in vv if float(v.get('price') or 0) > 0]
    return min(con_prezzo, key=lambda v: float(v['price'])) if con_prezzo else (vv[0] if vv else None)


def codice(sku, perc):
    """Su Storeden i prodotti con colori o lato (Xilon, Goman, Vanita…) avevano un solo codice, quello del modello:
    su Shopify le varianti hanno «<modello>-<opzione>». Si riprende il codice di oggi se c'è nel feed attuale."""
    if sku in perc:
        return sku
    pezzi = sku.split('-')
    for n in range(len(pezzi) - 1, 0, -1):
        if '-'.join(pezzi[:n]) in perc:
            return '-'.join(pezzi[:n])
    m = re.match(r'^(.+?)(?:DX|SX|[RL]|/\w+)$', sku)   # lato attaccato al codice (099.200.100R) o finitura (AN-030/01)
    if m and m.group(1) in perc:
        return m.group(1)
    return sku


def escluso(p, v):
    if not v:
        return 'senza codice'
    if re.match(r'campion[ei]\b', p['title'], re.I) or 'campione' in [t.lower() for t in p.get('tags', [])]:
        return 'campione'
    if not p['_media']:
        return 'senza immagine'
    if not float(v.get('price') or 0) > 0:
        return 'prezzo 0 o mancante'
    return None


def item(p, v, l, tr, perc, oggi):
    _, dominio, valuta = LINGUE[l]
    if l == 'it':
        titolo, descr = p['title'], testo(p.get('descriptionHtml'))
    else:
        t = tr.get(p['id'])
        if not t:
            return None, 'non tradotto'
        titolo, descr = spazi(t[0]), testo(t[1])
    prezzo, barrato = float(v['price']), float(v.get('compareAtPrice') or 0)
    # merce in magazzino prima di «su ordinazione», come il feed di oggi (giacenza > 0 = in_stock)
    if (v.get('inventoryQuantity') or 0) > 0:
        disp, data_disp = 'in_stock', None
    elif v.get('inventoryPolicy') == 'CONTINUE':
        giorni = int(float((p.get('giorni') or {}).get('value') or GIORNI_SU_ORDINAZIONE))
        disp, data_disp = 'backorder', (oggi + datetime.timedelta(days=giorni)).strftime('%Y-%m-%dT00:00+01:00')
    else:
        disp, data_disp = 'out_of_stock', None
    cod = codice(v['sku'], perc)
    pt, cl1, gcat = perc.get(cod, ['', '', ''])
    gcat = gcat or categoria_google(p['title'], pt)
    brand = pulisci_brand(p.get('vendor'))
    img = [u.split('?')[0] for u in p['_media']]
    c = [('g:id', cod), ('g:title', titolo[:150]), ('g:description', (descr or titolo)[:5000]),
         ('g:link', f"{dominio}/products/{p['handle']}"), ('g:image_link', img[0])]
    c += [('g:additional_image_link', i) for i in img[1:11]]
    c += [('g:condition', 'new'), ('g:availability', disp)]
    if data_disp:
        c.append(('g:availability_date', data_disp))
    if barrato > prezzo + 0.009:
        c += [('g:price', f'{barrato:.2f} {valuta}'), ('g:sale_price', f'{prezzo:.2f} {valuta}')]
    else:
        c.append(('g:price', f'{prezzo:.2f} {valuta}'))
    mq = float((p.get('mq') or {}).get('value') or 0)
    if mq > 0:
        c += [('g:unit_pricing_measure', f'{mq:g}sqm'), ('g:unit_pricing_base_measure', '1sqm')]
    if brand:
        c.append(('g:brand', brand))
    ean = re.sub(r'\D', '', v.get('barcode') or '')
    if len(ean) in (8, 12, 13, 14):
        c.append(('g:gtin', ean))
    elif brand:
        c.append(('g:mpn', cod))
    else:
        c.append(('g:identifier_exists', 'no'))
    if gcat:
        c.append(('g:google_product_category', str(gcat)))
    if pt:
        c.append(('g:product_type', pt))
    w = ((v.get('inventoryItem') or {}).get('measurement') or {}).get('weight') or {}
    if w.get('value'):
        kg = float(w['value']) * {'GRAMS': 0.001, 'POUNDS': 0.4536, 'OUNCES': 0.02835}.get(w.get('unit'), 1)
        c.append(('g:shipping_weight', f'{round(kg, 3):g} kg'))
    c += [('g:custom_label_0', brand or 'senza brand'), ('g:custom_label_1', cl1 or 'senza categoria'),
          ('g:custom_label_2', disp),
          ('g:custom_label_3', 'sotto 50' if prezzo < 50 else 'sotto 200' if prezzo < 200 else 'sotto 1000' if prezzo < 1000 else 'oltre 1000')]
    q = 999 if disp == 'backorder' else min(int(v.get('inventoryQuantity') or 0), GIACENZA_MAX) if disp == 'in_stock' else 0
    return f'<item><!--q:{q}-->' + ''.join(f'<{k}>{escape(str(x))}</{k}>' for k, x in c) + '</item>\n', None


def scrivi(l, items):
    _, dominio, _ = LINGUE[l]
    testa = ('<?xml version="1.0" encoding="utf-8"?>\n<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0"><channel>\n'
             f'<title>Universo Ceramiche</title><link>{dominio}</link><description>Feed prodotti {l.upper()} (Shopify)</description>\n')
    q = re.compile(r'<!--q:(\d+)-->')
    meta_disp = re.compile(r'<g:availability>backorder</g:availability><g:availability_date>[^<]*</g:availability_date>')
    meta = lambda i: q.sub(r'<g:quantity_to_sell_on_facebook>\1</g:quantity_to_sell_on_facebook>',  # noqa: E731
                           meta_disp.sub('<g:availability>in stock</g:availability>', i))
    for nome, righe in ((f'feed-{l}.xml.gz', [q.sub('', i) for i in items]), (f'feed-meta-{l}.xml.gz', [meta(i) for i in items])):
        with gzip.open(OUT / nome, 'wt', encoding='utf-8', compresslevel=6) as f:
            f.write(testa)
            f.writelines(righe)
            f.write('</channel></rss>\n')


def confronta(l):
    """Feed Shopify contro feed pubblicato di oggi (Storeden): codici, prezzi, disponibilità."""
    nuovo = {}
    with gzip.open(OUT / f'feed-{l}.xml.gz', 'rt', encoding='utf-8') as f:
        for it in re.findall(r'<item>(.*?)</item>', f.read(), re.S):
            c = {k: html.unescape(v) for k, v in re.findall(r'<g:([a-z_0-9]+)>([^<]*)</g:\1>', it)}
            nuovo[c['id']] = c
    vecchio = leggi_feed(f'{PAGES}feed-{l}.xml.gz')
    comuni = set(nuovo) & set(vecchio)
    prezzo = lambda c: c.get('sale_price') or c.get('price')  # noqa: E731
    diff_p = sorted(i for i in comuni if prezzo(nuovo[i]) != prezzo(vecchio[i]))
    diff_d = sorted(i for i in comuni if nuovo[i].get('availability') != vecchio[i].get('availability'))
    return {'shopify': len(nuovo), 'storeden': len(vecchio), 'comuni': len(comuni),
            'solo_shopify': len(set(nuovo) - set(vecchio)), 'solo_storeden': len(set(vecchio) - set(nuovo)),
            'prezzo_diverso': len(diff_p), 'disponibilita_diversa': len(diff_d),
            'esempi_solo_storeden': sorted(set(vecchio) - set(nuovo))[:15], 'esempi_solo_shopify': sorted(set(nuovo) - set(vecchio))[:15],
            'esempi_prezzo': [(i, prezzo(vecchio[i]), prezzo(nuovo[i])) for i in diff_p[:15]],
            'esempi_disponibilita': [(i, vecchio[i].get('availability'), nuovo[i].get('availability')) for i in diff_d[:15]]}


def main():
    a = argparse.ArgumentParser()
    a.add_argument('fase', nargs='?', default='feed')
    a.add_argument('--lingue', default='it,en,fr,de,es')
    args = a.parse_args()
    lingue = args.lingue.split(',')
    if args.fase == 'percorsi':
        return percorsi(lingue)
    OUT.mkdir(parents=True, exist_ok=True)
    sp = Shopify()
    log('esportazione da Shopify…')
    prod = esporta(sp)
    log(len(prod), 'prodotti attivi')
    estere = [l for l in lingue if l != 'it']
    tr = traduzioni(sp, list(prod), estere) if estere else {}
    log('traduzioni', {l: len(tr[l]) for l in estere})
    oggi = datetime.date.today()
    rapporto = {'data': str(oggi), 'prodotti_attivi': len(prod), 'lingue': {}}
    for l in lingue:
        f = SEED / f'percorsi-{l}.json.gz'
        perc = json.load(gzip.open(f, 'rt', encoding='utf-8')) if f.exists() else {}
        items, motivi, visti = [], {}, set()
        for p in prod.values():
            v = variante(p)
            m = escluso(p, v)
            if not m and codice(v['sku'], perc) in visti:
                m = 'codice doppio'
            if m:
                motivi[m] = motivi.get(m, 0) + 1
                continue
            x, m = item(p, v, l, tr.get(l, {}), perc, oggi)
            if m:
                motivi[m] = motivi.get(m, 0) + 1
                continue
            visti.add(codice(v['sku'], perc))
            items.append(x)
        scrivi(l, items)
        rapporto['lingue'][l] = {'item': len(items), 'esclusi': motivi, 'confronto': confronta(l)}
        log(l, len(items), 'item;', json.dumps({k: v for k, v in rapporto['lingue'][l]['confronto'].items() if not k.startswith('esempi')}))
    (OUT / 'rapporto.json').write_text(json.dumps(rapporto, ensure_ascii=False, indent=1))
    log('rapporto in', OUT / 'rapporto.json')


if __name__ == '__main__':
    main()
