# Zewnętrzny inwentarz dokumentów (DSM / wiki_ingest)

`wiki_ingest` **nie uruchamia modelu i nie ingestuje automatycznie**. `refresh`
uzgadnia tylko inwentarz; model wywołuje osobno `start/read/write_note/finish`.

## DSM Task Scheduler
Zainstaluj ten skrypt na NAS z repo (Python >=3.10), np.
`/volume2/docker/basic-memory/scripts/build_document_inventory.py`.
Uruchamiaj co 15 minut jako zadanie DSM (lub cron):

```sh
/usr/bin/python3 /volume2/docker/basic-memory/scripts/build_document_inventory.py \
  --root /volume1/dokumentacja \
  --output /volume2/docker/basic-memory/knowledge/_inventory/legal-documents.json
```

Zadanie powinno mieć dostęp do odczytu udziału i zapisu do katalogu
`_inventory`. Skrypt skanuje system plików raz na harmonogram, nie czyta
zawartości dokumentów i zapisuje atomowo. W szczególności wyklucza
`33_WIDEO/*/frame_*.jpg` i `33_WIDEO/*/ramka_*.jpg`.
Zachowuje właściwe wideo, raporty i zwykłe dokumenty.

## MCP Basic Memory
Dopiero po pierwszym udanym przebiegu skanera i wdrożeniu kodu tego PR
dodaj do mapowania projektu `legal` w `BASIC_MEMORY_INGEST_SOURCES`:

```json
{
  "inventory_index": "/app/data/_inventory/legal-documents.json",
  "inventory_expected_root": "/volume1/dokumentacja",
  "inventory_max_age_seconds": 7200
}
```

Ten obiekt **uzupełnia** istniejącą konfigurację projektu; nie zastępuje
`documents`, `legacy_workspace`, `ocr_url` i `ocr_token_env`.
Jeżeli plik nie istnieje, ma błędny format albo jest przeterminowany,
`refresh` sygnalizuje błąd zamiast wracać do pełnego `os.walk`.

Manifest zapisuje `identity` (device/inode), `stamp` (wielkość i mtime)
i zestaw `relocations`. Pozwala wykryć typową zmianę nazwy na tym samym
wolumenie bez hashowania wszystkich plików. **To nie aktualizuje jeszcze
odsyłaczy w notatkach Basic Memory:** wymagany jest osobny, testowany
mechanizm przenoszenia tożsamości `Entry` pomiędzy shardami i
aktualizacji `source_path`. Przy zmianie wolumenu inode nie wystarczy
do rozpoznania przeniesienia; taki przypadek oznacz jako niepewny.

Przed produkcyjnym przełączeniem uruchom testy
`pytest tests/wiki_ingest/test_external_inventory.py`, wykonaj kopię
konfiguracji stacku oraz zweryfikuj poprawność inwentarza.
