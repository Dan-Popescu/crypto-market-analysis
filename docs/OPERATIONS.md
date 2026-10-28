# Exploitation

## Configuration et collecte

Copiez `examples/observer.toml` vers `observer.toml` et adaptez les plateformes, actifs et cadences. Un `data_dir` relatif au TOML est résolu par rapport à son répertoire. `--data-dir` permet de le surcharger. `uv run crypto-market-analysis venues` affiche le registre ; une marque ne suffit pas à établir le statut MiCA d'une entité.

```bash
uv run crypto-market-analysis collect --venues kraken,coinbase --tasks market,liquidity,options,context --days 7
uv run crypto-market-analysis run --config observer.toml
uv run crypto-market-analysis status --config observer.toml
```

Le collecteur permanent programme marché (1 h), carnet (5 min), options (6 h) et contexte (24 h) par défaut ; chaque source peut être indisponible ou partielle. Un statut 403, 451 ou une limite régionale reste un échec d'accès, jamais une donnée neutre. N'essayez pas de le contourner. La collecte utilise les sources publiques sans API payante et sans modèle de langage. Les modes `live`, `demo` et `import` sont séparés ; aucun basculement synthétique automatique n'existe.

Pour générer un rapport quotidien local indépendant, créez un répertoire de sortie et ajoutez par exemple cette ligne à la crontab, en remplaçant les chemins absolus :

```cron
CRON_TZ=Europe/Paris
10 8 * * * cd /chemin/crypto-market-analysis && /chemin/vers/uv run --no-sync crypto-market-analysis brief --asset BTC --output /chemin/rapports/btc.md
```

La prise en charge de `CRON_TZ` dépend de l'implémentation de cron. Cette ligne écrit le rapport localement ; elle ne l'envoie pas. L'heure de lancement suit Europe/Paris et son changement d'heure, mais les clôtures de marché et les semaines complètes sont en UTC : la semaine commence le lundi à 00:00 UTC et ne compte qu'après la clôture du dimanche UTC. Le rapport dépend des données déjà collectées ; surveillez leur fraîcheur avec `status`.

## Stockage, export et sauvegarde

SQLite utilise WAL et permet au collecteur et au serveur MCP de partager la même base locale. Conservez ensemble le fichier de base et ses fichiers WAL/SHM quand ils existent ; pour une sauvegarde cohérente, utilisez la fonction de sauvegarde SQLite ou arrêtez les processus avant de copier. Le volume partagé doit fournir la sémantique de verrouillage locale SQLite ; évitez de supposer qu'un stockage réseau convient.

```bash
uv run crypto-market-analysis export --output output/parquet
uv run python -c "import duckdb; print(duckdb.sql(\"SELECT source, asset, quote, count(*) AS n FROM read_parquet('output/parquet/candles.parquet') GROUP BY 1,2,3\").df())"
```

L'export écrit les dernières versions disponibles en Parquet Zstd ; un fichier n'est créé que si la table contient des données. Il n'est pas une archive complète de toutes les versions. L'exemple DuckDB est une inspection locale sur un fichier exporté, pas une interface SQL exposée par MCP.

Le détail des résumés de carnet est conservé sept jours (`liquidity_retention_days`). Le collecteur permanent compacte chaque jour les points plus anciens en médianes horaires, avec minimum, maximum, nombre d'observations et couverture. Un retard est traité par lots de sept journées anciennes, puis le collecteur reprend le rattrapage à l'itération suivante tant qu'il reste des points éligibles. Cette réduction ne touche pas les bougies, le funding ou les options. Elle limite la possibilité de rejouer les anciennes observations intrajournalières ; les médianes portent la date de disponibilité de leur calcul. La commande `uv run crypto-market-analysis maintain --keep-days 7` traite la journée ancienne la plus éloignée ; répétez-la si `remaining_old_rows` est non nul pour un rattrapage manuel. Aucun stockage de ticks n'est effectué.

## Évaluer les scénarios

Les scénarios utilisent des fenêtres de 24 heures alignées sur la dernière clôture horaire connue ; l'heure et le prix d'ancrage sont affichés. Les jours et semaines des graphiques calendaires restent en UTC. `uv run crypto-market-analysis evaluate --asset BTC --horizon 7` utilise `known_at` : chaque origine est évaluée uniquement avec les versions de bougies déjà observées à cette date, à la même heure UTC d'ancrage que le scénario actuel. Le délai effectif de collecte est enregistré ; au-delà d'une heure de retard, l'origine est exclue. Les extrêmes de la première fenêtre peuvent inclure un mouvement survenu avant l'émission : les résultats de barrière restent un diagnostic fondé sur les bougies, pas une validation d'exécution à un prix négociable. Un historique téléchargé aujourd'hui ne constitue pas une archive de ce qui était connu il y a un an : il ne produit donc pas artificiellement des résultats de validation à ces anciennes dates. La couverture de validation se construit avec la conservation des versions et le suivi dans le temps.

