# Migracja ingestu z projektu legal

Implementacja wykonawcza: `src/basic_memory/wiki_ingest/`. Interfejs dla wszystkich
hostów: narzędzia MCP `wiki_ingest` i `wiki_plan` oraz istniejące narzędzia notatek,
schematów, wyszukiwania i grafu. Pełny protokół serwer dostarcza przez `man` oraz
zasób `memory://man/wiki-ingest(3)`. Instrukcje interpretacji prawnej należą do
grounding-prawo; basic-memory nie zawiera danych konkretnych spraw.

| Dotychczasowy element | Właściciel i odpowiednik |
|---|---|
| `kolejka_bm.py`, `_ingest/rejestr_bm.json` | `wiki_ingest`: refresh/status/start/finish/skip; stan i dziennik w notatce MCP |
| `ingest_prep.py` | start/read; kandydat sidecara, kontrola PDF stronami, sumy SHA-256, OCR przez extract |
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
   dla PDF inspect_page/verify_page każdej strony i ponownie start,
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

## Uwierzytelnienie OCR

`ocr_token_env` wskazuje nazwę zmiennej środowiskowej z tokenem Bearer usługi the-ocr.
Sam sekret nie trafia do notatek ani konfiguracji źródeł. Brak skonfigurowanego tokenu
zatrzymuje ekstrakcję. Jeśli `ocr_root` pominięto, serwer przesyła plik przez MCP jako
`content_base64` z nazwą pliku; limit `max_source_bytes` wynosi domyślnie 100 MiB.
To pozwala użyć OCR bez współdzielonego montowania katalogu dokumentów.

## Weryfikacja wdrożenia NAS 2026-10-18

Wdrożono rewizję `f834fbbe` jako `mcp-basic-memory:wiki-ingest-f834fbbe`.
Katalog źródeł projektu legal: `/volume1/dokumentacja` → `/ingest/documents`.
Historyczny workspace: `/volume1/legal` → `/ingest/legacy` (tylko odczyt).
OCR: `http://mcp-ocr:8000/mcp`, token pobierany ze zmiennej `BASIC_MEMORY_OCR_TOKEN`,
przesyłanie plików bez wspólnego montowania przez `content_base64`.
Konfiguracja aktywnego stacka Portainera 18 jest w jego `docker-compose.yml` i `stack.env`.
Zachowano kopie poprzedniej konfiguracji oraz obraz `mcp-basic-memory:before-wiki-ingest-20261018`.

Serwer wystawia 29 narzędzi; `wiki_ingest` i `wiki_plan` są widoczne w `tools/list`.
Osobny projekt `wiki-ingest-smoke` przeszedł przez MCP pełny przepływ na syntetycznym HTML:
refresh/start/extract/read/write_note/finish/index oraz create/step/finish planu.
Projekt legal ma poprawnie skonfigurowany status, bez rozpoczęcia ingestu akt.

## Bramka zgodności PDF i sidecara

Nowa wersja wymaga PyMuPDF (zależność instalowana z pakietem). `start` nie publikuje
sidecara PDF przed wizualną kontrolą wszystkich stron. `inspect_page` zwraca obraz
oryginału i receipt; `verify_page` przyjmuje poprawioną transkrypcję oraz opis kontroli.
Oryginał PDF i plik sidecara pozostają bez zmian. Zmiana któregokolwiek unieważnia
kontrolę poprzedniej wersji. Klient musi obsługiwać obrazy MCP.

Historyczne notatki źródłowe nie są usuwane ani automatycznie poprawiane. Aktywny
import utworzony starą wersją jest zatrzymywany przed finish, jeżeli nie ma kontroli
stron. Wznów przez start, wykonaj kontrolę i przypnij ustalenia do nowego źródła.
Ukończone wcześniej błędne importy wymagają osobnego, jawnego zadania naprawy.
