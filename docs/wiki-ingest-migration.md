# Migracja ingestu z projektu legal

Implementacja wykonawcza: `src/basic_memory/wiki_ingest/`. Interfejs dla wszystkich
hostów: narzędzia MCP `wiki_ingest` i `wiki_plan` oraz istniejące narzędzia notatek,
schematów, wyszukiwania i grafu. Pełny protokół serwer dostarcza przez `man` oraz
zasób `memory://man/wiki-ingest(3)`. Instrukcje interpretacji prawnej należą do
grounding-prawo; basic-memory nie zawiera danych konkretnych spraw.

| Dotychczasowy element | Właściciel i odpowiednik |
|---|---|
| `kolejka_bm.py`, `_ingest/rejestr_bm.json` | `wiki_ingest`: refresh/status/start/finish/skip; stan i dziennik w notatce MCP |
| `ingest_prep.py` | start/read; pełny istniejący sidecar, sumy SHA-256, OCR przez extract |
| `bm_mcp.py`, `wgraj_bm.py` | Istniejący autoryzowany KnowledgeClient, import migrate i schema_validate |
| `buduj_indeks.py` | index, graf notatek i istniejący projektor wiki |
| `kontrola_pokrycia.py` | audit; kontrola tokenów źródła w notatkach tematycznych |
| `lint_jezyka.py` | redakcja w skillu pisz-po-polsku; źródło pozostaje dosłowne |
| `plany.py`, `_plany/stan` | wiki_plan; historyczne plany przez read_note/migrate |
| `bm_hook.py` | Kontrole zakończenia ingestu na serwerze; recap przez skill w każdym hoście |
| `deleguj.py` | Pracę prowadzi wywołujący model; uruchamianie lokalnego CLI nie jest wymagane do ingestu |
| `konwersja_wiki.py`, `naprawa_dryfu.py`, `weryfikuj_import.py` | Historyczne operacje jednorazowe; migrate zachowuje plik dosłownie i weryfikuje odczytem zamiast ponawiać korekty konkretnych spraw |
| `inwentarz.py` | refresh obejmuje SHA-256 źródeł i sidecarów; migrate zachowuje SHA-256 migawki |
| `00_basic_memory`, `00_wiki`, `_plany/raporty` | Historyczne dane, pozostają bez zmian; nie są aktywnym silnikiem ani katalogiem danych serwera |

## Wdrożenie

1. Zbuduj i uruchom wersję serwera zawierającą oba narzędzia. Uzupełnij konfigurację
   montowań i `BASIC_MEMORY_INGEST_SOURCES` zgodnie z podręcznikiem. Konfiguracja
   celowo nie zgaduje UUID, ścieżek ani poświadczeń operatora.
2. Sprawdź `tools/list` oraz `man("wiki-ingest")` przez docelowy konektor MCP.
3. `status`, następnie `refresh`; `migrate` pozwala zachować rejestr i historyczne
   migawki pojedynczo. Sprawdź odczyt przed przetworzeniem kolejnego pliku.
4. Wykonaj pełny przepływ na jednym dokumencie: start, ewentualnie extract,
   read do końca, notatki tematyczne, audit, finish, index.
5. Zaktualizuj grounding-prawo i konfigurację klientów legal dopiero po wdrożeniu
   serwera. Zmiana checkoutu, przypięcie submodułu i PR nie aktualizują działającego
   kontenera ani listy narzędzi w już otwartym czacie.

Wycofanie: starsza wersja narzędzi jest w historii Git legal. Oryginały i migawki
pozostają niezmienione; nowe notatki mają osobną przestrzeń `_ingest/` i pełną
proweniencję. Nie usuwaj nowych notatek ani źródeł podczas wycofania wdrożenia.

Przykładowa nakładka montowań jest w `docker-compose.ingest.example.yml`.
Uzupełnij zmienne `INGEST_*` w środowisku operatora i połącz ją z konfiguracją NAS.
`INGEST_OCR_ROOT` oznacza ścieżkę widoczną dla the-ocr, nie dla klienta czatu.
Dokumenty muszą być udostępnione obu serwerom pod odpowiednimi ścieżkami.
Migracja obejmuje też historyczne pliki Markdown w `_narzedzia/notatki_bm`.
