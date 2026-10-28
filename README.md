# Crypto Market Analysis

Outil local d'analyse du marché BTC/ETH à partir de données publiques : collecte, analyses descriptives, scénarios historiques expérimentaux, graphiques et accès MCP en lecture seule. Aucune exécution de transactions. La collecte, les calculs et les graphiques n'appellent aucun modèle de langage.

## Démarrage

Dans le répertoire du projet, avec Python et `uv` installés :

```bash
uv sync --frozen
uv run crypto-market-analysis demo
uv run crypto-market-analysis brief --mode demo --asset BTC
uv run crypto-market-analysis chart --mode demo --asset BTC --kind weekly --output output/btc-weekly.svg
```

La démonstration est **synthétique** et stockée en mode `demo` séparé du mode `live` : elle ne décrit pas le marché réel. Aucun échec de collecte ne déclenche de données synthétiques. Pour collecter des données réelles et examiner la couverture :

```bash
uv run crypto-market-analysis collect --venues kraken,coinbase --days 7
uv run crypto-market-analysis status
uv run crypto-market-analysis brief --asset BTC
```

Le registre contient 14 plateformes par défaut ; disponibilité, actifs, historiques et accès varient selon la plateforme et la région. Une collecte initiale plus longue peut être demandée, sans garantie d'obtenir tout l'historique :

```bash
uv run crypto-market-analysis collect --venues kraken,coinbase --tasks market --days 1825 --max-pages 100
```

La pagination est bornée à 100 pages, jusqu'à 500 bougies par page, avec des limites parfois plus strictes côté plateforme. Contrôlez le résultat et `status` : données partielles, erreurs et absence de source restent visibles. Les collectes ultérieures peuvent être automatisées avec `uv run crypto-market-analysis run`.

Les prévisions sont expérimentales : elles comparent des situations historiques, ne démontrent aucun avantage prédictif et n'établissent pas de causalité. Les journées doivent contenir 24 bougies horaires complètes ; les semaines vont du lundi au dimanche UTC. Les cotations USD, EUR et stablecoins restent séparées. Les interfaces publiques de collecte ne nécessitent aucune API payante (coût d'API de collecte : 0 €) ; les coûts d'infrastructure, de réseau et d'hébergement ne sont pas chiffrés sans mesure.

Consultez [l'exploitation](docs/OPERATIONS.md), le [catalogue des données](docs/DATA_CATALOG.md), [la connexion des clients MCP](docs/MCP_CLIENTS.md), et `uv run crypto-market-analysis --help`.

Le [compte rendu de validation](docs/VALIDATION.md) précise les vérifications réalisées et les limites de cette première version.

## Développement local ou dans le cloud

Le dépôt est [Dan-Popescu/crypto-market-analysis](https://github.com/Dan-Popescu/crypto-market-analysis).
Il reprend le prototype Crypto Observer v0.1.0 sous le nom **Crypto Market Analysis**.
Le paquet Python est `crypto_market_analysis` et la commande est `crypto-market-analysis`.
Les variables `OBSERVER_*`, le scope `observer:read` et le fichier `observer.toml`
gardent leur nom pour conserver les réglages existants.

Après avoir récupéré le dépôt, préparez les dépendances puis lancez les contrôles :

```bash
uv sync --frozen --extra dev
uv run --frozen python tools/check.py
```

Ces commandes s'utilisent sur le PC et dans un environnement Codex cloud où `uv`
est installé. Les contrôles vérifient les dépendances, le code et les tests avec
des données temporaires ; ils ne lancent pas de collecte publique. Les tests MCP
utilisent un processus local et une connexion HTTP locale temporaire.

GitHub Actions est désactivé pour ce dépôt. Les résultats de vérification de la
version finale doivent être indiqués dans chaque pull request avant sa fusion.

Le dépôt contient le code et les démonstrations synthétiques. Les rapports réels
du ZIP d'origine restent locaux, comme les bases, les secrets et les réglages
personnels. Ledgerly conserve la gestion du portefeuille ; son raccordement à
cet outil d'analyse n'est pas encore réalisé.
