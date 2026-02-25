# Nematodes Dashboard (HLB AALTJES ANALYSE)

End-to-end Python 3.11 applicatie voor:
- inlezen van HLB **AALTJES ANALYSE** PDF’s,
- splitsen van meerdere samples/monsters binnen één PDF,
- opslaan in SQLite met caching,
- handmatig + fuzzy matchen van perceelnamen,
- visualisatie in Streamlit,
- export naar Excel.

## Inhoud
- [1. Vereisten](#1-vereisten)
- [2. Installatie](#2-installatie)
- [3. Snel starten](#3-snel-starten)
- [4. Werking ingest (`ingest.py`)](#4-werking-ingest-ingestpy)
- [5. Werking dashboard (`app.py`)](#5-werking-dashboard-apppy)
- [6. Datamodel (SQLite)](#6-datamodel-sqlite)
- [7. Bestandsstructuur](#7-bestandsstructuur)
- [8. Foutafhandeling en troubleshooting](#8-foutafhandeling-en-troubleshooting)

## 1. Vereisten
- Python **3.11**
- Pakketten uit `requirements.txt`:
  - `pdfplumber`
  - `pandas`
  - `openpyxl`
  - `rapidfuzz`
  - `streamlit`
  - `plotly`

## 2. Installatie
Maak bij voorkeur een virtual environment.

### Linux/macOS
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### Windows (PowerShell)
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 3. Snel starten

### 3.1 Parser self-test
```bash
python ingest.py --self_test
```
Dit voert basischecks uit op regex/parsing (vrijlevende + cysteaaltjes + NL-datum parsing).

### 3.2 Batch ingest van PDF-map
```bash
python ingest.py --pdf_dir /pad/naar/pdfs --db_path nematodes.db
```

Optionele argumenten:
- `--failed_path failed_samples.jsonl` (default: `failed_samples.jsonl`)

### 3.3 Start Streamlit dashboard
```bash
streamlit run app.py
```
Open daarna de URL die Streamlit toont (meestal `http://localhost:8501`).

## 4. Werking ingest (`ingest.py`)

### 4.1 Splitsen van meerdere samples in één PDF
De ingest leest tekst per pagina met `pdfplumber` en splitst vervolgens sample-blocks op basis van:
- `Monsternummer: <id>`
- Pagina zonder monsternummer wordt als **continuation** aan het laatste actieve sample toegevoegd.

Hierdoor werken zowel:
- PDF met 1 sample,
- PDF met tientallen samples,
- samples die over meerdere pagina’s lopen.

### 4.2 Metadata die wordt opgehaald
Voor elk sample-block worden (indien aanwezig) o.a. deze velden gelezen:
- `Debiteurnummer`
- `Perceel` (raw name)
- `Monsternummer`
- `Soort monster`
- `Datum ontvangst`
- `Datum verslag`

Datums met Nederlandse maandnamen worden waar mogelijk naar ISO omgezet (`yyyy-mm-dd`).

### 4.3 Resultaat parsing
Ondersteunde layouts:

1. **Vrijlevende aaltjes**
   - parser zoekt regels in het blok onder “Vrijlevende aaltjes”
   - species + integer waarde
   - unit: `per_100g`
   - categorie-labels worden genegeerd
   - indicatieregel `Cysteaaltjes 0 *` in de vrijlevende sectie wordt genegeerd

2. **Cysteaaltjes tabel (echte tabel)**
   - herkend op kopregel met o.a. `aantal cysten`, `aantal lle`, `besmettingsgraad`
   - opgeslagen als `analysis_type='cysteaaltjes'`
   - unit altijd genormaliseerd naar `per_200cc` (zowel 200 ml als 200 cc)
   - velden: `cysts`, `lle`, `infection_class`

### 4.4 Caching / upsert gedrag
- Per PDF wordt `sha256` berekend.
- Als dezelfde hash eerder volledig is verwerkt, wordt die PDF overgeslagen.
- `samples` wordt ge-upsert op `sample_id`.
- `results_long` wordt per sample eerst verwijderd en daarna opnieuw gevuld (delete+replace).

### 4.5 Failed samples logging
Bij parse-problemen of geen resultaten wordt gelogd naar:
- `failed_samples.jsonl`

Per regel o.a.:
- `sample_id`
- `reason`
- `excerpt` (ingekorte tekst voor debug)

## 5. Werking dashboard (`app.py`)
De app heeft 4 tabbladen:

### 5.1 Import
- Kies map met PDF’s **of** upload PDF-bestanden.
- Klik **Importeer / Update**.
- Toont telling van nieuwe en overgeslagen samples.

### 5.2 Percelen matchen
- Toont unieke `raw_field_name` waarden.
- Per raw waarde kun je een officiële `field_name` invullen.
- Fuzzy suggesties (RapidFuzz top-5) op basis van:
  - bestaande mappings in DB,
  - optioneel geüploade CSV met officiële perceelnamen.
- Bij opslaan wordt zowel `field_mapping` als `samples.field_name` bijgewerkt.

### 5.3 Analyse & Grafieken
Filters:
- perceel (gematchte naam of raw fallback),
- analysis type (`vrije_aaltjes` / `cysteaaltjes`),
- species multi-select,
- datum range op `report_date`.

Grafieken (Plotly):
- tijdlijn per species,
- top soorten op laatste meting,
- trend t.o.v. vorige meting.

Cysteaaltjes heeft extra keuze voor y-as: `cysts` of `lle`.

Onder de grafiek staat een tabel met CSV download.

### 5.4 Export
- Download Excel met 3 sheets:
  - `samples`
  - `results_long`
  - `field_mapping`

## 6. Datamodel (SQLite)
Databasebestand standaard: `nematodes.db`

### `samples`
- `sample_id` TEXT PRIMARY KEY
- `raw_field_name` TEXT
- `field_name` TEXT
- `report_date` TEXT
- `receive_date` TEXT
- `sample_type` TEXT
- `debtor_id` TEXT
- `source_pdf` TEXT
- `source_pdf_hash` TEXT

### `results_long`
- `id` INTEGER PRIMARY KEY AUTOINCREMENT
- `sample_id` TEXT
- `analysis_type` TEXT (`vrije_aaltjes` of `cysteaaltjes`)
- `species` TEXT
- `value` INTEGER
- `unit` TEXT (`per_100g` of `per_200cc`)
- `cysts` INTEGER
- `lle` INTEGER
- `infection_class` TEXT

### `field_mapping`
- `raw_field_name` TEXT PRIMARY KEY
- `field_name` TEXT

## 7. Bestandsstructuur
```text
.
├── app.py
├── ingest.py
├── requirements.txt
├── LICENSE
└── README.md
```

## 8. Foutafhandeling en troubleshooting

### `ModuleNotFoundError` bij starten
Installeer dependencies opnieuw in de actieve venv:
```bash
pip install -r requirements.txt
```

### `streamlit: command not found`
Je zit waarschijnlijk niet in je virtualenv, of Streamlit is niet geïnstalleerd.
Controleer:
```bash
which python
which pip
pip show streamlit
```

### Geen data zichtbaar in dashboard
- Controleer of ingest is uitgevoerd op juiste map.
- Controleer of je in de app naar het juiste DB-pad wijst.
- Bekijk `failed_samples.jsonl` voor parse-uitval.

### PDF parsing afwijkingen
HLB-layouts kunnen kleine variaties hebben. Als parser een sample niet herkent:
- check `failed_samples.jsonl` excerpt,
- voeg een testcase toe in `self_test()` en pas regex/parsingregels aan.

---

## Quick command reference
```bash
# Install
pip install -r requirements.txt

# Self-test
python ingest.py --self_test

# Ingest folder
python ingest.py --pdf_dir /pad/naar/pdfs --db_path nematodes.db

# Run UI
streamlit run app.py
```
