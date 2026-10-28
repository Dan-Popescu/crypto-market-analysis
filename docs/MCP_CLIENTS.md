# Utiliser les données depuis une IA

Le moteur ne choisit pas de modèle et ne facture pas d'appels LLM : ChatGPT, Claude ou un autre client MCP écrit l'analyse à partir des outils. La collecte doit être lancée séparément (`uv run crypto-market-analysis run`).

## Client local

`examples/mcp-config.json` contient un exemple STDIO pour les clients acceptant le format `mcpServers`. Remplacez les deux chemins absolus et vérifiez que `uv` est accessible au client. Exécutez d'abord `uv sync --frozen` dans le projet. Le client lance ensuite le serveur. Choisissez explicitement `--mode demo` si vous souhaitez utiliser les données synthétiques.

## ChatGPT sur le web ou Claude.ai

Le serveur Streamable HTTP existe au chemin `/mcp`. L'usage distant nécessite un domaine HTTPS et le fournisseur OAuth décrit dans [OPERATIONS.md](OPERATIONS.md), puis l'ajout de cette URL comme connexion MCP personnalisée dans le client. L'accès dépend des permissions du compte. Les exemples fournis n'exposent aucun service publiquement et ne connectent aucun compte automatiquement.

Documentation des clients : [MCP personnalisé dans ChatGPT](https://developers.openai.com/api/docs/guides/custom-mcp-server) ; [connecteurs MCP distants Claude](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp).

## Outils disponibles

| Outil | Rôle |
|---|---|
| `get_analysis_bundle` | Dossier horodaté, marché de référence, observations, scénarios et limites |
| `get_source_status` | Couverture, erreurs d'accès et fraîcheur |
| `get_forecast` | Fréquences historiques expérimentales à 1–60 jours |
| `get_evaluation` | Évaluation connue à chaque date ou exploration rétrospective explicitement choisie |
| `get_metric_history` | Séries bornées avec unités, devises et provenance |
| `explain_metric` | Définitions simples et limites d'interprétation |
| `render_chart` | Graphiques renvoyés comme images natives MCP |

Exemple de demande : « Utilise Crypto Market Analysis pour le bilan weekly de BTC et ETH. Commence par la couverture des sources. Donne les trois changements les plus utiles pour les 7, 21 et 60 prochains jours, les observations contradictoires et ce qui ferait changer la lecture. Trace le weekly et explique les termes techniques. Sépare les observations et les hypothèses ; n'invente aucune donnée ni probabilité. »

Les bilans Markdown de la CLI sont des synthèses déterministes. Le dossier JSON contient davantage d'éléments pour approfondir avec une IA. Les prévisions de la V1 reposent sur les prix et la volatilité historiques ; les données macro, réseau, options et levier enrichissent l'analyse descriptive et conversationnelle, sans être présentées comme des facteurs prédictifs déjà validés.
