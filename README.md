# Mega Tracker di Offerte

Radar locale per trovare acquisti e confrontare i possibili canali di rivendita. Il catalogo include GPU, CPU e componenti AM4, console, un set LEGO e un utensile. La demo è sintetica e resta concentrata sulle RTX 4070 e sul PC AM4. L’archivio reale parte con il catalogo e può essere alimentato in automatico.

## Avvio

Con Docker Compose 2.24 o successivo:

```sh
docker compose up --build
```

Apri [la dashboard locale](http://127.0.0.1:8000/demo). Il servizio di migrazione prepara PostgreSQL e carica la demo. Il worker genera gli avvisi in pochi secondi. I dati restano nel volume PostgreSQL anche dopo `docker compose down`.

Con Python 3.12 o successivo, dalla cartella del progetto:

```sh
python -m venv .venv
# Attiva .venv con il comando della tua shell.
python -m pip install -e ".[dataset,documents,test]"
tracker run-local
```

`tracker run-local` legge `.env`, aggiorna il database e avvia interfaccia e worker. Le variabili già presenti nella shell hanno precedenza. L’interfaccia ascolta su `127.0.0.1:8000` e puoi scegliere una porta diversa con `--port`. Il worker viene riavviato se termina mentre l’interfaccia rimane attiva. Lascia il comando in esecuzione per ripetere le ricerche e rilancialo dopo un arresto del computer o una modifica al file di configurazione.

Questa modalità usa SQLite in `data/tracker.db` per impostazione predefinita. Le impostazioni di monitoraggio e lo storico restano nel database fra due avvii. Con SQLite tieni un solo worker attivo. Per le scansioni e le immagini installa Tesseract con i dati lingua `ita` ed `eng`. L’immagine Docker li include.

Per avviare interfaccia e worker come processi gestiti separatamente, esporta le variabili nei rispettivi ambienti e usa `alembic upgrade head` e `tracker bootstrap` prima di avviare `tracker worker` e `uvicorn tracker.web:app --host 127.0.0.1 --port 8000`.

Per ricaricare solo la demo:

```sh
tracker reset-demo --yes
tracker worker --once --mode demo
```

Da Compose esegui i comandi CLI con `docker compose run --rm tools`, per esempio `docker compose run --rm tools tracker reset-demo --yes`.

### VM gratuita e database Supabase

Usa un progetto Supabase Free dedicato. Il piano include 500 MB per il database e può mettere in pausa il progetto dopo una settimana di inattività. Su una VM Ubuntu, per esempio Oracle Cloud Always Free se c’è capacità, installa Docker con il plugin Compose 2.24 o successivo e clona il repository. [Piano Supabase Free](https://supabase.com/pricing) · [Oracle Always Free](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)

Copia `.env.cloud.example` in `.env.cloud` sulla VM e rendi il file leggibile solo dal tuo utente. In Supabase apri **Connect** e usa la stringa **Session pooler** sulla porta 5432 se la VM è IPv4. Con IPv6 puoi usare **Direct connection**. Imposta `TRACKER_DATABASE_URL` con la stringa del progetto e la password del database codificata nell’URL. Scarica il certificato del database da Supabase e salvalo in `data/supabase-ca.crt` sulla VM per la verifica TLS. L’app usa PostgreSQL nello schema `tracker`. Puoi disattivare la Data API del progetto, che l’app non usa. [Connessione PostgreSQL e TLS](https://supabase.com/docs/guides/database/connecting-to-postgres) · [Data API](https://supabase.com/docs/guides/api/securing-your-api)

Per riprendere l’archivio, ferma il tracker locale e copia il suo `data/tracker.db` sulla VM come `data/tracker-restore.db`. Esegui i comandi seguenti prima del primo avvio. Il ripristino accetta solo tabelle del tracker vuote, conserva gli identificativi che legano storico e attività e apre il file SQLite in sola lettura.

```sh
docker compose --env-file .env.cloud -f compose.cloud.yaml run --rm --build migrate alembic upgrade head
docker compose --env-file .env.cloud -f compose.cloud.yaml run --rm --build tools tracker restore-sqlite /app/data/tracker-restore.db
```

Avvia manualmente dashboard e worker quando vuoi eseguire il tracker. La porta web resta su loopback. Con un tunnel SSH, apri `http://127.0.0.1:8800/real/alerts` sul tuo computer.

```sh
# Sulla VM
docker compose --env-file .env.cloud -f compose.cloud.yaml up -d --build

# Dal tuo computer
ssh -N -L 8800:127.0.0.1:8000 utente@vm
```

Per fermarlo, usa `docker compose --env-file .env.cloud -f compose.cloud.yaml down` sulla VM. L’archivio resta in Supabase. Configura in `.env.cloud` le fonti autorizzate che vuoi eseguire sul server. L’automazione Codex già configurata sul PC continua a usare la sua cartella locale.

## Percorso demo

| Pagina | Caso da provare |
| --- | --- |
| **Occasioni** | RTX 4070 a €250 più €9 di spedizione. Apri il dettaglio per confrontare eBay Italia e Subito, le commissioni e il margine. Vedi il prezzo precedente e l’unità ripubblicata. |
| **Revisione** | Apri «PC gaming vecchio» e correggi il prodotto. La ventola e la 4070 SUPER mantengono identità distinte. |
| **Avvisi** | Apri la valutazione salvata al momento della notifica. |
| **Lotti e stock** | Confronta il PC intero con i pezzi. Seleziona GPU ASUS, Ryzen 5600, kit B550 e case per salvare un piano. Lo stock include quantità e articoli da verificare. |
| **Richieste e budget** | Cerca le compatibilità con la richiesta demo. Con €420 è visibile l’esclusione di una ripubblicazione della stessa GPU. |
| **Inventario** | Modifica ed esporta la bozza esistente oppure registra l’esito di una vendita. |
| **Simulazione** | Confronta le due strategie sullo stesso periodo e cambia la latenza o l’ipotesi di disponibilità. |
| **Sorgenti** | Apri le ricerche per piattaforma, prova gli esempi CSV e controlla le vendite con prezzo pagato ignoto. |

## Raccolta e vendita reale

Imposta i costi del tuo account con `TRACKER_FEE_<FONTE>` e `TRACKER_OUTBOUND_<FONTE>`. La commissione è una frazione, per esempio `0.12`. Il radar mostra il margine quando sono disponibili sia il costo consegnato sia i costi di vendita. Le percentuali della demo sono ipotesi sintetiche.

| Piattaforma | Acquisto | Rivendita |
| --- | --- | --- |
| eBay Italia | Browse API, feed o file | Confronti da vendite importate, bozza, esportazione e accesso all’area vendita |
| Subito | Lettura automatica degli avvisi email delle ricerche salvate, feed autorizzato o file | Confronti da vendite importate e bozze |
| Amazon Italia | Creators API con approvazione di Amazon, feed autorizzato o file | Confronti da vendite importate e bozze |
| AliExpress | Affiliate API TOP, feed autorizzato o file | Confronti da vendite importate e bozze |
| Vinted | Feed di ricerca autorizzato o segnalazioni dall’indice Brave. Il lettore email è utilizzabile se la fonte invia annunci con link diretti | Confronti da vendite importate e bozze |
| Wallapop, Facebook Marketplace | Feed autorizzato, segnalazioni dall’indice Brave oppure file | Confronti da vendite importate e bozze |
| Altri provider | Configurabili da CLI con feed e permesso per fonte | Comparabili importati con campo `destination` |

Per eBay, configura `EBAY_CLIENT_ID` ed `EBAY_CLIENT_SECRET` di produzione. La Browse API restituisce inserzioni attive. Per i prezzi pagati importa gli ordini o confronti ottenuti legittimamente. Puoi controllare i confronti con Ricerca prodotti nel tuo account eBay. [eBay Browse API](https://developer.ebay.com/api-docs/buy/api-browse.html)

Per **Subito**, salva le ricerche sul sito e attiva gli avvisi via email. Fai recapitare questi avvisi in una cartella IMAP dedicata e imposta `TRACKER_MAIL_SUBITO_HOST`, `_USER`, `_PASSWORD`, `_FOLDER` e `_SENDER` (indirizzo esatto del mittente). Usa una password per applicazione se prevista dal tuo provider. Il worker legge la cartella in TLS e in sola lettura, riprende dagli UID già visti e salva i dati normalizzati di annunci con un link diretto riconoscibile. La frequenza di invio resta quella di Subito. [Ricerche salvate](https://assistenza.subito.it/hc/it/articles/360002172138-Salvare-una-ricerca), [Condizioni del servizio](https://info.subito.it/policies/condizioni-generali.htm)

Per **Amazon**, abilita `AMAZON_CREATORS_TRACKER_APPROVED=1` se Amazon ha approvato l’uso di questi dati per il tracker, quindi imposta `AMAZON_CREATORS_ID`, `_SECRET` e `_TAG`. L’adattatore usa le credenziali europee e cerca sul mercato italiano. Per **AliExpress**, imposta `ALIEXPRESS_TOP_KEY`, `_SECRET` e `ALIEXPRESS_TRACKING_ID` da un’app abilitata alla ricerca Affiliate TOP. La ricerca richiede risultati per la consegna in Italia e importi in EUR. In entrambi i casi il prezzo di catalogo resta indicativo se manca il costo di spedizione. [Amazon Creators API](https://programma-affiliazione.amazon.it/creatorsapi/docs/en-us/api-reference/operations/search-items), [Politiche Amazon Associates](https://affiliate-program.amazon.com/help/operating/policies), [AliExpress Product Query](https://developer.alibaba.com/docs/api.htm?apiId=45803)

La guida di **Vinted** descrive un contatore di nuovi annunci all’apertura della ricerca salvata. **Wallapop** offre notifiche delle ricerche salvate nell’app. Il lettore email è riutilizzabile sui marketplace inclusi quando una fonte invia effettivamente messaggi con titoli e link diretti delle offerte, sostituendo `SUBITO` nelle variabili con la fonte corrispondente. [Ricerche salvate su Vinted](https://www.vinted.it/help/392/435-save-your-searches), [Avvisi delle ricerche su Wallapop](https://assistenza.wallapop.com/hc/it/articles/4406242256017-Salvare-ricerche-e-ricevere-avvisi-su-Wallapop)

Per attivare un feed imposta `TRACKER_FEED_<FONTE>_URL`, `_TOKEN` facoltativo e `_KIND=sale` se restituisce vendite. Il tracker invia `q` e il cursore. Il formato di risposta è `{"items":[...],"next_cursor":"..."}`. I campi delle righe sono gli stessi degli import manuali. Per Soldgraph serve un feed compatibile o un adattatore basato sul contratto del provider. La configurazione di esempio contiene segnaposto.

### Ricerca di annunci indicizzati

Su questo computer puoi collegare l’automazione Codex autorizzata impostando `TRACKER_CODEX_DISCOVERY=1` in `.env` e avviando `tracker run-local`. Il worker crea `data/codex-index/requests.json` dai prodotti monitorati con segnalazioni incomplete attive. L’automazione usa quei prodotti e consegna al worker i titoli e i link pubblici. I risultati compaiono in **Avvisi**, con prezzo e disponibilità da verificare. Per le fonti già collegate con API, feed, Brave o email si usa il collegamento configurato.

Per Subito e Vinted, l’automazione verifica le singole pagine pubbliche. A ogni ricerca ricontrolla anche fino a cinque annunci già indicizzati negli ultimi sette giorni, dando precedenza a quelli verificati meno di recente. Se la ricerca di nuovi annunci fallisce, gli esiti dei ricontrolli vengono comunque salvati e la ricerca resta in coda. Gli avvisi venduti, riservati, dichiarati in trattativa o rimossi passano alla sezione richiudibile **Annunci non disponibili**. Una prenotazione può rientrare nell’elenco quando la pagina conferma che l’annuncio è di nuovo disponibile. L’ora indica quando è stata raccolta l’ultima evidenza. Le trattative private e le pagine che non mostrano uno stato chiaro restano da verificare.

L’automazione e il worker devono poter usare la stessa cartella di lavoro. Il calendario di ogni prodotto resta quello impostato nel tracker. Disattiva la variabile e riavvia per togliere la coda di ricerca Codex. Le cartelle di scambio restano locali e gli avvisi già raccolti restano nell’archivio.

Le fonti senza API o feed configurato possono condividere una chiave Brave Search. Usa un piano che consenta di conservare i risultati prima di abilitare questa configurazione nel file `.env`. [Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search), [Condizioni di Brave Search API](https://api-dashboard.search.brave.com/terms-of-service)

```dotenv
TRACKER_BRAVE_API_KEY=
TRACKER_BRAVE_STORAGE_ALLOWED=1
TRACKER_BRAVE_SOURCES=vinted,wallapop,facebook
TRACKER_BRAVE_MAX_PAGES=1
```

In **Prodotti monitorati**, seleziona le fonti e **Mostra anche offerte da verificare con dati incompleti**. Abilita **Storico consentito** per le fonti selezionate. Il worker interroga l’indice a ogni ciclo e accetta solo link ad annunci compatibili con il prodotto. La pagina **Avvisi** mostra il link identificato e indica che disponibilità, prezzo e costi restano da controllare. La copertura dipende dalle pagine indicizzate. Il limite configurabile da 1 a 10 pagine vale per prodotto, marketplace e ciclo, con fino a 20 risultati per chiamata.

In **Dati reali → Sorgenti → Prodotti monitorati** seleziona un prodotto e i marketplace, poi imposta la frequenza e le soglie. I monitoraggi si possono modificare o mettere in pausa. Il worker usa la frequenza più breve delle ricerche che condividono la stessa casella di una fonte. Per gli avvisi di margine servono costo completo, disponibilità e confronti sufficienti. Se selezioni anche le offerte da verificare, queste appaiono nella stessa pagina con ciò che manca.

Per attivare conservazione e monitoraggio da CLI:

```sh
tracker source ebay --allow-history
tracker monitor rtx4070-asus-dual --source ebay --source subito --minutes 60 --max-cost 280 --min-profit 25 --signals
tracker collect
```

Lascia attivo il servizio worker, incluso in Docker Compose. I risultati sono visibili nella pagina **Avvisi**. Telegram si può attivare in seguito con `TRACKER_TELEGRAM_ENABLED=1` e le credenziali del canale. L’area Inventario registra canale, prezzi, esiti, costi e resi. Le bozze si esportano per la pubblicazione manuale.

## Import e archivio per l’AI

- **Offerte.** `external_id,title,condition,price,shipping,currency,market,status,quantity,url`. Prezzo mancante, spedizione ignota e stato incerto hanno significati distinti. Conserva `fx_rate,fx_date,fx_source` per importi convertibili. La spedizione gratuita deve essere indicata con zero.
- **Vendite.** Aggiungi `sold_at,verified,paid_price,shown_price,sale_format,order_id,listing_id,destination`. Usa un ID distinto per ogni riga d’ordine. Il dataset usa il totale pagato diviso per la quantità. L’esito verificato con importo ignoto può contribuire alle metriche di mercato.
- **Tempi.** Includi `observed_at` solo quando la data di disponibilità è documentata e seleziona l’opzione corrispondente nell’import. Altrimenti vale il momento dell’import. Ai fini del dataset usa `decision_at` (o `published_at`) e uno snapshot osservato del relativo `listing_id`. In alternativa, fornisci `decision_title,decision_condition,decision_format` da una copia documentata al momento della decisione.
- **Stock.** CSV, JSON o foglio XLSX con intestazioni. Per i PDF il parser prova righe con codice, titolo, quantità, condizione e prezzo, da verificare in anteprima. Le scansioni usano OCR.
- **Permessi.** Lo storico registra dati normalizzati, provenienza, versioni e date di osservazione. Le colonne con dati anagrafici non vengono importate. I file grezzi delle transazioni non vengono salvati. L’export per l’AI richiede un permesso esplicito a livello di fonte. Tieni i dati reali fuori da Git.

```sh
tracker import offer local tracker/static/esempio-offerte.csv --mode demo --preview
tracker archive data/storico.jsonl --mode real
tracker dataset data/demo.parquet --mode demo
tracker dataset data/training.parquet --mode real
tracker replay --start 2025-01-01 --end 2025-03-01 --budget 420 --assume-availability
```

Il JSONL esporta lo storico autorizzato. Il Parquet ha un manifest con schema, fonti, esclusioni e cutoff. I comparabili del dataset e del radar condividono lo stesso calcolo. Il modello AI e il suo obiettivo restano per la fase successiva. La valutazione operativa usa mediana e quartili.

## Verifica

```sh
python -m pytest -q
docker compose run --rm tools sh -c 'TRACKER_TEST_POSTGRES_URL="$TRACKER_DATABASE_URL" pytest -q'
```

I test coprono entrambi i database quando configurati, la concorrenza della coda, i costi, la reimportazione, la deduplica, le pagine demo, il flusso anteprima–import–dashboard e il connettore eBay con risposte simulate. Le chiamate reali richiedono credenziali e autorizzazioni del relativo servizio.
