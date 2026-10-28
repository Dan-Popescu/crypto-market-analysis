# Validation de la V1

Ce document conserve le compte rendu fourni avec le ZIP Crypto Observer v0.1.0.
Les résultats ci-dessous sont ceux déclarés dans cette livraison d'origine.
Les vérifications de l'import renommé sont consignées dans [IMPORT.md](IMPORT.md).

Vérifications d'origine réalisées le 28 octobre 2026 dans l'environnement de développement. Elles décrivent les résultats de cette exécution ; la disponibilité des interfaces publiques peut évoluer.

## Vérifications du logiciel

- Suite complète : **71 tests réussis**.
- `uv run ruff check src tests` : aucune erreur.
- `uv lock --check --offline` : fichier de verrouillage cohérent.
- Construction de la distribution Python avec `uv build --wheel` réussie.
- Intégration MCP testée sur STDIO et Streamable HTTP local, réponses JSON structurées et images natives comprises.
- Vérification des révisions de données, de leur disponibilité à chaque date, des fenêtres sans chevauchement, des jours incomplets et de l'alignement horaire des scénarios.
- Vérification de la séparation des sources, des devises et des modes réel/synthétique, de l'idempotence et de la reprise de la rétention après interruption.
- Deux revues séparées, sur les exigences et les conventions du projet ; les problèmes retenus ont été corrigés et les points sensibles revérifiés.

La dernière modification de présentation des bilans a également été vérifiée avec les tests CLI et le contrôle Ruff.

## Collecte réelle

Des bougies horaires ont été obtenues sur **10 plateformes** : Coinbase, Kraken, OKX, Bitstamp, Bitfinex, Bitget, Crypto.com, Gate, Gemini et HTX. L'historique demandé n'est pas garanti sur chaque plateforme.

Les quatre autres connecteurs du registre ont rencontré des restrictions : Binance a renvoyé HTTP 451 ; les catalogues publics de Bybit, KuCoin et MEXC étaient inaccessibles depuis cet environnement. Les erreurs apparaissent dans le statut et ne produisent jamais de données de remplacement.

Les collectes Deribit, DefiLlama, FRED et les métriques gratuites de base de Coin Metrics ont aussi été exécutées. Les métriques réservées aux offres payantes ne sont pas activées.

Le statut MiCA reste **à vérifier par entité juridique et juridiction**. Le logiciel n'attribue pas un statut réglementaire à une marque entière.

## Exemples fournis

Dans le ZIP d'origine, les fichiers `live-*`, `btc-daily-live.md` et les deux graphiques `btc-*-live.png` proviennent d'une collecte réelle, avec une analyse arrêtée au `2026-10-28T18:07:44Z`. Ces sept sorties sont exclues du dépôt GitHub ; les fichiers `demo-*` synthétiques y sont conservés. Le projet n'embarque pas la base complète de collecte.

Un historique Bitstamp d'environ cinq ans a permis de calculer des fréquences expérimentales sur les cinq horizons. Il ne démontre aucun avantage prédictif.

L'évaluation stricte `known_at` produit **zéro origine évaluable** sur cet historique téléchargé aujourd'hui : il n'était pas connu du système aux dates historiques simulées. L'évaluation `retrospective` est disponible pour explorer les données courantes, avec un avertissement explicite. Elle ne constitue pas une validation dans les conditions réelles.

## Ce qui reste à configurer ou à mesurer

- Le serveur n'est pas déployé publiquement et aucun compte ChatGPT ou Claude n'est connecté. L'accès depuis leurs sites web demande un hébergement HTTPS et un fournisseur OAuth compatible.
- Les exemples Docker sont fournis ; leur construction n'a pas été vérifiée, Docker étant absent de cet environnement.
- Les frais d'infrastructure et le volume de stockage sur plusieurs mois n'ont pas été mesurés. Les carnets détaillés sont compactés en statistiques horaires après sept jours par défaut.
- L'historique macro utilise les valeurs actuellement publiées ; il n'est pas présenté comme un historique des premières publications. Le calendrier et les flux ETF passent par des imports sourcés.
- Le suivi détaillé des whales est différé. Les scénarios de la V1 utilisent les prix et la volatilité ; les autres données enrichissent le dossier que l'IA peut interpréter, sans prétendre à un modèle causal validé.

La collecte continue commence uniquement lorsqu'on lance `uv run crypto-market-analysis run`. La livraison ne crée pas de tâche planifiée extérieure.
