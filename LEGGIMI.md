# Feed Google Merchant Center – Universo Ceramiche

Genera ogni notte `feed-it.xml`, `feed-en.xml`, `feed-de.xml`, `feed-es.xml` e `feed-fr.xml`, tutti **solo dalle API Storeden**.
Sostituisce i `g-feed-*.xml` di No Gravity, fermi dal 15 luglio 2026.

## Come legge i dati
1. **Elenco prodotti** (circa 130 chiamate, ogni notte): prezzo, prezzo scontato, giacenza, stato, foto e data di ultima modifica di tutti i prodotti.
2. **Scheda completa** (EAN, peso, m² per confezione, su ordinazione, giorni di consegna, galleria, brand, categorie): letta solo per i prodotti nuovi o modificati.
3. **Traduzioni** di titolo e descrizione in ES, EN, DE e FR, e dei nomi delle categorie: lette solo per i prodotti nuovi o modificati.
   - L'elenco si legge **due volte** e si uniscono i risultati: mentre lo si scorre, i prodotti modificati cambiano pagina e qualcuno viene saltato (18/10/2026: 34 prodotti in vendita mancanti).
   - Un prodotto già noto ma assente dall'elenco si **ricontrolla con la sua scheda** prima di toglierlo: si toglie solo se Storeden risponde «Product not found» o se non è più attivo; se la scheda non si legge, resta com'era fino al giro dopo.
   - Ogni giro rilegge anche fino a `ROTAZIONE_SCHEDE` (5.000) schede tra quelle lette da più tempo, dopo le nuove/modificate e prima delle traduzioni. Serve per i cambi che non aggiornano la data di modifica, per esempio le categorie cambiate via API (CHG-427). Le traduzioni restano valide se titolo e descrizione italiani non cambiano.
4. Tutto il resto si riprende dallo stato della notte prima, `stato/stato.json.gz`. Lo stato sta nella cache privata di GitHub Actions e non viene pubblicato, perché contiene le giacenze esatte.

Storeden permette 100.000 chiamate ogni 24 ore per chiave e le traduzioni costano una chiamata per campo:
- ogni giro si ferma a `MAX_CHIAMATE` (90.000), pubblica quello che ha e riprende la notte dopo;
- il primo riempimento delle traduzioni richiede qualche notte;
- fino ad allora i feed esteri hanno meno prodotti (motivo «traduzione non ancora letta» in `rapporto.json`).

## Regole principali
- L'ID è il codice prodotto, lo stesso usato dal pixel Meta e da Google Ads.
- Esclusi i prodotti nascosti, senza foto, con prezzo 0, senza codice, con codice doppio o campione (codice «…_sample»).
- Nelle lingue estere sono esclusi anche i prodotti senza traduzione in Storeden (motivo «non tradotto in Storeden»).
- Disponibilità come nel tema:
  - `backorder` con data se «Su ordinazione» (`data.unlimited`);
  - `in_stock` se c'è giacenza;
  - `out_of_stock` negli altri casi.
- Categoria Google ufficiale secondo `REGOLE_CATEGORIA` in `genera_feed.py`; le vostre categorie, tradotte, vanno in `product_type`.
- Piastrelle: `unit_pricing_measure` sono i m² nella confezione, così Google mostra il prezzo al m².
- Link: `https://universoceramiche.<dominio>/product/<id>`.

## Comandi
```
python3 genera_feed.py                     # giro normale
python3 genera_feed.py --limite 200        # prova veloce (non salva lo stato)
python3 genera_feed.py --lingue it,es      # solo alcune lingue
python3 genera_feed.py --campione          # mostra i campi di un prodotto dalle API
```
La chiave API si legge da `STOREDEN_KEY` / `STOREDEN_EXCHANGE`, oppure dal file `.chiave-storeden.txt` (escluso da git).

## GitHub
- Il job «Feed Google notturno» parte alle 01:30 UTC; si può anche avviare a mano da Actions → Run workflow.
- I feed sono su `https://cosmocolarusso-coder.github.io/uc-feed-google/feed-<lingua>.xml`; il riepilogo è in `rapporto.json`.
