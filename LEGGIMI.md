# Feed Google Merchant Center – Universo Ceramiche

Genera ogni notte `feed-it.xml`, `feed-en.xml`, `feed-de.xml`, `feed-es.xml` e `feed-fr.xml`.
Sostituisce i `g-feed-*.xml` di No Gravity, fermi dal 15 luglio 2026.

## Da dove prende i dati
- **API Storeden** (chiave di sola lettura): prezzo a confezione, giacenza, stato, EAN, peso e immagini.
- **File Clerk di No Gravity** su S3, rigenerati ogni notte: titoli, descrizioni, link e categorie nelle altre lingue.

## Regole principali
- L'ID è il codice prodotto, lo stesso usato dal pixel Meta e da Google Ads.
- Escluso ogni prodotto nascosto, senza immagine, con prezzo 0, senza codice, con codice doppio o campione (codice «…_sample»).
- Disponibilità:
  - `in_stock` se c'è giacenza;
  - `backorder` con data di disponibilità se si vende su ordinazione (giorni presi da packageTime, altrimenti 14);
  - `out_of_stock` negli altri casi.
- Categoria Google ufficiale secondo `REGOLE_CATEGORIA` in `genera_feed.py`; le vostre categorie vanno in `product_type`.
- Piastrelle: `unit_pricing_measure` è la quantità in m² contenuta nella confezione.
- Finché il file Clerk ES è una copia di quello francese, i testi spagnoli si prendono dal vecchio `g-feed-es.xml` di No Gravity (fermo al 15/07/2026). I prodotti creati dopo luglio restano fuori dal feed ES.

## Comandi
```
python3 genera_feed.py --campione          # mostra i campi di un prodotto dalle API
python3 genera_feed.py --limite 200        # prova veloce
python3 genera_feed.py                     # feed completi
python3 genera_feed.py --solo-clerk        # anteprima senza chiave API
```
La chiave API si legge da `STOREDEN_KEY` / `STOREDEN_EXCHANGE`, oppure dal file `.chiave-storeden.txt`, escluso da git, con le righe:
```
key: ...
exchange: ...
```

## GitHub
1. Crea il repository e caricaci questa cartella.
2. Vai in Settings → Secrets and variables → Actions e aggiungi `STOREDEN_KEY` e `STOREDEN_EXCHANGE`.
3. Vai in Settings → Pages e scegli Source = **GitHub Actions**.
4. Nella scheda Actions avvia «Feed Google notturno» a mano la prima volta, poi parte da solo ogni notte.
5. I feed sono pubblicati su `https://<utente>.github.io/<repository>/feed-it.xml`; il riepilogo è in `rapporto.json`.

GitHub Pages su un repository privato richiede un piano a pagamento (Pro o Team). Con il piano gratuito il repository deve essere pubblico. Il codice non contiene segreti e i dati del feed sono comunque pubblici.