Pour explorer un historique actuel, `uv run crypto-market-analysis evaluate --asset BTC --horizon 7 --validation retrospective --output output/evaluation.json` réalise une simulation chronologique explicitement rétrospective, sans garantir la disponibilité historique des versions. Le graphique correspondant s'obtient avec `chart --kind calibration --validation retrospective --output output/calibration.png`. Ce diagnostic ne prouve ni une calibration en conditions réelles ni une rentabilité. Pour la démonstration synthétique, ajoutez `--mode demo` aux deux commandes.

## MCP local et distant

```bash
uv run crypto-market-analysis serve --transport stdio
uv run crypto-market-analysis serve --transport streamable-http --host 127.0.0.1 --port 8000
```

L'HTTP local écoute sur `127.0.0.1:8000`, chemin `/mcp`. STDIO convient à un client MCP local. Le mode distant nécessite un fournisseur OAuth externe, une configuration d'inscription compatible avec le client MCP (CIMD ou DCR selon le fournisseur/client), un proxy inverse HTTPS et des jetons avec le scope `observer:read`. Le serveur valide les JWT selon l'émetteur et les clés JWKS configurés. Réglez `OBSERVER_OAUTH_ISSUER`, `OBSERVER_JWKS_URL` et `OBSERVER_PUBLIC_URL` (URL publique HTTPS se terminant par `/mcp`) ; `OBSERVER_OAUTH_AUDIENCE` est facultatif. Une écoute sur `0.0.0.0` est refusée sans configuration OAuth. Auth0 ou Keycloak peuvent être configurés comme fournisseur externe ; aucun n'est installé, connecté ou déployé par ce projet. Vérifiez la prise en charge réelle du flux d'inscription par votre fournisseur et votre client avant toute exposition.

L'exemple `deploy/compose.yaml` lance collecteur et MCP avec une base commune, et garde MCP sur la boucle locale du système hôte Linux via `network_mode: host`. Depuis la racine :

```bash
docker compose -f deploy/compose.yaml up --build
```

Ce fichier est un exemple local, pas un déploiement public ni une configuration OAuth de production. Le réseau hôte diffère selon la plateforme Docker ; vérifiez son comportement avant utilisation.

## Imports

`examples/events.json` est une liste vide valide : remplacez-la par vos propres événements vérifiés. Chaque objet exige `title`, `event_time` (epoch secondes ou ISO avec fuseau) et `source_url` HTTPS, par exemple avec une date effective vérifiée sur une source officielle comme https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm. Ne remplissez pas de dates supposées. `uv run crypto-market-analysis import-events --file examples/events.json` importe ce calendrier comme manuel et non exhaustif ; la date de publication historique n'est pas vérifiée automatiquement. Aucun flux ETF automatique complet n'est revendiqué.

Les bougies peuvent être importées via `import-candles` avec source et cotation explicites ; consulter `--help`. Les données importées sont isolées en mode `import`.

Le CSV de bougies attend `open_time,open,high,low,close,volume` ; `open_time` est l'ouverture de l'heure UTC en secondes Unix ou date ISO avec fuseau. Colonnes facultatives : `quote_volume,taker_buy_quote_volume`. Exemple : `uv run crypto-market-analysis import-candles --file candles.csv --source mon-fournisseur --asset BTC --quote USD`, puis `uv run crypto-market-analysis brief --mode import`.

`import-metrics --file measures.csv` accepte les colonnes `source,asset,metric,event_time,value,unit,quote,source_url` et crée des observations en mode `import`. Une URL HTTPS de provenance est obligatoire ; `event_time` est en secondes Unix ou date ISO avec fuseau. Pour des flux ETF, choisissez une métrique explicite comme `etf_net_flow_usd`, l'actif BTC ou ETH et une unité USD, en vérifiant qu'il s'agit de flux et non d'une variation de valeur des avoirs. Le moment de disponibilité historique n'est pas reconstitué : ces imports ne sont pas des variables admissibles au backtest actuel.
