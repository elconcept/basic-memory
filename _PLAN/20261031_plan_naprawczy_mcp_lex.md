# BASIC-MEMORY — plan usunięcia dryfu konfiguracji i ochrony indeksu

> Audyt: 2026-10-31. Źródła: test wszystkich narzędzi przez portal lex.xlap.pl/mcp, testy bezpośrednich kontenerów przez Portainer/Synology, kontrola kodu repozytoriów elconcept. To **plan**, nie potwierdzenie wdrożenia naprawy. Oznaczenia: **POTWIERDZONE** = zaobserwowany wynik i/lub wskazana przyczyna w kodzie; **DO SPRAWDZENIA** = hipoteza. 

## Reguły wspólne
- Publiczny interfejs narzędzi, zapytania w języku naturalnym, metadane prezentacyjne, fragmenty i błędy: **tylko po polsku**. Bez widocznego selektora języka. Identyfikatory techniczne, oryginalne sygnatury i adresy źródłowe pozostają niezmienione. W okresie migracji stary parametr `lang` można akceptować dla zgodności, ignorować i stosować PL; potem usunąć z publicznego schematu.
- Wyniki prawne nie mogą być dopowiadane. Rozróżniać tekst urzędowy (ze wskazaniem języka i źródła) od komentarza/nieurzędowego przekładu; przy braku wiarygodnej polskiej wersji raportować brak, nie zastępować angielskim tekstem ani nie udawać polskiej wersji urzędowej.
- MCP `content` (dane merytoryczne) **i** `structuredContent` (metadane) zachować równolegle. Testować bezpośrednio oraz przez Cloudflare i portal `lex`; nie uznawać samej listy `citations` za sukces funkcjonalny.
- Diagnostyka i testy przed zmianą produkcyjną; PR → testy offline → smoke staging → porównanie surowych odpowiedzi → kontrolowany rollout → monitoring/rollback. Nigdy nie publikować haseł, tokenów, PII ani pełnych logów uwierzytelnienia.

## Materiał dowodowy
- **POTWIERDZONE:** historyczny `docker-compose.nas.yml` wskazywał `intfloat/multilingual-e5-large`, provider `openai`, 1024 wymiary i `shared-e5-large`; diagnostyka instancji wskazywała środowiskowy override `fastembed`, `intfloat/multilingual-e5-small`, 384 wymiary.
- **WYKONANE 2026-10-31:** commit `25eabcd371816fe332d65e5c940b87fc8477438d` poprawił `docker-compose.nas.yml` do FastEmbed/e5-small/384, usunął zależność sieciową i klucz starego serwisu. **Nie** wdrożono go automatycznie do kontenera.
- **POTWIERDZONE:** istnieje odrębny zdrowy kontener `shared-e5-small`, a BASIC-MEMORY działa jako obraz `mcp-basic-memory:wiki-shards-d8a6413d`; nie należy zakładać, że obraz jest równoważny checkoutowi GitHub. Projekty: `main`, `legal`, `model-memory`.
- **DO SPRAWDZENIA:** stan manifestu embeddingów i zgodność aktywnych wektorów z 384D, backend przechowywania (diagnostyka pokazywała SQLite oraz pgvector), miejsce trwałego cache FastEmbed, koszt i ryzyko ponownego indeksowania.

## Plan
1. **P0 — utrwalić bieżący stan:** zanonimizowany eksport konfiguracji runtime, obraz/digest, status health, listy wolumenów i indeksu; snapshot NAS / backup DB przed zmianami.
2. **P0 — spójność indeksu:** odczytać manifest wektorów, policzyć próbkę wymiarów w tabelach i zweryfikować odpowiadający model; nie łączyć 1024D z 384D w tym samym indeksie. Ustalić czy działający tryb `fastembed` korzysta z dedykowanego kontenera `shared-e5-small` (odrębna usługa może być zbędna dla Basic Memory) czy z lokalnej biblioteki.
3. **P1 — porównanie konfiguracji:** repo `docker-compose.nas.yml` vs definicja stacka Portainer 18 vs zmienne procesu; usunąć rozbieżności z instrukcji i workflow, ustalić jeden autorytatywny opis.
4. **P1 — test semantyczny po polsku:** pytania fleksyjne/diakrytyczne z projektów `legal` i `main`; porównać top-k, trafność, uprawnienia izolacji projektów, latencję oraz brak znikających notatek.
5. **P2 — decyzja wdrożeniowa:** dopiero po spójności i backupie zastosować poprawiony compose; nigdy nie wykonywać bezrefleksyjnego `reindex` / usuwania wolumenów. W razie zmiany backendu przygotować izolowany nowy indeks, migrację i rollback.

## Akceptacja
- `diagnostics`, manifest wektorów, faktyczny embedder i kontrolny wymiar dają zgodne wyniki: `multilingual-e5-small`, 384D; stare E5-large nie wraca po redeployu.
- Co najmniej pięć polskich zapytań na projekt uzyskuje oczekiwane trafienia; brak cross-project leakage; brak regresji danych, health stabilny.
- Instrukcje i odpowiedzi opisowe po polsku; angielskie identyfikatory modeli są wyjątkami technicznymi.

## Pliki / dowody
`docker-compose.nas.yml`, `.env.example`, `src/basic_memory/repository/embedding_provider_factory.py`, `semantic_vector_index_factory.py`, stack Portainer 18, diagnostyka MCP.
