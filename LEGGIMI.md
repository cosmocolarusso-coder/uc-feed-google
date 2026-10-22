# Feed Google Merchant Center – Universo Ceramiche

Genera ogni notte, **solo dalle API Storeden**:
- feed Google `feed-<lingua>.xml.gz` (it, en, de, es, fr);
- feed Meta `feed-meta-<lingua>.xml.gz`, uguali a quelli Google ma con «su ordinazione» = `in stock` (Meta non conosce `backorder`);
- feed Clerk `clerk/products-<locale>.json` e `clerk/categories-<locale>.json`. Clerk legge le categorie da `https://raw.githubusercontent.com/cosmocolarusso-coder/uc-feed-google/clerk-categorie/categories-<locale>.json` (ramo riscritto ogni notte): dallo stesso host del file prodotti la seconda richiesta falliva a volte con «Connection reset by peer».

I feed Google e Meta sono pubblicati solo compressi: GitHub Pages ha un limite di 1 GB.
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

## Controlli di completezza (dal 19/10/2026)
- `products/list.json` (tutti i prodotti in una chiamata) come terza fonte dell'elenco: gli uid attivi che l'elenco a pagine salta vengono letti dalla scheda (`saltati_elenco_pagine` in `rapporto.json`).
- Mappa categorie ogni notte: elenco a pagine filtrato per ogni categoria (~250 chiamate). Aggiorna le categorie di tutti i prodotti, perché `PUT products/category.json` non cambia `update_at` (`categorie_aggiornate_da_mappa` nel rapporto). Si applica solo se è stata letta per intero.
- Rotazione schede ridotta a 2.000 a notte (`ROTAZIONE_SCHEDE`).
- `sqmPrice` di Clerk arrotondato a mezzo centesimo per eccesso, come No Gravity.

## Immagini (dal 20/10/2026)
- Google rifiuta le foto oltre 64 megapixel (alcune importazioni Gessi/Flaminia hanno foto 8268×8268) e quelle in formati non supportati, e segnala quelle sotto 500 px.
- Lo script legge le dimensioni di ogni foto dall'intestazione del file (richiesta Range, fuori dal budget API) e le tiene in cache nello stato: un id immagine Storeden non cambia mai. Al massimo `MAX_MISURE` (40.000) misure per giro.
- Nel feed Google la foto principale è la prima valida (meglio se ≥500 px); le foto non valide vengono tolte dalle aggiuntive. Clerk usa sempre la prima foto.
- I prodotti senza nessuna foto valida finiscono in `foto-da-sistemare.csv` (pubblicato con i feed): vanno ricaricate le foto in Storeden.


## Configuratore pavimenti (dal 21/10/2026)
`configuratore.py` aggiunge al feed Clerk, solo per pavimenti e rivestimenti da proporre, i campi che il configuratore usa come filtri:
`cfg_lavoro` (gres, spc, pareti, esterno), `cfg_effetto` (legno, marmo, pietra, cemento, cotto, tinta), `cfg_decoro` (1 = decori, rilievi, muretti, mosaici), `cfg_m2box`, `cfg_giorni`, `cfg_campione` (id del campione a 10 €), `cfg_foto_amb` (foto di una stanza), `cfg_foto_tex` (foto della sola superficie), `cfg_tono` (chiaro, beige, grigio, scuro, colore).
- Lavoro ed effetto vengono da categorie e nome del prodotto; prima il nome («effetto legno»), poi le categorie.
- Foto e tonalità: misurate con Pillow sulle miniature 300×300 (`cdn.storeden.net/secache/<id>`), fino a 3 foto per prodotto; risultati in `stato/immagini.json.gz`, al massimo `MAX_IMMAGINI` (10.000) foto nuove per notte. Le soglie di tonalità sono relative a tutto il catalogo (35% più chiaro = chiaro, 15% più scuro = scuro, il resto caldo o grigio secondo il colore; azzurri e verdi = colore).
- Senza Pillow il feed si genera lo stesso, senza foto e tonalità.

## Giacenza pubblicata (dal 21/10/2026)
I file dei feed sono pubblici: la giacenza esce al massimo **50** («50+», `GIACENZA_MAX`) nel campo `stock` di Clerk e in `quantity_to_sell_on_facebook` di Meta (su ordinazione resta 999). Regole ed elementi di Clerk usano solo `stock > 0` / `stock == 0`, che non cambiano. La giacenza esatta resta solo nello stato privato (cache di GitHub Actions).
